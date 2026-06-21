from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import shutil
from urllib.parse import urljoin, urlparse

from fastapi import HTTPException
import httpx
from yt_dlp.utils import js_to_json

from .assets import inspect_media
from .config import settings


PAGE_HOSTS = ("xiaohongshu.com", "xhslink.com")
MEDIA_HOSTS = ("xhscdn.com", "xhscdn.net", "xiaohongshu.com")
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)


@dataclass(frozen=True)
class MediaSource:
    url: str
    media_type: str
    label: str


@dataclass(frozen=True)
class ParsedNote:
    note_id: str
    title: str
    body: str
    tags: list[str]
    media: list[MediaSource]


def _host_allowed(host: str | None, allowed: tuple[str, ...]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed)


def normalize_source_url(value: str) -> str:
    match = re.search(r"https?://[^\s<>\"']+", value)
    if not match:
        raise HTTPException(status_code=422, detail="没有找到有效的小红书链接")
    url = match.group(0).rstrip("。；，、,.!！?)）]")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not _host_allowed(parsed.hostname, PAGE_HOSTS):
        raise HTTPException(status_code=422, detail="仅支持小红书作品链接或 xhslink 分享短链")
    return url


def _balanced_object(source: str, start: int) -> str:
    if start < 0:
        raise ValueError("initial state object is absent")
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise ValueError("initial state object is incomplete")


def _find_note(initial_state: dict, source_url: str) -> tuple[str, dict]:
    path_match = re.search(r"/(?:explore|discovery/item)/([\da-f]+)", urlparse(source_url).path)
    expected_id = path_match.group(1) if path_match else None
    detail_map = initial_state.get("note", {}).get("noteDetailMap", {})
    if expected_id and isinstance(detail_map.get(expected_id), dict):
        note = detail_map[expected_id].get("note")
        if isinstance(note, dict):
            return expected_id, note
    for key, value in detail_map.items() if isinstance(detail_map, dict) else []:
        note = value.get("note") if isinstance(value, dict) else None
        if isinstance(note, dict) and note:
            return str(note.get("noteId") or key), note
    raise ValueError("note detail is absent")


def _valid_media_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and _host_allowed(parsed.hostname, MEDIA_HOSTS):
        return value
    return None


def _video_urls(value: dict) -> list[object]:
    backups = value.get("backupUrls")
    if not isinstance(backups, list):
        backups = [backups] if backups else []
    return [value.get("masterUrl"), *backups]


def _walk_video_streams(value: object):
    if isinstance(value, dict):
        if any(_valid_media_url(url) for url in _video_urls(value)):
            yield value
        for child in value.values():
            yield from _walk_video_streams(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_video_streams(child)


def parse_note_page(html: str, source_url: str) -> ParsedNote:
    marker = re.search(r"window\.__INITIAL_STATE__\s*=", html)
    if not marker:
        raise HTTPException(
            status_code=422,
            detail="页面没有公开作品数据，链接可能已失效、仅登录可见或触发了平台验证",
        )
    object_start = html.find("{", marker.end())
    try:
        raw_state = _balanced_object(html, object_start)
        initial_state = json.loads(js_to_json(raw_state))
        note_id, note = _find_note(initial_state, source_url)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="无法解析小红书作品页面数据") from exc

    media: list[MediaSource] = []
    video_streams = list(_walk_video_streams(note.get("video", {}).get("media", {}).get("stream", {})))
    if video_streams:
        best = max(
            video_streams,
            key=lambda item: (
                int(item.get("width") or 0) * int(item.get("height") or 0),
                int(item.get("videoBitrate") or item.get("avgBitrate") or 0),
            ),
        )
        video_url = next(
            url for url in _video_urls(best)
            if _valid_media_url(url)
        )
        media.append(MediaSource(video_url, "video", "video"))

    seen_urls: set[str] = set()
    image_list = note.get("imageList") if isinstance(note.get("imageList"), list) else []
    for index, image in enumerate(image_list, start=1):
        if not isinstance(image, dict):
            continue
        image_url = None
        for key in ("urlDefault", "urlPre", "url"):
            if candidate := _valid_media_url(image.get(key)):
                image_url = candidate
                break
        if image_url and image_url not in seen_urls:
            seen_urls.add(image_url)
            media.append(MediaSource(image_url, "image", f"image_{index:02d}"))

    if not media:
        raise HTTPException(status_code=422, detail="作品中没有解析到可下载的图片或视频")

    tags = []
    for tag in note.get("tagList") or []:
        name = tag.get("name") if isinstance(tag, dict) else None
        if isinstance(name, str) and name.strip() and name.strip() not in tags:
            tags.append(name.strip())
    title = str(note.get("title") or "").strip() or f"小红书作品 {note_id}"
    return ParsedNote(
        note_id=note_id,
        title=title[:200],
        body=str(note.get("desc") or "").strip(),
        tags=tags[:30],
        media=media[: settings.max_import_assets],
    )


