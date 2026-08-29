from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import random
import re
import shutil
import tempfile
import time
from urllib.parse import urlparse, urlunparse

from fastapi import HTTPException

from .assets import inspect_media
from .config import settings


PAGE_HOSTS = ("douyin.com", "iesdouyin.com", "tiktok.com")
MEDIA_HOSTS = (
    "douyin.com",
    "iesdouyin.com",
    "douyincdn.com",
    "douyinvod.com",
    "tiktok.com",
    "ttwstatic.com",
    "bytedance.net",
    "byteimg.com",
    "byteacdn.com",
)


@dataclass(frozen=True)
class DouyinPost:
    post_id: str
    title: str
    body: str
    author_display_name: str
    tags: list[str] = field(default_factory=list)


def _host_allowed(host: str | None, allowed: tuple[str, ...]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed)


def normalize_douyin_url(value: str) -> str:
    match = re.search(r"https?://[^\s<>\"']+", value)
    if not match:
        raise HTTPException(status_code=422, detail="没有找到有效的链接")
    url = match.group(0).rstrip("。；，、,.!！?)）]")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not _host_allowed(parsed.hostname, PAGE_HOSTS):
        raise HTTPException(status_code=422, detail="仅支持抖音 / TikTok 公开链接")
    return url


def _extract_post_id(html: str) -> str | None:
    """从抖音页面 HTML 中提取作品 ID。"""
    patterns = [
        r"(?:\b|/)(?:video|note)/([\w-]{3,})",
        r"(?:aweme_id|itemId|awemeId|item_id|videoId)\s*[:=]\s*['\"]?(\d{10,20})['\"]?",
        r'"itemId"\s*:\s*"(\d+)"',
        r'"aweme_id"\s*:\s*"(\d+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


def _extract_title(html: str) -> str:
    """从 HTML 中提取标题或描述文本。"""
    # 先尝试 <title>
    title_match = re.search(r"<title[^>]*>(.+?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else ""

    # 进一步提取 og:description
    desc_match = re.search(
        r'<meta\s+(?:name|property)\s*=\s*"(?:og:description|description)"\s+content\s*=\s*"([^"]+)"',
        html,
        flags=re.IGNORECASE,
    )
    description = desc_match.group(1).strip() if desc_match else ""

    if description and len(description) > 10:
        return description
    if title:
        # 去掉 " - 抖音" 等后缀
        title = re.sub(r"\s*[-|]\s*(抖音|Douyin|TikTok).*$", "", title, flags=re.IGNORECASE)
        return title.strip()
    return ""


def _extract_author(html: str) -> str:
    """从 HTML 中提取作者名。"""
    patterns = [
        r'"nickname"\s*:\s*"([^"]+)"',
        r'"author"\s*:\s*\{[^}]*"nickname"\s*:\s*"([^"]+)"',
        r'"authorName"\s*:\s*"([^"]+)"',
        r'<meta\s+property\s*=\s*"og:site_name"\s+content\s*=\s*"([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match and match.group(1):
            return match.group(1)
    return "抖音用户"


def _extract_tags(html: str) -> list[str]:
    """从 HTML 中提取话题标签。"""
    tags: list[str] = []
    # 提取 # 话题
    for match in re.finditer(r"[#＃]([\w\u4e00-\u9fff]+)", html):
        tag = match.group(1).strip()
        if tag and tag not in tags and len(tag) <= 30:
            tags.append(tag)
    # 如果页面有显式的话题数据
    for match in re.finditer(r'"text_extra"\s*:\s*\[[^\]]*\]|"hashtagList"\s*:\s*\[[^\]]*\]', html):
        for tag_match in re.finditer(r'"hashtag_name"\s*:\s*"([^"]+)"|"tagName"\s*:\s*"([^"]+)"', match.group(0)):
            tag = (tag_match.group(1) or tag_match.group(2) or "").strip()
            if tag and tag not in tags and len(tag) <= 30:
                tags.append(tag)
    return tags[:30]


def _sanitize_filename(name: str) -> str:
    """规范化文件名——替换不安全字符。"""
    sanitized = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", name)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    return sanitized[:120] or "douyin_post"


# -----------------------------------------------------------------------------
# 浏览器级下载入口
# -----------------------------------------------------------------------------

def _download_with_browser(
    source_url: str,
    post_id: str,
    progress_callback: Callable[[dict], None] | None = None,
) -> tuple[str, DouyinPost, list[dict]]:
    """使用 Playwright 浏览器级反检测下载抖音作品。

    策略：
    1. 先用浏览器访问页面，获得真实页面 HTML + 规范化 URL
    2. 通过 HTML 提取元数据（标题、作者、话题、可能的视频直链）
    3. 浏览器的 request API 下载媒体（替代 yt-dlp 的纯网络接口）

    这样既能使用完整的浏览器反检测栈，又可以逐步从 yt-dlp 过渡到
    浏览器 API，避免一次性大规模重构。
    """
    source_url = normalize_douyin_url(source_url)
    target_dir = settings.upload_dir / post_id / "downloads"
    target_dir.mkdir(parents=True, exist_ok=False)

    def report(payload: dict) -> None:
        if progress_callback:
            try:
                progress_callback(payload)
            except Exception:
                pass

    try:
        # 延迟导入
        from .browser_downloader import BrowserDownloadSession

        total_size = 0

        with BrowserDownloadSession(
            browser_type="chrome",
            profile_subdir="douyin",
            progress_callback=progress_callback,
        ) as session:
            page = session.fetch_page(source_url, wait_until="networkidle")
            canonical_url = page.canonical_url
            html = page.html

            # 解析元数据
            parsed_id = _extract_post_id(html) or post_id
            title_text = _extract_title(html) or f"抖音作品 {parsed_id}"
            author = _extract_author(html)
            tags = _extract_tags(html)
            post_info = DouyinPost(
                post_id=parsed_id,
                title=title_text[:200],
                body=title_text[:1000],
                author_display_name=author,
                tags=tags,
            )

            # 策略：如果 HTML 中能解析出直链（mp4/webm），就用浏览器 request API 下载
            # 否则，回退到 yt-dlp（通过 HTTP 下载，但继承浏览器 UA）
            direct_media_url = _extract_media_url(html)

            assets: list[dict] = []
            if direct_media_url:
                # 用浏览器 request API 下载视频
                report({
                    "post_name": post_info.title,
                    "image_downloaded": 0,
                    "image_total": 1,
                })
                filename = f"{_sanitize_filename(post_info.title)}_{parsed_id}.mp4"
                downloaded = session.download_media(
                    direct_media_url,
                    target_dir=target_dir,
                    filename=filename,
                    media_type="video",
                    allowed_hosts=MEDIA_HOSTS,
                    referer=canonical_url,
                )
                path = target_dir / downloaded.original_name
                width, height, duration = inspect_media(path, "video")
                total_size += downloaded.file_size
                if total_size > settings.max_import_total_bytes:
                    raise HTTPException(status_code=413, detail="作品媒体文件总大小超过导入限制")
                assets.append({
                    "original_name": downloaded.original_name[:255],
                    "storage_name": path.relative_to(settings.upload_dir).as_posix(),
                    "media_type": "video",
                    "mime_type": downloaded.mime_type,
                    "file_size": downloaded.file_size,
                    "checksum": downloaded.checksum,
                    "width": width,
                    "height": height,
                    "duration_seconds": duration,
                    "position": 0,
                })
                report({
                    "post_name": post_info.title,
                    "image_downloaded": 1,
                    "image_total": 1,
                })
            else:
                # 回退：使用 yt-dlp。关键差异：传递与页面一致的 UA 和 cookie。
                assets = _download_via_ytdlp(
                    source_url=canonical_url,
                    target_dir=target_dir,
                    session=session,
                    report=report,
                )

        return canonical_url, post_info, assets
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
        raise HTTPException(status_code=502, detail=f"抖音下载失败：{type(exc).__name__}") from exc


def _extract_media_url(html: str) -> str | None:
    """尝试从 HTML 中提取视频/图片直链。"""
    patterns = [
        r'"playAddr"\s*:\s*\[.*?"url"\s*:\s*"(https?://[^"]+\.(?:mp4|webm)[^"]*)"',
        r'"playAddr"\s*:\s*"(https?://[^"]+\.(?:mp4|webm)[^"]*)"',
        r'"video"\s*:\s*\{[^}]*"playAddr"\s*:\s*\{[^}]*"urlList"\s*:\s*\[[^\]]*"',
        r'"(?:src|play_url|video_url)"\s*:\s*"(https?://[^"]+\.(?:mp4|webm)[^"]*)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)

    # 进一步搜索视频直链（更宽松的模式）
    for match in re.finditer(r'"(https?://[^"]+\.(?:mp4|webm)[^"]*)"', html):
        url = match.group(1)
        if _host_allowed(urlparse(url).hostname, MEDIA_HOSTS):
            return url
    return None


def _download_via_ytdlp(
    *,
    source_url: str,
    target_dir: Path,
    session,  # BrowserDownloadSession 实例
    report: Callable[[dict], None],
) -> list[dict]:
    """回退路径：使用 yt-dlp 下载，但使用浏览器上下文的 cookie 和 UA。"""
    import yt_dlp

    total_size = 0

    # 从浏览器上下文获取 cookies 和 UA
    headers = session.header_tracker.build_ordered_headers()
    ua_override = None
    for key, value in headers:
        if key == "User-Agent":
            ua_override = value
            break
    cookies_text = _extract_cookies(session)

    report({
        "post_name": "抖音作品",
        "image_downloaded": 0,
        "image_total": 1,
    })

    ydl_opts = {
        "outtmpl": str(target_dir / "%(title).80s.%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": settings.import_media_retry_attempts,
        "socket_timeout": 30,
    }

    if cookies_text:
        cookie_file = target_dir / "_temp_cookies.txt"
        cookie_file.write_text(cookies_text, encoding="utf-8")
        ydl_opts["cookiefile"] = str(cookie_file)
    elif ua_override:
        ydl_opts["http_headers"] = {
            "User-Agent": ua_override,
            "Referer": "https://www.douyin.com/",
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(source_url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise HTTPException(status_code=502, detail=f"yt-dlp 下载失败：{str(exc)[:200]}") from exc

    # 找到已下载的文件
    downloaded_files = []
    for entry in info.get("entries", [info]) if isinstance(info, dict) else [info]:
        if not isinstance(entry, dict):
            continue
        filepath = entry.get("filepath") or entry.get("_filename")
        if filepath:
            downloaded_files.append(filepath)

    # 如果 yt-dlp 没有直接返回文件名，扫描目录
    if not downloaded_files:
        for path in sorted(target_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}:
                downloaded_files.append(str(path))

    if not downloaded_files:
        raise HTTPException(status_code=422, detail="没有下载到任何视频文件")

    assets: list[dict] = []
    for position, file_path in enumerate(downloaded_files):
        path = Path(file_path)
        if not path.exists():
            continue
        file_size = path.stat().st_size
        if file_size <= 0 or file_size > settings.max_upload_bytes:
            continue
        with path.open("rb") as f:
            sha256 = hashlib.sha256()
            for chunk in iter(lambda: f.read(1 * 1024 * 1024), b""):
                sha256.update(chunk)
        total_size += file_size
        if total_size > settings.max_import_total_bytes:
            raise HTTPException(status_code=413, detail="作品媒体文件总大小超过导入限制")

        width, height, duration = inspect_media(path, "video")
        mime = "video/mp4" if path.suffix.lower() in {".mp4", ""} else f"video/{path.suffix.lstrip('.')}"
        assets.append({
            "original_name": path.name[:255],
            "storage_name": path.relative_to(settings.upload_dir).as_posix(),
            "media_type": "video",
            "mime_type": mime,
            "file_size": file_size,
            "checksum": sha256.hexdigest(),
            "width": width,
            "height": height,
            "duration_seconds": duration,
            "position": position,
        })

    report({
        "post_name": "抖音作品",
        "image_downloaded": 1,
        "image_total": 1,
    })
    return assets


def _extract_cookies(session) -> str | None:
    """从浏览器上下文提取 Netscape cookie 格式。"""
    try:
        context = session._context  # 使用内部属性获取 context
        cookies = context.cookies()
        if not cookies:
            return None

        lines = ["# Netscape HTTP Cookie File", "# This is a generated file!  Do not edit."]
        for cookie in cookies:
            try:
                domain = cookie.get("domain", "")
                name = cookie.get("name", "")
                value = cookie.get("value", "")
                path = cookie.get("path", "/")
                secure = "TRUE" if cookie.get("secure") else "FALSE"
                expiry = str(int(cookie.get("expires", -1)))
                lines.append(f"{domain}\tTRUE\t{path}\t{secure}\t{expiry}\t{name}\t{value}")
            except Exception:
                continue
        return "\n".join(lines) + "\n"
    except Exception:
        return None


async def import_public_douyin(
    source_url: str,
    post_id: str,
    progress_callback: Callable[[dict], None] | None = None,
) -> tuple[str, DouyinPost, list[dict]]:
    """异步包装器——在工作线程中运行同步的浏览器下载逻辑。"""
    import asyncio
    return await asyncio.to_thread(
        _download_with_browser,
        source_url,
        post_id,
        progress_callback,
    )