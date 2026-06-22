from __future__ import annotations

from abc import ABC
import re
import threading
import time
from pathlib import Path

from .base import ContentCallback, PublicationCancelled, PublishSnapshot, StatusCallback
from ..config import settings


PUBLISH_URLS = {
    "douyin": "https://creator.douyin.com/creator-micro/content/upload",
    "xiaohongshu": "https://creator.xiaohongshu.com/publish/publish",
    "bilibili_video": "https://member.bilibili.com/platform/upload/video/frame",
    "bilibili_dynamic": "https://t.bilibili.com/",
}
PLATFORM_BROWSER_LOCKS = {
    platform: threading.Lock()
    for platform in ("douyin", "xiaohongshu", "bilibili", "wechat_moments")
}
VISIBILITY_LABELS = {
    "public": ("公开可见", "所有人可见", "公开"),
    "friends": ("仅互关好友可见", "互关好友可见", "好友可见", "仅好友可见"),
    "private": ("仅自己可见", "私密", "仅我可见"),
}
VISIBILITY_NAMES = {"public": "公开可见", "friends": "仅互关好友可见", "private": "仅自己可见"}


def _browser_executable() -> Path | None:
    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path.home() / r"AppData\Local\Microsoft\Edge\Application\msedge.exe",
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    return next((path for path in candidates if path.is_file()), None)


