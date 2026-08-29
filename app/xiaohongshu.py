from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import random
import re
import shutil
import time
from urllib.parse import urljoin, urlparse

from fastapi import HTTPException
from yt_dlp.utils import js_to_json
import json

from .assets import inspect_media
from .config import settings


PAGE_HOSTS = ("xiaohongshu.com", "xhslink.com")
MEDIA_HOSTS = ("xhscdn.com", "xhscdn.net", "xiaohongshu.com")
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
PAUSE_STATUSES = {401, 403, 429}
TRANSIENT_STATUSES = {408, 500, 502, 503, 504}


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

    # 1. 优先从 noteDetailMap 查找
    detail_map = initial_state.get("note", {}).get("noteDetailMap", {})
    if expected_id and isinstance(detail_map.get(expected_id), dict):
        note = detail_map[expected_id].get("note")
        if isinstance(note, dict):
            return expected_id, note
    for key, value in detail_map.items() if isinstance(detail_map, dict) else []:
        note = value.get("note") if isinstance(value, dict) else None
        if isinstance(note, dict) and note:
            return str(note.get("noteId") or key), note

    # 2. 从 feed.feeds 列表查找（页面可能通过 feed 方式渲染）
    feeds = initial_state.get("feed", {}).get("feeds", [])
    if isinstance(feeds, list):
        for item in feeds:
            if not isinstance(item, dict):
                continue
            note_candidate = item.get("note") if isinstance(item.get("note"), dict) else item
            if "noteId" in note_candidate and "title" in note_candidate:
                if expected_id and str(note_candidate.get("noteId")) == expected_id:
                    return expected_id, note_candidate
                if not expected_id and note_candidate:
                    return str(note_candidate.get("noteId")), note_candidate

    # 3. 递归搜索整个 state，找到包含 noteId 和 media/imageList 的对象
    def _recursive_search(obj: object, depth: int = 0):
        if depth > 6:
            return None
        if isinstance(obj, dict):
            if (isinstance(obj.get("noteId"), str) and len(str(obj.get("noteId"))) >= 10 and
                (isinstance(obj.get("imageList"), list) or isinstance(obj.get("video"), dict))):
                return obj
            for v in obj.values():
                result = _recursive_search(v, depth + 1)
                if result is not None:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = _recursive_search(item, depth + 1)
                if result is not None:
                    return result
        return None

    found = _recursive_search(initial_state)
    if found:
        return str(found.get("noteId")), found

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
    except ValueError:
        # 检查是否为404/权限错误页面
        detail_map = {}
        try:
            detail_map = initial_state.get("note", {}).get("noteDetailMap", {})
        except Exception:
            detail_map = {}
        if isinstance(detail_map, dict) and len(detail_map) == 0:
            raise HTTPException(
                status_code=403,
                detail="该笔记不可公开访问（可能仅本人可见、已删除或需要登录小红书账号）。请在浏览器中打开该链接确认是否能公开访问。",
            )
        raise HTTPException(status_code=422, detail="无法解析小红书作品页面数据")
    except (TypeError, json.JSONDecodeError) as exc:
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
    return ".mp4" if media_type == "video" else ".jpg"


def _should_pause_import(exc: HTTPException) -> bool:
    detail = str(exc.detail)
    return exc.status_code in PAUSE_STATUSES or any(
        token in detail for token in ("验证码", "安全验证", "登录", "限流", "过于频繁", "429", "暂停")
    )


def _wait_between_media_downloads() -> None:
    delay = random.uniform(
        settings.import_media_delay_min_seconds,
        settings.import_media_delay_max_seconds,
    )
    if delay > 0:
        time.sleep(delay)


# -----------------------------------------------------------------------------
# 浏览器级下载入口
# -----------------------------------------------------------------------------

