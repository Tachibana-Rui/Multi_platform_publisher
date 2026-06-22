from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import shutil
from urllib.parse import urlparse

from fastapi import HTTPException
import httpx
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .assets import inspect_media
from .config import settings


PAGE_HOSTS = ("douyin.com", "iesdouyin.com")


@dataclass(frozen=True)
class ParsedDouyinPost:
    item_id: str
    title: str
    body: str
    tags: list[str]


def _host_allowed(host: str | None) -> bool:
    host = (host or "").casefold().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in PAGE_HOSTS)


def normalize_douyin_url(value: str) -> str:
    match = re.search(r"https?://[^\s<>\"']+", value)
    if not match:
        raise HTTPException(status_code=422, detail="没有找到有效的抖音作品链接")
    url = match.group(0).rstrip("。；，、,.!！?)）]")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not _host_allowed(parsed.hostname):
        raise HTTPException(status_code=422, detail="仅支持抖音作品链接或 v.douyin.com 分享短链")
    return url


async def _resolve_url(source_url: str) -> str:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        ) as client:
            response = await client.get(source_url)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="连接抖音失败，请稍后重试") from exc
    resolved = str(response.url)
    if not _host_allowed(urlparse(resolved).hostname):
        raise HTTPException(status_code=422, detail="抖音短链跳转到了不受信任的地址")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"抖音页面返回状态 {response.status_code}")
    return resolved


def _download_douyin(
    resolved_url: str,
    post_id: str,
    progress_callback: Callable[[dict], None] | None = None,
) -> tuple[str, ParsedDouyinPost, list[dict]]:
    target_dir = settings.upload_dir / post_id / "downloads"
    target_dir.mkdir(parents=True, exist_ok=False)
    profile_dir = settings.browser_profile_dir / "douyin"
    def report_progress(data: dict) -> None:
        if not progress_callback:
            return
        info = data.get("info_dict") if isinstance(data.get("info_dict"), dict) else {}
        title = str(info.get("description") or info.get("title") or resolved_url).strip()
        progress_callback({
            "post_name": title.splitlines()[0][:200] if title else resolved_url,
            "image_downloaded": 0,
            "image_total": 0,
        })

    options = {
        "outtmpl": str(target_dir / "%(id)s.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": settings.max_upload_bytes,
        "http_headers": {"Referer": "https://www.douyin.com/"},
        "progress_hooks": [report_progress],
    }
    if profile_dir.is_dir():
        options["cookiesfrombrowser"] = ("edge", str(profile_dir), None, None)

    def cleanup_target() -> None:
        shutil.rmtree(target_dir, ignore_errors=True)
        try:
            target_dir.parent.rmdir()
        except OSError:
            pass

    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(resolved_url, download=True)
            if not isinstance(info, dict):
                raise HTTPException(status_code=422, detail="抖音没有返回可识别的作品数据")
            extractor = str(info.get("extractor_key") or info.get("extractor") or "").casefold()
            if "douyin" not in extractor:
                raise HTTPException(status_code=422, detail="链接解析结果不是抖音作品")
            downloads = info.get("requested_downloads") or []
            filepath = next((item.get("filepath") for item in downloads if item.get("filepath")), None)
            path = Path(filepath or downloader.prepare_filename(info)).resolve()
            if not path.is_file() or not path.is_relative_to(target_dir.resolve()):
                candidates = [item for item in target_dir.iterdir() if item.is_file()]
                path = candidates[0] if len(candidates) == 1 else path
            if not path.is_file():
                raise HTTPException(status_code=422, detail="抖音作品没有可下载的视频")
            if path.stat().st_size > settings.max_upload_bytes:
                raise HTTPException(status_code=413, detail="抖音视频超过单文件大小限制")

            digest = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            width, height, duration = inspect_media(path, "video")
            description = str(info.get("description") or info.get("title") or "").strip()
            item_id = str(info.get("id") or "")
            title = description.splitlines()[0][:200] if description else f"抖音作品 {item_id}"
            if progress_callback:
                progress_callback({
                    "post_name": title,
                    "image_downloaded": 0,
                    "image_total": 0,
                })
            tags = []
            for value in [*(info.get("tags") or []), *re.findall(r"#([^#\s]+)", description)]:
                value = str(value).strip().lstrip("#")
                if value and value not in tags:
                    tags.append(value[:50])
            canonical_url = str(info.get("webpage_url") or resolved_url)
            return canonical_url, ParsedDouyinPost(
                item_id=item_id,
                title=title,
                body=description,
                tags=tags[:30],
            ), [{
                "original_name": path.name[:255],
                "storage_name": path.relative_to(settings.upload_dir).as_posix(),
                "media_type": "video",
                "mime_type": "video/mp4" if path.suffix.casefold() == ".mp4" else "video/webm",
                "file_size": path.stat().st_size,
                "checksum": digest.hexdigest(),
                "width": width,
                "height": height,
                "duration_seconds": duration,
                "position": 0,
            }]
    except HTTPException:
        cleanup_target()
        raise
    except (DownloadError, OSError, ValueError) as exc:
        cleanup_target()
        message = str(exc).splitlines()[-1][:300]
        raise HTTPException(
            status_code=422,
            detail=f"无法导入抖音作品：{message}。如作品需要登录，请先在账号管理中登录抖音",
        ) from exc


async def import_public_douyin(
    source_url: str,
    post_id: str,
    progress_callback: Callable[[dict], None] | None = None,
) -> tuple[str, ParsedDouyinPost, list[dict]]:
    normalized = normalize_douyin_url(source_url)
    resolved = await _resolve_url(normalized)
    return await asyncio.to_thread(_download_douyin, resolved, post_id, progress_callback)