async def _fetch_page(client: httpx.AsyncClient, source_url: str) -> tuple[str, str]:
    current = source_url
    for _ in range(6):
        if not _host_allowed(urlparse(current).hostname, PAGE_HOSTS):
            raise HTTPException(status_code=422, detail="小红书短链跳转到了不受信任的地址")
        try:
            response = await client.get(current, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="连接小红书失败，请稍后重试") from exc
        if response.status_code in REDIRECT_STATUSES:
            location = response.headers.get("location")
            if not location:
                break
            current = urljoin(current, location)
            continue
        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"小红书页面返回状态 {response.status_code}，作品可能需要登录或已不可见",
            )
        return str(response.url), response.text
    raise HTTPException(status_code=422, detail="小红书短链跳转次数过多")


def _extension(content_type: str, url: str, media_type: str) -> str:
    normalized = content_type.split(";", 1)[0].lower()
    overrides = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "image/gif": ".gif", "video/mp4": ".mp4", "video/quicktime": ".mov",
        "video/webm": ".webm",
    }
    if normalized in overrides:
        return overrides[normalized]
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix and len(suffix) <= 6:
        return suffix
    guessed = mimetypes.guess_extension(normalized)
    return guessed or (".mp4" if media_type == "video" else ".jpg")


async def _download_media(
    client: httpx.AsyncClient,
    source: MediaSource,
    target_dir: Path,
    position: int,
    referer: str,
) -> tuple[dict, Path]:
    current = source.url
    for _ in range(5):
        if not _host_allowed(urlparse(current).hostname, MEDIA_HOSTS):
            raise HTTPException(status_code=422, detail="作品媒体地址不受信任")
        try:
            async with client.stream(
                "GET", current, headers={"Referer": referer}, follow_redirects=False
            ) as response:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        break
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise HTTPException(status_code=502, detail=f"第 {position} 个媒体文件下载失败")
                mime_type = response.headers.get("content-type", "application/octet-stream")
                if mime_type.startswith("text/"):
                    raise HTTPException(status_code=422, detail=f"第 {position} 个媒体文件返回了无效内容")
                suffix = _extension(mime_type, current, source.media_type)
                filename = f"{position:02d}_{source.label}{suffix}"
                target = target_dir / filename
                digest = hashlib.sha256()
                size = 0
                with target.open("wb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > settings.max_upload_bytes:
                            raise HTTPException(status_code=413, detail=f"第 {position} 个媒体文件过大")
                        digest.update(chunk)
                        output.write(chunk)
                width, height, duration = inspect_media(target, source.media_type)
                return ({
                    "original_name": filename,
                    "storage_name": "",  # Set by the caller after path normalization.
                    "media_type": source.media_type,
                    "mime_type": mime_type.split(";", 1)[0],
                    "file_size": size,
                    "checksum": digest.hexdigest(),
                    "width": width,
                    "height": height,
                    "duration_seconds": duration,
                    "position": position - 1,
                }, target)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"第 {position} 个媒体文件下载中断") from exc
    raise HTTPException(status_code=422, detail="媒体地址跳转次数过多")


async def import_public_note(source_url: str, post_id: str) -> tuple[str, ParsedNote, list[dict]]:
    source_url = normalize_source_url(source_url)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    target_dir = settings.upload_dir / post_id
    target_dir.mkdir(parents=True, exist_ok=False)
    total_size = 0
    try:
        async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(45.0)) as client:
            canonical_url, html = await _fetch_page(client, source_url)
            note = parse_note_page(html, canonical_url)
            assets: list[dict] = []
            for position, source in enumerate(note.media, start=1):
                details, path = await _download_media(client, source, target_dir, position, canonical_url)
                total_size += details["file_size"]
                if total_size > settings.max_import_total_bytes:
                    raise HTTPException(status_code=413, detail="作品媒体文件总大小超过导入限制")
                details["storage_name"] = f"{post_id}/{path.name}"
                assets.append(details)
            return canonical_url, note, assets
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
