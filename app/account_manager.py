from __future__ import annotations

from datetime import datetime, timezone
import threading
import time

from .config import settings
from .publishers.browser import (
    PLATFORM_BROWSER_LOCKS,
    PUBLISH_URLS,
    _browser_executable,
    browser_context_options,
)


ACCOUNT_PLATFORMS = ("douyin", "xiaohongshu", "bilibili")
PLATFORM_NAMES = {"douyin": "抖音", "xiaohongshu": "小红书", "bilibili": "B站"}
ACCOUNT_URLS = {
    "douyin": PUBLISH_URLS["douyin"],
    "xiaohongshu": PUBLISH_URLS["xiaohongshu"],
    "bilibili": PUBLISH_URLS["bilibili_video"],
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AccountManager:
    """Checks and establishes login state inside Content Hub's dedicated browser profiles."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, threading.Thread] = {}
        self._states = {
            platform: {
                "platform": platform,
                "name": PLATFORM_NAMES[platform],
                "status": "unknown",
                "message": "尚未检测",
                "last_checked_at": None,
            }
            for platform in ACCOUNT_PLATFORMS
        }

    def list_statuses(self) -> list[dict]:
        with self._lock:
            return [dict(self._states[platform]) for platform in ACCOUNT_PLATFORMS]

    def check_all(self) -> None:
        for platform in ACCOUNT_PLATFORMS:
            self.start(platform, visible=False)

    def start(self, platform: str, *, visible: bool) -> bool:
        if platform not in ACCOUNT_PLATFORMS:
            raise ValueError("暂不支持该平台账号")
        with self._lock:
            task = self._tasks.get(platform)
            if task and task.is_alive():
                return False
            thread = threading.Thread(
                target=self._run,
                args=(platform, visible),
                name=f"account-{platform}",
                daemon=True,
            )
            self._tasks[platform] = thread
            self._set_state(
                platform,
                "awaiting_login" if visible else "checking",
                "请在打开的浏览器中完成登录" if visible else "正在检测本机登录状态",
            )
            thread.start()
            return True

    def _run(self, platform: str, visible: bool) -> None:
        platform_lock = PLATFORM_BROWSER_LOCKS[platform]
        acquired = False
        try:
            acquired = platform_lock.acquire(blocking=False)
            if not acquired:
                self._set_state(platform, "busy", "该平台正在执行发布任务，请稍后检测")
                return
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise RuntimeError("缺少 Playwright，请先安装 playwright") from exc

            executable = _browser_executable()
            if executable is None:
                raise RuntimeError("未找到 Microsoft Edge 或 Google Chrome")
            profile_dir = settings.browser_profile_dir / platform
            profile_dir.mkdir(parents=True, exist_ok=True)

            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    executable_path=str(executable),
                    **browser_context_options(visible=visible, accept_downloads=False),
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page_closed = threading.Event()
                    page.on("close", lambda: page_closed.set())
                    page.goto(ACCOUNT_URLS[platform], wait_until="load", timeout=90_000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=15_000)
                    except Exception:
                        pass
                    timeout = 15 * 60 if visible else 25
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        if page_closed.is_set() or page.is_closed():
                            self._set_state(platform, "not_logged_in", "登录窗口已关闭", checked=True)
                            return
                        pages = [item for item in context.pages if not item.is_closed()]
                        if not pages:
                            self._set_state(platform, "not_logged_in", "登录窗口已关闭")
                            return
                        page = pages[-1]
                        if self._is_authenticated(page, platform):
                            self._set_state(platform, "logged_in", "已登录，可直接发布", checked=True)
                            return
                        page.wait_for_timeout(1000)
                    message = "登录等待超时，请重新打开登录窗口" if visible else "当前浏览器尚未登录"
                    self._set_state(platform, "not_logged_in", message, checked=True)
                finally:
                    try:
                        context.close()
                    except Exception:
                        # Closing the window manually can disconnect Playwright first.
                        # The account task still needs to release the shared platform lock.
                        pass
        except Exception as exc:
            self._set_state(platform, "error", str(exc).strip() or exc.__class__.__name__, checked=True)
        finally:
            if acquired:
                platform_lock.release()
            with self._lock:
                self._tasks.pop(platform, None)

    @staticmethod
    def _is_authenticated(page, platform: str) -> bool:
        try:
            url = page.url.casefold()
            if any(token in url for token in ("login", "signin", "passport")):
                return False
            if page.locator("input[type='file']").count():
                return True
            markers = {
                "douyin": ("投稿管理", "作品管理", "发布作品"),
                "xiaohongshu": ("笔记管理", "发布笔记", "创作服务"),
                "bilibili": ("稿件管理", "创作中心", "内容管理"),
            }[platform]
            for marker in markers:
                locator = page.get_by_text(marker, exact=True)
                if locator.count() and any(
                    locator.nth(index).is_visible() for index in range(min(locator.count(), 3))
                ):
                    body = page.locator("body").inner_text(timeout=2000)
                    return not any(text in body for text in ("扫码登录", "登录后", "请登录"))
        except Exception:
            return False
        return False

    def _set_state(
        self,
        platform: str,
        status: str,
        message: str,
        *,
        checked: bool = False,
    ) -> None:
        with self._lock:
            state = self._states[platform]
            state["status"] = status
            state["message"] = message[:500]
            if checked:
                state["last_checked_at"] = _iso_now()


account_manager = AccountManager()
