from __future__ import annotations

from abc import ABC
import random
import re
import threading
import time
from pathlib import Path

from .base import ContentCallback, PublicationCancelled, PublishSnapshot, StatusCallback
from ..config import settings


_HERE = Path(__file__).resolve().parent


def _load_anti_detection_data() -> tuple[tuple[str, ...], str, str]:
    """从同目录的 anti_detection_args.txt 与 anti_detection.js 读取反检测数据。

    返回:
        (浏览器启动参数元组, 原始 JS init script 文本, 带指纹配置的 init script)
    解耦目标：启动参数（纯文本，一行一个，支持 # 注释）与 init script（独立
    JS 文件）都从 Python 源码中移除，便于用编辑器独立维护与审阅。
    文件缺失时返回空数据，保证代码路径仍能正常运行。
    """
    from .fingerprint_config import FingerprintConfig  # 延迟导入避免循环

    args_path = _HERE / "anti_detection_args.txt"
    js_path = _HERE / "anti_detection.js"
    try:
        args_lines: list[str] = []
        if args_path.is_file():
            for raw in args_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                args_lines.append(line)
        raw_init_script = ""
        if js_path.is_file():
            raw_init_script = js_path.read_text(encoding="utf-8")
        # 基于随机设备 profile 拼入 __fp_cfg__（包含屏幕/CPU/时区/字体等）
        cfg = FingerprintConfig.random()
        fingerprinted_init_script = cfg.build_init_script(raw_init_script)
    except Exception:
        return (), "", ""
    return tuple(args_lines), raw_init_script, fingerprinted_init_script


# 模块级只读引用：运行时从文件加载，避免 Python 源码里混合长字符串
ANTI_DETECTION_ARGS, _RAW_ANTI_DETECTION_INIT_SCRIPT, ANTI_DETECTION_INIT_SCRIPT = (
    _load_anti_detection_data()
)


PUBLISH_URLS = {
    "douyin": "https://creator.douyin.com/creator-micro/content/upload",
    "xiaohongshu": "https://creator.xiaohongshu.com/publish/publish",
    "bilibili_video": "https://member.bilibili.com/platform/upload/video/frame",
    "bilibili_dynamic": "https://t.bilibili.com/",
}
PLATFORM_BROWSER_LOCKS = {
    platform: threading.Lock()
    for platform in ("douyin", "xiaohongshu", "bilibili")
}
VISIBILITY_LABELS = {
    "public": ("公开可见", "所有人可见", "公开"),
    "friends": ("仅互关好友可见", "互关好友可见", "好友可见", "仅好友可见"),
    "private": ("仅自己可见", "私密", "仅我可见"),
}
VISIBILITY_NAMES = {"public": "公开可见", "friends": "仅互关好友可见", "private": "仅自己可见"}
INTERACTIVE_TOKEN_PATTERN = re.compile(
    r"(?<!\w)([#＃@＠][^\s#＃@＠,，。.!！?？;；:：]+)"
)
RISK_TEXT_PATTERN = re.compile(
    r"验证码|安全验证|身份验证|操作频繁|请求过于频繁|429|Too Many Requests|"
    r"登录异常|重新登录|扫码登录|登录后|请登录|账号异常|风控",
    re.IGNORECASE,
)
LOGIN_URL_PATTERN = re.compile(r"login|signin|passport|sso", re.IGNORECASE)
RISK_RESPONSE_STATUSES = {401, 403, 429}


def split_interactive_tokens(body: str) -> tuple[list[str], list[str]]:
    """Return unique hashtags and mentions without their marker."""
    hashtags: list[str] = []
    mentions: list[str] = []
    seen: set[tuple[str, str]] = set()
    for raw_token in INTERACTIVE_TOKEN_PATTERN.findall(body):
        marker = "#" if raw_token[0] in "#＃" else "@"
        value = raw_token[1:].strip()
        key = (marker, value.casefold())
        if not value or key in seen:
            continue
        seen.add(key)
        (hashtags if marker == "#" else mentions).append(value)
    return hashtags, mentions


def _browser_executable() -> Path | None:
    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path.home() / r"AppData\Local\Microsoft\Edge\Application\msedge.exe",
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    return next((path for path in candidates if path.is_file()), None)