class BrowserPublisher(ABC):
    platform = ""
    image_limit = 1
    title_limit = 300

    title_selectors = (
        "input[placeholder*='标题']",
        "input[placeholder*='作品名称']",
    )
    body_selectors = (
        "textarea[placeholder*='描述']",
        "textarea[placeholder*='正文']",
        "div[contenteditable='true']",
    )
    publish_names = (r"发布", r"立即发布", r"确认发布")

    def validate(self, snapshot: PublishSnapshot) -> list[dict]:
        issues: list[dict] = []
        images = [asset for asset in snapshot.assets if asset.media_type == "image"]
        videos = [asset for asset in snapshot.assets if asset.media_type == "video"]
        if not snapshot.assets:
            issues.append(self._issue("error", "media_required", "至少选择一个发布素材"))
        if images and videos:
            issues.append(self._issue("error", "mixed_media", "单次发布不能混合图片和视频"))
        if len(videos) > 1:
            issues.append(self._issue("error", "video_count", "单次只能发布一个视频"))
        if len(images) > self.image_limit:
            issues.append(self._issue(
                "error", "image_count", f"{self.display_name}单次最多选择 {self.image_limit} 张图片"
            ))
        if len(snapshot.title) > self.title_limit:
            issues.append(self._issue(
                "error", "title_length", f"标题超过 {self.display_name}的 {self.title_limit} 字建议上限"
            ))
        if not snapshot.title.strip() and not snapshot.body.strip():
            issues.append(self._issue("error", "copy_required", "标题和正文不能同时为空"))
        for asset in snapshot.assets:
            if not asset.path.is_file():
                issues.append(self._issue("error", "file_missing", f"素材文件不存在：{asset.path.name}"))
            elif asset.file_size <= 0:
                issues.append(self._issue("error", "file_empty", f"素材文件为空：{asset.path.name}"))
        return issues

    @property
    def display_name(self) -> str:
        return {"douyin": "抖音", "xiaohongshu": "小红书", "bilibili": "B站"}[self.platform]

    @staticmethod
    def _issue(level: str, code: str, message: str) -> dict:
        return {"level": level, "code": code, "message": message}

    def publish_url(self, snapshot: PublishSnapshot) -> str:
        return PUBLISH_URLS[self.platform]

    def execute(
        self,
        snapshot: PublishSnapshot,
        confirm_event: threading.Event,
        cancel_event: threading.Event,
        on_status: StatusCallback,
        on_content: ContentCallback,
    ) -> dict:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("缺少 Playwright，请执行 python -m pip install -r requirements.txt") from exc

        executable = _browser_executable()
        if executable is None:
            raise RuntimeError("未找到 Microsoft Edge 或 Google Chrome")
        profile_dir = settings.browser_profile_dir / self.platform
        profile_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                executable_path=str(executable),
                headless=False,
                no_viewport=True,
                accept_downloads=False,
                args=["--start-maximized"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                on_status("awaiting_login", f"请在打开的{self.display_name}窗口中完成登录")
                page.goto(self.publish_url(snapshot), wait_until="domcontentloaded", timeout=90_000)
                file_input = self._wait_for_file_input(page, snapshot, cancel_event)
                self._check_cancelled(cancel_event)
                file_input.set_input_files([str(asset.path) for asset in snapshot.assets])
                on_status("preparing", "素材已交给平台上传，正在填写标题和正文")
                self._wait_and_fill_metadata(page, snapshot, cancel_event)
                visibility_applied = self._wait_and_apply_visibility(
                    page, snapshot.visibility, cancel_event
                )
                visibility_name = VISIBILITY_NAMES[snapshot.visibility]
                visibility_message = (
                    f"已设置为{visibility_name}"
                    if visibility_applied
                    else f"未能自动确认“{visibility_name}”，请务必在平台窗口手动选择"
                )
                on_status(
                    "review_pending",
                    f"{visibility_message}；请检查封面、分区等选项，然后回到 Content Hub 确认发布",
                )
                review_url = page.url
                last_content = (snapshot.title, snapshot.body)
                while True:
                    self._check_cancelled(cancel_event)
                    if page.is_closed():
                        raise RuntimeError("平台发布窗口已关闭")
                    current_content = self._read_metadata(page, snapshot)
                    if current_content and current_content != last_content:
                        on_content(*current_content)
                        last_content = current_content
                    manual_result = self._detect_result(page, review_url)
                    if manual_result:
                        manual_result["manual"] = True
                        return manual_result
                    if confirm_event.wait(0.5):
                        break

                self._check_cancelled(cancel_event)
                on_status("publishing", "正在向平台提交作品")
                button = self._find_publish_button(page)
                if button is None:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        manual_result = self._detect_result(page, review_url)
                        if manual_result:
                            manual_result["manual"] = True
                            return manual_result
                        page.wait_for_timeout(500)
                    raise RuntimeError("未找到可用的发布按钮，请确认平台必填项已经补全")
                button.click(timeout=15_000)
                self._click_secondary_confirmation(page)
                return self._wait_for_result(page)
            finally:
                context.close()

    def _wait_for_file_input(self, page, snapshot, cancel_event: threading.Event):
        deadline = time.monotonic() + 15 * 60
        while time.monotonic() < deadline:
            self._check_cancelled(cancel_event)
            self._choose_upload_mode(page, snapshot)
            inputs = page.locator("input[type='file']")
            for index in range(inputs.count()):
                item = inputs.nth(index)
                accept = (item.get_attribute("accept") or "").lower()
                media_type = snapshot.assets[0].media_type
                if media_type == "image" and "video" in accept and "image" not in accept:
                    continue
                if media_type == "video" and "image" in accept and "video" not in accept:
                    continue
                return item
            page.wait_for_timeout(1000)
        raise RuntimeError(f"等待{self.display_name}登录或上传入口超时")

    def _choose_upload_mode(self, page, snapshot: PublishSnapshot) -> None:
        return None

    def _wait_and_fill_metadata(self, page, snapshot: PublishSnapshot, cancel_event: threading.Event) -> None:
        deadline = time.monotonic() + 8 * 60
        while time.monotonic() < deadline:
            self._check_cancelled(cancel_event)
            filled = self._fill_metadata(page, snapshot)
            if filled:
                return
            page.wait_for_timeout(1000)
        raise RuntimeError("素材上传后未找到标题或正文编辑框，平台页面结构可能已变化")

    def _fill_metadata(self, page, snapshot: PublishSnapshot) -> bool:
        title_done = not snapshot.title.strip() or self._fill_first(page, self.title_selectors, snapshot.title)
        body_done = not snapshot.body.strip() or self._fill_first(page, self.body_selectors, snapshot.body)
        return title_done and body_done

    def _read_metadata(self, page, snapshot: PublishSnapshot) -> tuple[str, str] | None:
        title = self._read_first(page, self.title_selectors)
        body = self._read_first(page, self.body_selectors)
        if title is None and body is None:
            return None
        return (
            snapshot.title if title is None else title.strip(),
            snapshot.body if body is None else body.strip(),
        )

    @staticmethod
    def _read_first(page, selectors: tuple[str, ...]) -> str | None:
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 5)):
                item = locator.nth(index)
                try:
                    if not item.is_visible():
                        continue
                    try:
                        return item.input_value(timeout=1500)
                    except Exception:
                        return item.inner_text(timeout=1500)
                except Exception:
                    continue
        return None

    @staticmethod
    def _apply_visibility(page, visibility: str) -> bool:
        desired = VISIBILITY_LABELS.get(visibility, ())
        triggers = ("谁可以看", "可见范围", "观看权限", "发布范围", "公开可见")

        trigger_clicked = False
        for trigger in triggers:
            locator = page.get_by_text(trigger, exact=True)
            for index in range(min(locator.count(), 4)):
                try:
                    item = locator.nth(index)
                    if item.is_visible():
                        item.click(timeout=1500)
                        page.wait_for_timeout(400)
                        trigger_clicked = True
                        break
                except Exception:
                    continue
            if trigger_clicked:
                break

        for label in desired:
            locator = page.get_by_text(label, exact=True)
            for index in range(min(locator.count(), 6)):
                try:
                    item = locator.nth(index)
                    if item.is_visible():
                        item.click(timeout=2000)
                        return True
                except Exception:
                    continue
            radio = page.locator(
                f"label:has-text('{label}'), [role='radio']:has-text('{label}'), [role='option']:has-text('{label}')"
            )
            for index in range(min(radio.count(), 4)):
                try:
                    item = radio.nth(index)
                    if item.is_visible():
                        item.click(timeout=2000)
                        return True
                except Exception:
                    continue
        return False

    def _wait_and_apply_visibility(
        self,
        page,
        visibility: str,
        cancel_event: threading.Event,
    ) -> bool:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self._check_cancelled(cancel_event)
            if self._apply_visibility(page, visibility):
                return True
            page.wait_for_timeout(500)
        return False

    @staticmethod
    def _fill_first(page, selectors: tuple[str, ...], value: str) -> bool:
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 5)):
                item = locator.nth(index)
                try:
                    if item.is_visible():
                        item.fill(value, timeout=3000)
                        return True
                except Exception:
                    continue
        return False

    def _find_publish_button(self, page):
        for name in self.publish_names:
            locator = page.get_by_role("button", name=re.compile(rf"^{name}$"))
            for index in range(locator.count()):
                item = locator.nth(index)
                try:
                    if item.is_visible() and item.is_enabled():
                        return item
                except Exception:
                    continue
        return None

    @staticmethod
    def _click_secondary_confirmation(page) -> None:
        page.wait_for_timeout(800)
        for text in ("确认发布", "确认投稿", "仍要发布"):
            locator = page.get_by_role("button", name=re.compile(rf"^{text}$"))
            if locator.count():
                try:
                    if locator.first.is_visible() and locator.first.is_enabled():
                        locator.first.click(timeout=3000)
                        return
                except Exception:
                    pass

    @staticmethod
    def _wait_for_result(page) -> dict:
        starting_url = page.url
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if page.is_closed():
                break
            try:
                result = BrowserPublisher._detect_result(page, starting_url)
                if result:
                    return result
                page.wait_for_timeout(1000)
            except Exception:
                break
        return BrowserPublisher._result("submitted", page.url if not page.is_closed() else starting_url)

    @staticmethod
    def _detect_result(page, starting_url: str) -> dict | None:
        if page.is_closed():
            return None
        success_pattern = re.compile(r"发布成功|投稿成功|提交成功|已提交审核|审核中")
        try:
            success = page.get_by_text(success_pattern)
            if success.count() and any(
                success.nth(index).is_visible() for index in range(min(success.count(), 5))
            ):
                return BrowserPublisher._result("published", page.url)
            if page.url != starting_url and not any(
                part in page.url.casefold() for part in ("/upload", "/publish")
            ):
                return BrowserPublisher._result("published", page.url)
        except Exception:
            return None
        return None

    @staticmethod
    def _result(status: str, url: str) -> dict:
        matches = re.findall(r"(?<!\d)(\d{6,})(?!\d)", url or "")
        return {
            "status": status,
            "platform_url": url or None,
            "platform_item_id": matches[-1] if matches else None,
        }

    @staticmethod
    def _check_cancelled(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise PublicationCancelled("发布任务已取消")


class DouyinPublisher(BrowserPublisher):
    platform = "douyin"
    image_limit = 30
    title_limit = 55
    title_selectors = (
        "input[placeholder*='作品标题']",
        "input[placeholder*='标题']",
        "input[maxlength='55']",
    )
    body_selectors = (
        "textarea[placeholder*='作品描述']",
        "textarea[placeholder*='描述']",
        "div[contenteditable='true']",
    )


class XiaohongshuPublisher(BrowserPublisher):
    platform = "xiaohongshu"
    image_limit = 18
    title_limit = 20
    title_selectors = (
        "input[placeholder*='填写标题']",
        "input[placeholder*='标题']",
    )
    body_selectors = (
        "div[contenteditable='true']",
        ".ql-editor",
        "textarea[placeholder*='正文']",
    )

    def _choose_upload_mode(self, page, snapshot: PublishSnapshot) -> None:
        labels = ("上传图文", "图文") if snapshot.assets[0].media_type == "image" else ("上传视频", "视频")
        for label in labels:
            locator = page.get_by_text(label, exact=True)
            if locator.count():
                try:
                    if locator.first.is_visible():
                        locator.first.click(timeout=1000)
                        return
                except Exception:
                    pass


class BilibiliPublisher(BrowserPublisher):
    platform = "bilibili"
    image_limit = 9
    title_limit = 80
    title_selectors = (
        "input[placeholder*='稿件标题']",
        "input[placeholder*='标题']",
        "input[maxlength='80']",
    )
    body_selectors = (
        "textarea[placeholder*='简介']",
        "div[contenteditable='true']",
        "textarea",
    )
    publish_names = (r"立即投稿", r"发布", r"立即发布")

    def publish_url(self, snapshot: PublishSnapshot) -> str:
        return PUBLISH_URLS[
            "bilibili_video" if snapshot.assets[0].media_type == "video" else "bilibili_dynamic"
        ]

    def _choose_upload_mode(self, page, snapshot: PublishSnapshot) -> None:
        if snapshot.assets[0].media_type != "image":
            return
        for label in ("发布动态", "图片", "图文"):
            locator = page.get_by_text(label, exact=True)
            if locator.count():
                try:
                    if locator.first.is_visible():
                        locator.first.click(timeout=1000)
                except Exception:
                    pass

    def _fill_metadata(self, page, snapshot: PublishSnapshot) -> bool:
        if snapshot.assets[0].media_type == "video":
            return super()._fill_metadata(page, snapshot)
        combined = "\n\n".join(part for part in (snapshot.title.strip(), snapshot.body.strip()) if part)
        return self._fill_first(page, self.body_selectors, combined)

    def _read_metadata(self, page, snapshot: PublishSnapshot) -> tuple[str, str] | None:
        if snapshot.assets[0].media_type == "video":
            return super()._read_metadata(page, snapshot)
        combined = self._read_first(page, self.body_selectors)
        if combined is None:
            return None
        combined = combined.strip()
        prefix = snapshot.title.strip()
        if prefix and combined.startswith(prefix):
            return prefix, combined[len(prefix):].lstrip()
        return snapshot.title, combined


PUBLISHERS = {
    "douyin": DouyinPublisher,
    "xiaohongshu": XiaohongshuPublisher,
    "bilibili": BilibiliPublisher,
}


def get_publisher(platform: str) -> BrowserPublisher:
    try:
        return PUBLISHERS[platform]()
    except KeyError as exc:
        raise ValueError(f"暂不支持发布到 {platform}") from exc