def _download_with_browser(
    source_url: str,
    post_id: str,
    progress_callback: Callable[[dict], None] | None = None,
) -> tuple[str, ParsedNote, list[dict]]:
    """使用 Playwright 浏览器级反检测下载小红书作品。

    与原 httpx 实现的主要差异：
    - 使用真实浏览器栈（TLS/HTTP/HTTP2 指纹与 Chrome 完全一致）
    - 自动注入反检测脚本（navigator.webdriver、window.chrome 等）
    - 使用 RequestHeaderTracker 动态跟踪页面导航与 Referer
    - 使用与发布系统一致的设备指纹配置
    """
    source_url = normalize_source_url(source_url)
    target_dir = settings.upload_dir / post_id / "downloads"
    target_dir.mkdir(parents=True, exist_ok=False)

    def report(payload: dict) -> None:
        if progress_callback:
            try:
                progress_callback(payload)
            except Exception:
                pass

    try:
        # 延迟导入浏览器下载管理器，避免在非下载场景下启动 Playwright
        from .browser_downloader import BrowserDownloadSession

        total_size = 0
        with BrowserDownloadSession(
            browser_type="chrome",
            profile_subdir="xiaohongshu",
            progress_callback=progress_callback,
        ) as session:
            page = session.fetch_page(source_url, wait_until="domcontentloaded")
            canonical_url = page.canonical_url
            # 优先用原始URL作为source_url传递给解析器（确保expected_id正确匹配）
            # 仅当原始URL无法提取note ID时，才用canonical_url
            try:
                note = parse_note_page(page.html, source_url)
            except ValueError:
                note = parse_note_page(page.html, canonical_url)

            image_total = sum(1 for source in note.media if source.media_type == "image")
            image_downloaded = 0
            report({
                "post_name": note.title,
                "image_downloaded": 0,
                "image_total": image_total,
            })

            assets: list[dict] = []
            for position, source in enumerate(note.media, start=1):
                if position > 1:
                    _wait_between_media_downloads()
                suffix = _extension("", source.url, source.media_type)
                filename = f"{position:02d}_{source.label}{suffix}"
                downloaded = session.download_media(
                    source.url,
                    target_dir=target_dir,
                    filename=filename,
                    media_type=source.media_type,
                    allowed_hosts=MEDIA_HOSTS,
                    referer=canonical_url,
                )
                path = target_dir / downloaded.original_name
                # 修正：重新从文件读取以确保元数据一致性
                # 这里我们用 inspect_media 检查并获得更精确的宽高
                width, height, duration = inspect_media(path, source.media_type)
                total_size += downloaded.file_size
                if total_size > settings.max_import_total_bytes:
                    raise HTTPException(status_code=413, detail="作品媒体文件总大小超过导入限制")

                asset_dict = {
                    "original_name": downloaded.original_name[:255],
                    "storage_name": path.relative_to(settings.upload_dir).as_posix(),
                    "media_type": source.media_type,
                    "mime_type": downloaded.mime_type,
                    "file_size": downloaded.file_size,
                    "checksum": downloaded.checksum,
                    "width": width,
                    "height": height,
                    "duration_seconds": duration,
                    "position": position - 1,
                }
                assets.append(asset_dict)
                if source.media_type == "image":
                    image_downloaded += 1
                report({
                    "post_name": note.title,
                    "image_downloaded": image_downloaded,
                    "image_total": image_total,
                })
        return canonical_url, note, assets
    except HTTPException:
        shutil.rmtree(target_dir, ignore_errors=True)
        try:
            target_dir.parent.rmdir()
        except OSError:
            pass
        raise
    except Exception as exc:
        shutil.rmtree(target_dir, ignore_errors=True)
        try:
            target_dir.parent.rmdir()
        except OSError:
            pass
        raise HTTPException(status_code=502, detail=f"下载失败：{type(exc).__name__}") from exc


async def import_public_note(
    source_url: str,
    post_id: str,
    progress_callback: Callable[[dict], None] | None = None,
) -> tuple[str, ParsedNote, list[dict]]:
    """异步包装器——在工作线程中运行同步的浏览器下载逻辑。

    Playwright 的同步 API 在 asyncio 内使用，需要通过 to_thread 隔离事件循环。
    """
    return await asyncio.to_thread(
        _download_with_browser,
        source_url,
        post_id,
        progress_callback,
    )