def browser_context_options(*, visible: bool, accept_downloads: bool = False) -> dict:
    base_args = ["--start-maximized"] if visible else []
    # 去重：保持基础参数与反检测参数的顺序，使用 dict.fromkeys 去重
    extra_args: list[str] = []
    extra_args.extend(base_args)
    extra_args.extend(ANTI_DETECTION_ARGS)
    seen: dict[str, None] = dict.fromkeys(extra_args)
    final_args = list(seen.keys())
    options = {
        "headless": not visible,
        "no_viewport": visible,
        "accept_downloads": accept_downloads,
        "args": final_args,
        # 禁止 Playwright 默认注入 "--enable-automation"，这是最基础的反爬指纹
        "ignore_default_args": ["--enable-automation"],
    }
    if settings.browser_user_agent:
        options["user_agent"] = settings.browser_user_agent
    if settings.browser_timezone:
        options["timezone_id"] = settings.browser_timezone
    return options


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
        self._page_closed_event = threading.Event()
        self._risk_event = threading.Event()
        self._risk_message = ""
        self._active_cancel_event = cancel_event
        self._active_on_status = on_status
        self._active_page = None
        self._action_count = 0

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                executable_path=str(executable),
                **browser_context_options(visible=True, accept_downloads=False),
            )
            page = None
            try:
                page = context.pages[0] if context.pages else context.new_page()
                # 注入反指纹 init script（在页面脚本之前执行）
                if ANTI_DETECTION_INIT_SCRIPT:
                    try:
                        context.add_init_script(ANTI_DETECTION_INIT_SCRIPT)
                    except Exception:
                        # 旧版 Playwright 或上下文已关闭时，退化为在当前 page 注入
                        try:
                            page.add_init_script(ANTI_DETECTION_INIT_SCRIPT)
                        except Exception:
                            pass
                self._active_page = page
                page.on("close", lambda: self._page_closed_event.set())
                page.on("response", self._capture_risk_response)
                on_status("awaiting_login", f"请在打开的{self.display_name}窗口中完成登录")
                page.goto(self.publish_url(snapshot), wait_until="load", timeout=90_000)
                self._wait_for_full_load(page, cancel_event)
                file_input = self._wait_for_file_input(page, snapshot, cancel_event)
                self._check_runtime_pause(page, cancel_event)
                on_status("preparing", "已找到上传入口，上传前短暂停顿")
                self._human_pause(
                    page,
                    settings.publish_upload_delay_min_seconds,
                    settings.publish_upload_delay_max_seconds,
                    cancel_event,
                )
                file_input.set_input_files([str(asset.path) for asset in snapshot.assets])
                self._record_human_action(page, cancel_event)
                on_status("preparing", "素材已交给平台上传，上传后短暂停顿")
                self._human_pause(
                    page,
                    settings.publish_upload_delay_min_seconds,
                    settings.publish_upload_delay_max_seconds,
                    cancel_event,
                )
                on_status("preparing", "正在分步填写标题和正文")
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
                self._prepare_interactive_review(page, snapshot)
                on_status(
                    "review_pending",
                    self._review_message(snapshot, visibility_message),
                )
                review_url = page.url
                last_content = self._read_metadata(page, snapshot) or (
                    snapshot.title,
                    self._body_for_prefill(snapshot),
                )
                while True:
                    self._check_runtime_pause(page, cancel_event)
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

                self._check_runtime_pause(page, cancel_event)
                on_status("publishing", "正在向平台提交作品")
                button = self._find_publish_button(page)
                if button is None:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        self._check_runtime_pause(page, cancel_event)
                        manual_result = self._detect_result(page, review_url)
                        if manual_result:
                            manual_result["manual"] = True
                            return manual_result
                        page.wait_for_timeout(500)
                    raise RuntimeError("未找到可用的发布按钮，请确认平台必填项已经补全")
                self._human_pause(page, 0.8, 1.8, cancel_event)
                button.click(timeout=15_000)
                self._record_human_action(page, cancel_event)
                self._click_secondary_confirmation(page, cancel_event)
                return self._wait_for_result(page, cancel_event)
            except PublicationCancelled:
                raise
            except Exception as exc:
                if self._page_was_closed(page) or self._is_closed_target_error(exc):
                    raise PublicationCancelled(
                        f"{self.display_name}发布页已关闭，任务已取消，可以直接重试"
                    ) from exc
                raise
            finally:
                try:
                    context.close()
                except Exception:
                    # A manually closed browser can make Playwright's close call fail.
                    # The publication worker must still unwind and release the platform lock.
                    pass
                self._active_page = None
                self._active_cancel_event = None
                self._active_on_status = None

    def _wait_for_file_input(self, page, snapshot, cancel_event: threading.Event):
        deadline = time.monotonic() + 15 * 60
        while time.monotonic() < deadline:
            self._check_runtime_pause(page, cancel_event, allow_login=True)
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
            self._human_pause(page, 0.8, 1.2, cancel_event, allow_login=True)
        raise RuntimeError(f"等待{self.display_name}登录或上传入口超时")

    def _choose_upload_mode(self, page, snapshot: PublishSnapshot) -> None:
        return None

    def _wait_and_fill_metadata(self, page, snapshot: PublishSnapshot, cancel_event: threading.Event) -> None:
        deadline = time.monotonic() + 2 * 60
        left_editor_at: float | None = None
        while time.monotonic() < deadline:
            self._check_runtime_pause(page, cancel_event)
            if self.platform == "douyin" and not self._is_publish_editor_url(page.url):
                left_editor_at = left_editor_at or time.monotonic()
                if time.monotonic() - left_editor_at >= 2:
                    raise PublicationCancelled(
                        "已离开抖音发布页或取消素材上传，任务已取消，可以直接重试"
                    )
            else:
                left_editor_at = None
            filled = self._fill_metadata(page, snapshot)
            if filled:
                return
            self._human_pause(page, 0.8, 1.2, cancel_event)
        raise RuntimeError("素材上传后未找到标题或正文编辑框，平台页面结构可能已变化")

    def _fill_metadata(self, page, snapshot: PublishSnapshot) -> bool:
        title_done = not snapshot.title.strip() or self._fill_first(page, self.title_selectors, snapshot.title)
        body = self._body_for_prefill(snapshot)
        body_done = not body.strip() or self._fill_first(page, self.body_selectors, body)
        return title_done and body_done

    def _body_for_prefill(self, snapshot: PublishSnapshot) -> str:
        if self.platform not in {"douyin", "xiaohongshu"}:
            return snapshot.body
        body = INTERACTIVE_TOKEN_PATTERN.sub("", snapshot.body)
        lines = [re.sub(r"[ \t]{2,}", " ", line).strip() for line in body.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    @staticmethod
    def _interactive_tokens(body: str) -> list[str]:
        return list(dict.fromkeys(INTERACTIVE_TOKEN_PATTERN.findall(body)))

    def _review_message(self, snapshot: PublishSnapshot, visibility_message: str) -> str:
        tokens = self._interactive_tokens(snapshot.body)
        if self.platform in {"douyin", "xiaohongshu"} and tokens:
            token_text = " ".join(tokens[:12])
            token_hint = f"；请在官方页面手动输入并点击下拉候选：{token_text}"
        elif self.platform in {"douyin", "xiaohongshu"}:
            token_hint = "；如需 #tag 或 @用户，请在官方页面输入并点击下拉候选"
        else:
            token_hint = ""
        return f"{visibility_message}{token_hint}；请检查封面、分区等选项，然后回到 Content Hub 确认发布"

    def _prepare_interactive_review(self, page, snapshot: PublishSnapshot) -> None:
        return None

    def _read_metadata(self, page, snapshot: PublishSnapshot) -> tuple[str, str] | None:
        title = self._read_first(page, self.title_selectors)
        body = self._read_first(page, self.body_selectors)
        if title is None and body is None:
            return None
        return (
            snapshot.title if title is None else title.strip(),
            self._body_for_prefill(snapshot) if body is None else body.strip(),
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
            self._check_runtime_pause(page, cancel_event)
            if self._apply_visibility(page, visibility):
                self._record_human_action(page, cancel_event)
                return True
            self._human_pause(page, 0.4, 0.7, cancel_event)
        return False

    def _fill_first(self, page, selectors: tuple[str, ...], value: str) -> bool:
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 5)):
                item = locator.nth(index)
                try:
                    if item.is_visible():
                        self._type_into_field(page, item, value)
                        return True
                except RuntimeError:
                    raise
                except Exception:
                    continue
        return False

    def _type_into_field(self, page, item, value: str) -> None:
        cancel_event = getattr(self, "_active_cancel_event", None)
        self._check_runtime_pause(page, cancel_event)
        item.click(timeout=3000)
        item.press("Control+A", timeout=3000)
        item.press("Backspace", timeout=3000)
        for chunk in self._typing_chunks(value):
            self._check_runtime_pause(page, cancel_event)
            item.type(chunk, delay=random.randint(25, 70), timeout=max(3000, len(chunk) * 250))
            if not chunk.isspace():
                self._human_pause(
                    page,
                    settings.publish_typing_pause_min_seconds,
                    settings.publish_typing_pause_max_seconds,
                    cancel_event,
                )
        self._record_human_action(page, cancel_event)

    @staticmethod
    def _typing_chunks(value: str) -> list[str]:
        chunks: list[str] = []
        for part in re.findall(r"\s+|[^\s]+", value):
            if part.isspace():
                chunks.append(part)
                continue
            sentence_parts = re.findall(r"[^。！？.!?；;，,、]+[。！？.!?；;，,、]?", part) or [part]
            for sentence in sentence_parts:
                if len(sentence) <= 10:
                    chunks.append(sentence)
                    continue
                start = 0
                while start < len(sentence):
                    step = random.randint(5, 9)
                    chunks.append(sentence[start:start + step])
                    start += step
        return chunks

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

    def _click_secondary_confirmation(self, page, cancel_event: threading.Event) -> None:
        self._human_pause(page, 0.7, 1.1, cancel_event)
        for text in ("确认发布", "确认投稿", "仍要发布"):
            locator = page.get_by_role("button", name=re.compile(rf"^{text}$"))
            if locator.count():
                try:
                    if locator.first.is_visible() and locator.first.is_enabled():
                        locator.first.click(timeout=3000)
                        self._record_human_action(page, cancel_event)
                        return
                except RuntimeError:
                    raise
                except Exception:
                    pass

    def _wait_for_result(self, page, cancel_event: threading.Event) -> dict:
        starting_url = page.url
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            self._check_runtime_pause(page, cancel_event)
            if page.is_closed():
                break
            try:
                result = BrowserPublisher._detect_result(page, starting_url)
                if result:
                    return result
            except Exception:
                break
            self._human_pause(page, 0.8, 1.2, cancel_event)
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

    def _wait_for_full_load(self, page, cancel_event: threading.Event) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        self._check_runtime_pause(page, cancel_event, allow_login=True)

    def _capture_risk_response(self, response) -> None:
        try:
            status = response.status
            resource_type = response.request.resource_type
            if status == 429 or (status in RISK_RESPONSE_STATUSES and resource_type == "document"):
                self._set_risk(
                    f"平台返回 HTTP {status}（{resource_type}），可能触发限流、登录异常或安全验证"
                )
        except Exception:
            return

    def _set_risk(self, message: str) -> None:
        if getattr(self, "_risk_event", None) is None:
            return
        if not self._risk_event.is_set():
            self._risk_message = message
            self._risk_event.set()

    def _check_runtime_pause(
        self,
        page,
        cancel_event: threading.Event | None,
        *,
        allow_login: bool = False,
    ) -> None:
        if cancel_event is not None:
            self._check_cancelled(cancel_event)
        self._ensure_page_available(page)
        if getattr(self, "_risk_event", None) is not None and self._risk_event.is_set():
            raise RuntimeError(
                f"{self.display_name}发布已暂停：{self._risk_message or '检测到平台风控或登录异常'}，请人工处理后重试"
            )
        risk = self._detect_page_risk(page, allow_login=allow_login)
        if risk:
            self._set_risk(risk)
            raise RuntimeError(f"{self.display_name}发布已暂停：{risk}，请人工处理后重试")

    def _detect_page_risk(self, page, *, allow_login: bool) -> str | None:
        if page is None or self._page_was_closed(page):
            return None
        try:
            if not allow_login and LOGIN_URL_PATTERN.search(page.url or ""):
                return "平台页面跳转到登录页"
            body = page.locator("body")
            if not body.count():
                return None
            text = body.inner_text(timeout=1000)
        except Exception:
            return None
        match = RISK_TEXT_PATTERN.search(text[:20_000])
        if not match:
            return None
        token = match.group(0)
        if allow_login and token in {"扫码登录", "登录后", "请登录"}:
            return None
        return f"检测到页面提示“{token}”"

    def _human_pause(
        self,
        page,
        min_seconds: float,
        max_seconds: float,
        cancel_event: threading.Event | None,
        *,
        allow_login: bool = False,
    ) -> None:
        delay = random.uniform(min_seconds, max(min_seconds, max_seconds))
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            self._check_runtime_pause(page, cancel_event, allow_login=allow_login)
            remaining = max(0.0, deadline - time.monotonic())
            page.wait_for_timeout(max(50, int(min(0.25, remaining) * 1000)))

    def _record_human_action(self, page, cancel_event: threading.Event | None) -> None:
        self._action_count = getattr(self, "_action_count", 0) + 1
        threshold = settings.publish_rest_every_actions
        if threshold <= 0 or self._action_count < threshold:
            return
        self._action_count = 0
        on_status = getattr(self, "_active_on_status", None)
        if on_status:
            on_status("preparing", "连续操作后短暂休息，保持正常人工节奏")
        self._human_pause(
            page,
            settings.publish_rest_min_seconds,
            settings.publish_rest_max_seconds,
            cancel_event,
        )

    def _ensure_page_available(self, page) -> None:
        if self._page_was_closed(page):
            raise PublicationCancelled(
                f"{self.display_name}发布页已关闭，任务已取消，可以直接重试"
            )

    def _page_was_closed(self, page) -> bool:
        if getattr(self, "_page_closed_event", None) is not None:
            if self._page_closed_event.is_set():
                return True
        if page is None:
            return False
        try:
            return page.is_closed()
        except Exception:
            return True

    def _is_publish_editor_url(self, url: str) -> bool:
        normalized = (url or "").casefold()
        if self.platform == "douyin":
            return "creator.douyin.com" in normalized and any(
                path in normalized for path in ("/content/upload", "/content/post")
            )
        return True

    @staticmethod
    def _is_closed_target_error(exc: Exception) -> bool:
        message = str(exc).casefold()
        return any(token in message for token in (
            "target page, context or browser has been closed",
            "page has been closed",
            "browser has been closed",
            "target closed",
        ))


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
    topic_candidate_selectors = (
        "[role='listbox']:visible [role='option']:visible",
        ".semi-portal:visible .semi-select-option:visible",
        "[class*='suggest']:visible [class*='item']:visible",
        "[class*='topic']:visible [class*='item']:visible",
        "[class*='popover']:visible [class*='item']:visible",
        "[class*='dropdown']:visible [class*='item']:visible",
    )

    def __init__(self) -> None:
        self._hashtags_attempted = False
        self._bound_hashtags: list[str] = []
        self._unresolved_hashtags: list[str] = []
        self._opened_mention: str | None = None

    def _fill_metadata(self, page, snapshot: PublishSnapshot) -> bool:
        if not super()._fill_metadata(page, snapshot):
            return False
        hashtags, _mentions = split_interactive_tokens(snapshot.body)
        if not hashtags or self._hashtags_attempted:
            return True
        editor = self._find_visible(page, self.body_selectors)
        if editor is None:
            return False
        self._hashtags_attempted = True
        self._append_and_bind_hashtags(page, editor, hashtags, bool(self._body_for_prefill(snapshot)))
        return True

    def _append_and_bind_hashtags(
        self,
        page,
        editor,
        hashtags: list[str],
        has_plain_body: bool,
    ) -> None:
        cancel_event = getattr(self, "_active_cancel_event", None)
        self._check_runtime_pause(page, cancel_event)
        editor.click(timeout=3000)
        editor.press("Control+End")
        if has_plain_body:
            editor.press("Enter")
            editor.press("Enter")
        for index, hashtag in enumerate(hashtags):
            self._check_runtime_pause(page, cancel_event)
            if index:
                editor.type(" ")
            editor.type(f"#{hashtag}", delay=random.randint(35, 80))
            self._human_pause(
                page,
                settings.publish_typing_pause_min_seconds,
                settings.publish_typing_pause_max_seconds,
                cancel_event,
            )
            if self._select_topic_candidate(page, hashtag):
                self._bound_hashtags.append(hashtag)
            else:
                self._unresolved_hashtags.append(hashtag)
        self._record_human_action(page, cancel_event)

    def _select_topic_candidate(self, page, hashtag: str) -> bool:
        cancel_event = getattr(self, "_active_cancel_event", None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self._check_runtime_pause(page, cancel_event)
            for selector in self.topic_candidate_selectors:
                candidates = page.locator(selector)
                for index in range(min(candidates.count(), 12)):
                    candidate = candidates.nth(index)
                    try:
                        if not candidate.is_visible():
                            continue
                        text = candidate.inner_text(timeout=500)
                        if not self._topic_candidate_matches(text, hashtag):
                            continue
                        candidate.click(timeout=2000)
                        self._record_human_action(page, cancel_event)
                        self._human_pause(page, 0.2, 0.4, cancel_event)
                        return True
                    except RuntimeError:
                        raise
                    except Exception:
                        continue
            self._human_pause(page, 0.12, 0.2, cancel_event)
        return False

    @staticmethod
    def _topic_candidate_matches(candidate_text: str, hashtag: str) -> bool:
        candidate = re.sub(r"\s+", " ", candidate_text).strip().lstrip("#＃").strip()
        expected = re.escape(hashtag.strip())
        return bool(re.match(rf"^{expected}(?:$|\s|[（(·])", candidate, re.IGNORECASE))

    @staticmethod
    def _find_visible(page, selectors: tuple[str, ...]):
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 5)):
                item = locator.nth(index)
                try:
                    if item.is_visible():
                        return item
                except Exception:
                    continue
        return None

    def _review_message(self, snapshot: PublishSnapshot, visibility_message: str) -> str:
        _hashtags, mentions = split_interactive_tokens(snapshot.body)
        hints: list[str] = []
        if self._bound_hashtags:
            hints.append("已自动关联抖音话题：" + " ".join(f"#{tag}" for tag in self._bound_hashtags))
        if self._unresolved_hashtags:
            hints.append(
                "以下话题未找到精确候选，请在官方页面重新输入并选择："
                + " ".join(f"#{tag}" for tag in self._unresolved_hashtags)
            )
        if mentions:
            prefix = (
                f"已打开 @{self._opened_mention} 的官方下拉列表；请确认后继续处理用户："
                if self._opened_mention
                else "请在官方页面逐个输入并从下拉列表确认用户："
            )
            hints.append(prefix + " ".join(f"@{name}" for name in mentions))
        if not hints:
            hints.append("如需 @用户，请在官方页面输入并从下拉列表确认")
        return f"{visibility_message}；{'；'.join(hints)}；请检查封面、分区等选项，然后回到 Content Hub 确认发布"

    def _prepare_interactive_review(self, page, snapshot: PublishSnapshot) -> None:
        _hashtags, mentions = split_interactive_tokens(snapshot.body)
        if not mentions:
            return
        editor = self._find_visible(page, self.body_selectors)
        if editor is None:
            return
        try:
            cancel_event = getattr(self, "_active_cancel_event", None)
            self._check_runtime_pause(page, cancel_event)
            editor.click(timeout=3000)
            editor.press("Control+End")
            editor.press("Enter")
            editor.press("Enter")
            editor.type(f"@{mentions[0]}", delay=random.randint(35, 80))
            self._record_human_action(page, cancel_event)
            self._human_pause(page, 0.4, 0.7, cancel_event)
            self._opened_mention = mentions[0]
        except RuntimeError:
            raise
        except Exception:
            self._opened_mention = None


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
                        self._record_human_action(page, getattr(self, "_active_cancel_event", None))
                        return
                except RuntimeError:
                    raise
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
                        self._record_human_action(page, getattr(self, "_active_cancel_event", None))
                except RuntimeError:
                    raise
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