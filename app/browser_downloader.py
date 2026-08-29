from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import hashlib
import random
import threading
import time
from urllib.parse import urlparse

from fastapi import HTTPException

from .config import settings
from .publishers.browser import (
    ANTI_DETECTION_INIT_SCRIPT,
    RequestHeaderTracker,
    browser_context_options,
    detect_browser_type,
    get_browser_executable,
)
from .publishers.fingerprint_config import FingerprintConfig


# -----------------------------------------------------------------------------
# 通用数据结构
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class DownloadedAsset:
    """下载后的素材信息（与现有 MediaAsset 字段对齐）。"""
    original_name: str
    storage_name: str
    media_type: str
    mime_type: str
    file_size: int
    checksum: str
    width: int | None
    height: int | None
    duration_seconds: float | None
    position: int


@dataclass(frozen=True)
class DownloadedPage:
    """下载页面解析结果：规范化 URL + HTML 文本。"""
    canonical_url: str
    html: str


# -----------------------------------------------------------------------------
# 浏览器下载会话
# -----------------------------------------------------------------------------

class BrowserDownloadSession:
    """基于 Playwright 的浏览器级下载会话。

    职责：
    1. 启动真实浏览器（复用发布系统已有的反检测参数）
    2. 注入反检测脚本 + 随机设备指纹
    3. 通过 RequestHeaderTracker 动态维护 Referer / 请求头顺序
    4. 通过 `context.request` API 执行页面抓取 + 媒体下载

    设计要点：
    - 与 BrowserPublisher（发布系统）共享完全一致的反检测策略
    - 使用 *单个浏览器上下文* 完成一次导入（单条作品）
    - 每个会话包含独立的 RequestHeaderTracker 与 FingerprintConfig
    - 支持通过 `progress_callback` 汇报下载进度
    """

    def __init__(
        self,
        *,
        browser_type: str | None = None,
        profile_subdir: str | None = None,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> None:
        self._progress_callback = progress_callback
        self._lock = threading.Lock()
        self._browser_type = browser_type
        # 为当前会话生成唯一设备指纹
        self._fingerprint = FingerprintConfig.random()
        # 请求头追踪器：与发布系统使用同一份逻辑
        resolved_browser_type = browser_type or "chrome"
        self._header_tracker = RequestHeaderTracker(browser=resolved_browser_type)
        # 可选的 cookies/登录态目录（与发布系统共享相同的用户配置）
        self._profile_dir = None
        if profile_subdir:
            self._profile_dir = settings.browser_profile_dir / profile_subdir
            self._profile_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------------------
    # 上下文管理
    # -------------------------------------------------------------------------

    def __enter__(self) -> "BrowserDownloadSession":
        # 延迟导入以避免在非下载场景下启动 Playwright
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        executable = get_browser_executable(self._browser_type)
        options = browser_context_options(
            visible=False,
            accept_downloads=True,
            browser_type=self._browser_type or detect_browser_type(executable),
        )
        if executable is not None:
            options["executable_path"] = str(executable)
        if self._profile_dir and self._profile_dir.is_dir():
            # 持久化上下文可以复用登录态，但为了反检测，我们在单次会话中
            # 使用临时上下文以避免 cookie 与自动化签名关联过多。
            context = self._pw.chromium.launch_persistent_context(
                str(self._profile_dir), **options
            )
        else:
            browser = self._pw.chromium.launch(**options)
            context = browser.new_context(**{
                k: v for k, v in options.items()
                if k in ("user_agent", "timezone_id")
            })

        # 注入浏览器端反检测脚本（与发布系统完全一致）
        if ANTI_DETECTION_INIT_SCRIPT:
            try:
                context.add_init_script(ANTI_DETECTION_INIT_SCRIPT)
            except Exception:
                pass

        # 记录请求历史以便后续 Referer 使用
        context.on("request", self._on_request)
        self._context = context
        self._page = context.new_page()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            # 等待页面请求结束后再关闭上下文
            time.sleep(0.4)
            self._context.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # 内部事件处理
    # -------------------------------------------------------------------------

    def _on_request(self, request) -> None:
        """把真实的浏览器请求记录到 tracker，以便后续 Referer 生成。"""
        try:
            if request.resource_type == "document":
                self._header_tracker.navigate_to(request.url)
        except Exception:
            pass

    def _report(self, payload: dict) -> None:
        cb = self._progress_callback
        if cb is not None:
            try:
                cb(payload)
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # 页面获取
    # -------------------------------------------------------------------------

    def fetch_page(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        timeout_ms: int = 30_000,
    ) -> DownloadedPage:
        """通过真实浏览器访问页面，返回规范化 URL 与 HTML。

        与发布系统使用相同的行为：
        - 使用 page.goto() 加载（可被平台正常解析为"用户访问"）
        - 通过 RequestHeaderTracker 自动跟踪页面导航作为后续下载的 Referer
        """
        from playwright.sync_api import Error as PlaywrightError

        try:
            response = self._page.goto(
                url,
                wait_until=wait_until,
                timeout=timeout_ms,
            )
        except PlaywrightError as exc:
            # 超时 / 网络错误
            raise HTTPException(
                status_code=502,
                detail=f"页面打开失败：{str(exc).splitlines()[0] if str(exc) else str(exc)}",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"加载页面时发生未知错误：{type(exc).__name__}",
            ) from exc

        canonical_url = self._page.url

        # 检查返回状态码
        if response is not None:
            status = response.status
            if status in {401, 403, 429}:
                raise HTTPException(
                    status_code=status,
                    detail=(
                        f"平台返回状态 {status}，可能需要登录、验证码"
                        "或请求过于频繁，已暂停导入"
                    ),
                )
            if status >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"页面返回状态 {status}，作品可能需要登录或已不可见",
                )

        # 记录到请求追踪器
        self._header_tracker.navigate_to(canonical_url)

        html = self._page.content()
        if not html:
            raise HTTPException(
                status_code=422,
                detail="页面返回内容为空，可能被平台拦截",
            )

        return DownloadedPage(canonical_url=canonical_url, html=html)

    # -------------------------------------------------------------------------
    # 媒体下载
    # -------------------------------------------------------------------------

    def download_media(
        self,
        media_url: str,
        *,
        target_dir: Path,
        filename: str,
        media_type: str,
        allowed_hosts: tuple[str, ...],
        referer: str | None = None,
        max_bytes: int | None = None,
        extra_wait_seconds: float = 0.3,
    ) -> DownloadedAsset:
        """使用浏览器的 request API 下载单个媒体文件。

        相比 httpx 的差异：
        1. 自动携带浏览器上下文的 cookie（与页面使用一致）
        2. 使用与页面请求相同的 TLS/HTTP 栈，避免"脚本请求 vs 浏览器请求"的指纹差异
        3. 通过 RequestHeaderTracker 动态设置 Referer，而不是硬编码
        """
        from playwright.sync_api import APIResponse
        from .assets import inspect_media

        if max_bytes is None:
            max_bytes = settings.max_upload_bytes

        last_exc: Exception | None = None
        for attempt in range(1, settings.import_media_retry_attempts + 1):
            try:
                return self._download_media_once(
                    media_url=media_url,
                    target_dir=target_dir,
                    filename=filename,
                    media_type=media_type,
                    allowed_hosts=allowed_hosts,
                    referer=referer or self._header_tracker.referer,
                    max_bytes=max_bytes,
                )
            except HTTPException as exc:
                last_exc = exc
                # 暂停信号：直接向上抛出
                if _should_pause_download(exc):
                    raise
                # 不重试的错误直接抛出
                if not _should_retry_download(exc) or attempt >= settings.import_media_retry_attempts:
                    raise
                # 渐退重试，模拟用户卡顿
                wait = min(
                    8.0,
                    0.8 * attempt + random.uniform(0.2, 0.8) + extra_wait_seconds,
                )
                time.sleep(wait)
            except Exception as exc:  # 意外网络错误
                last_exc = exc
                if attempt >= settings.import_media_retry_attempts:
                    raise HTTPException(status_code=502, detail=f"媒体下载失败：{type(exc).__name__}") from exc
                time.sleep(min(8.0, 0.8 * attempt + random.uniform(0.2, 0.8)))
        if last_exc:
            raise last_exc
        raise HTTPException(status_code=502, detail="媒体下载失败")

    def _download_media_once(
        self,
        *,
        media_url: str,
        target_dir: Path,
        filename: str,
        media_type: str,
        allowed_hosts: tuple[str, ...],
        referer: str | None,
        max_bytes: int,
    ) -> DownloadedAsset:
        from playwright.sync_api import APIResponse
        from .assets import inspect_media

        host = urlparse(media_url).hostname
        if not host or not _host_allowed(host, allowed_hosts):
            raise HTTPException(status_code=422, detail="媒体地址不在信任域名列表")

        # 用与页面一致的浏览器栈发起请求
        headers = self._header_tracker.build_ordered_headers(
            user_agent=self._fingerprint.platform and None,  # 使用浏览器自带 UA
        )
        # dict化 headers（Playwright API 需要 dict）
        headers_dict = dict(headers)
        if referer and "Referer" not in headers_dict:
            headers_dict["Referer"] = referer

        try:
            response: APIResponse = self._context.request.get(
                media_url,
                headers=headers_dict,
                timeout=60_000,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail="媒体下载请求失败") from exc

        status = response.status
        if status in {401, 403, 429}:
            raise HTTPException(
                status_code=status,
                detail=f"媒体下载返回状态 {status}，可能触发限流或验证",
            )
        if status >= 400:
            raise HTTPException(
                status_code=502 if status >= 500 else 422,
                detail=f"媒体下载返回状态 {status}",
            )

        # 读取响应体并流式写入
        try:
            body = response.body()
        except Exception as exc:
            raise HTTPException(status_code=502, detail="媒体响应读取失败") from exc

        content_type = response.headers.get("content-type", "application/octet-stream")
        if content_type and content_type.startswith("text/"):
            raise HTTPException(status_code=422, detail="媒体下载返回的内容类型无效")

        file_size = len(body)
        if file_size <= 0:
            raise HTTPException(status_code=422, detail="媒体文件为空")
        if file_size > max_bytes:
            raise HTTPException(status_code=413, detail="媒体文件过大")

        suffix = _extension_from_mime(content_type, media_url, media_type)
        if not filename.endswith(suffix):
            filename = filename + suffix

        target = target_dir / filename
        if not target.parent.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as output:
            output.write(body)

        digest = hashlib.sha256(body).hexdigest()
        width, height, duration = inspect_media(target, media_type)

        return DownloadedAsset(
            original_name=filename[:255],
            storage_name="",  # 由调用方设置
            media_type=media_type,
            mime_type=content_type.split(";", 1)[0],
            file_size=file_size,
            checksum=digest,
            width=width,
            height=height,
            duration_seconds=duration,
            position=0,
        )

    # -------------------------------------------------------------------------
    # 会话辅助
    # -------------------------------------------------------------------------

    @property
    def header_tracker(self) -> RequestHeaderTracker:
        return self._header_tracker

    @property
    def fingerprint(self) -> FingerprintConfig:
        return self._fingerprint

    @property
    def current_url(self) -> str | None:
        try:
            return self._page.url
        except Exception:
            return None


# -----------------------------------------------------------------------------
# 辅助函数
# -----------------------------------------------------------------------------

def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in allowed
    )


def _extension_from_mime(content_type: str, url: str, media_type: str) -> str:
    normalized = content_type.split(";", 1)[0].lower()
    overrides = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
    }
    if normalized in overrides:
        return overrides[normalized]
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix and len(suffix) <= 6:
        return suffix
    return ".mp4" if media_type == "video" else ".jpg"


def _should_retry_download(exc: HTTPException) -> bool:
    return exc.status_code in {502, 503, 504}


def _should_pause_download(exc: HTTPException) -> bool:
    detail = str(exc.detail)
    return exc.status_code in {401, 403, 429} or any(
        token in detail
        for token in ("验证码", "安全验证", "登录", "限流", "过于频繁", "429", "暂停")
    )