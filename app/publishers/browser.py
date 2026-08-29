from __future__ import annotations

from abc import ABC
from datetime import datetime, timezone
import random
import re
import threading
import time
from pathlib import Path
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .base import ContentCallback, PublicationCancelled, PublishSnapshot, StatusCallback
from ..config import settings


_HERE = Path(__file__).resolve().parent


def _platform_schedule_timezone():
    """Use the configured publishing timezone, never the host machine timezone."""
    try:
        return ZoneInfo(settings.publish_day_timezone)
    except ZoneInfoNotFoundError:
        return timezone.utc


class NativeScheduleSetupError(RuntimeError):
    """A recoverable creator-page scheduling setup error requiring manual input."""


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


# ----------------------------------------------------------------------
# 请求头管理：严格复刻 Chrome 的请求头排序，并随浏览路径动态更新
# 参考：https://github.com/whatwg/fetch/issues/1428
#       Chromium 发送顺序为：Host → Connection → Content-Length → Content-Type →
#       Cache-Control → Upgrade-Insecure-Requests → User-Agent → Accept →
#       Sec-Fetch-Site → Sec-Fetch-Mode → Sec-Fetch-User → Sec-Fetch-Dest →
#       Referer → Accept-Encoding → Accept-Language → Cookie
# ----------------------------------------------------------------------

# Chrome 默认首导航的请求头顺序（列表顺序即发送顺序）
CHROME_HEADER_ORDER: tuple[str, ...] = (
    "Host",
    "Connection",
    "Content-Length",
    "Content-Type",
    "Cache-Control",
    "Upgrade-Insecure-Requests",
    "User-Agent",
    "Accept",
    "Sec-Fetch-Site",
    "Sec-Fetch-Mode",
    "Sec-Fetch-User",
    "Sec-Fetch-Dest",
    "Referer",
    "Accept-Encoding",
    "Accept-Language",
    "Cookie",
)
# Firefox 的常见首导航请求头顺序
FIREFOX_HEADER_ORDER: tuple[str, ...] = (
    "Host",
    "User-Agent",
    "Accept",
    "Accept-Language",
    "Accept-Encoding",
    "Referer",
    "Connection",
    "Cookie",
    "Upgrade-Insecure-Requests",
    "Sec-Fetch-Dest",
    "Sec-Fetch-Mode",
    "Sec-Fetch-Site",
    "Cache-Control",
)
# Microsoft Edge：基于 Chromium，但请求头仍有细微差异，Sec-Fetch-* 顺序略前，且会额外发送 Sec-CH-UA/UA-Cookie 略有不同
EDGE_HEADER_ORDER: tuple[str, ...] = (
    "Host",
    "Connection",
    "Cache-Control",
    "Upgrade-Insecure-Requests",
    "User-Agent",
    "Accept",
    "Sec-Fetch-Site",
    "Sec-Fetch-Mode",
    "Sec-Fetch-User",
    "Sec-Fetch-Dest",
    "Referer",
    "Accept-Encoding",
    "Accept-Language",
    "Cookie",
)
# Safari（Mac/iOS）：通常没有 Sec-Fetch-* 系列，Cookie 和 Accept 顺序和 Chrome 不同
SAFARI_HEADER_ORDER: tuple[str, ...] = (
    "Host",
    "User-Agent",
    "Accept-Encoding",
    "Accept",
    "Accept-Language",
    "Referer",
    "Connection",
    "Cookie",
    "Upgrade-Insecure-Requests",
)

# 常见浏览器的 UA 模板（供 RequestHeaderTracker 与 context 选择）
BROWSER_USER_AGENTS: dict[str, str] = {
    "chrome": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "edge": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0"
    ),
    "firefox": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) "
        "Gecko/20100101 Firefox/131.0"
    ),
    "safari": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.0 Safari/605.1.15"
    ),
    "safari_ios": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.0 Mobile/15E148 Safari/604.1"
    ),
}
# 每种浏览器的 Accept / Accept-Language / Accept-Encoding 默认值
BROWSER_ACCEPT_VALUES: dict[str, dict[str, str]] = {
    "chrome": {
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "accept_language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "accept_encoding": "gzip, deflate, br, zstd",
    },
    "edge": {
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "accept_language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
        "accept_encoding": "gzip, deflate, br, zstd",
    },
    "firefox": {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept_language": "zh-CN,zh;q=0.8,en-US;q=0.5,en;q=0.3",
        "accept_encoding": "gzip, deflate, br, zstd",
    },
    "safari": {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept_language": "zh-CN,zh-Hans;q=0.9,en-US;q=0.7,en;q=0.3",
        "accept_encoding": "gzip, deflate, br",
    },
    "safari_ios": {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept_language": "zh-CN,zh-Hans;q=0.9,en-US;q=0.7,en;q=0.3",
        "accept_encoding": "gzip, deflate, br",
    },
}


class RequestHeaderTracker:
    """追踪浏览路径，按真实浏览器顺序动态管理请求头与 Referer。

    用法：
        tracker = RequestHeaderTracker(browser="chrome")
        page.on("request", lambda r: tracker.on_request(r))
        page.on("framenavigated", lambda f: tracker.on_navigation(f.url))
        tracker.navigate_to(list_page_url)    # 进入列表页
        # 页面内自然跳转时，Referer 会被自动更新为上一页 URL
    """

    _ORDER_MAP: dict[str, tuple[str, ...]] = {
        "chrome": CHROME_HEADER_ORDER,
        "chromium": CHROME_HEADER_ORDER,
        "edge": EDGE_HEADER_ORDER,
        "firefox": FIREFOX_HEADER_ORDER,
        "safari": SAFARI_HEADER_ORDER,
        "safari_ios": SAFARI_HEADER_ORDER,
    }

    def __init__(self, browser: str = "chrome") -> None:
        self.browser: str = browser.lower()
        self.header_order: tuple[str, ...] = self._ORDER_MAP.get(
            self.browser, CHROME_HEADER_ORDER
        )
        # 浏览历史栈：最后一个为"当前页"，前一个为"上一页（用于 Referer）"
        self._history: list[str] = []
        # 上一个非同源跳转的 referer 来源
        self._last_document_url: str | None = None

    # ---------- 浏览历史 / Referer 管理 ----------
    def navigate_to(self, url: str) -> None:
        """记录一次有意的"页面导航"，可用于后续 Referer 生成。"""
        self._history.append(url)
        if len(self._history) > 32:
            self._history.pop(0)
        self._last_document_url = url

    def on_navigation(self, url: str) -> None:
        """与 Playwright `framenavigated` 事件配合使用。"""
        self.navigate_to(url)

    def on_request(self, request) -> None:
        """与 Playwright `request` 事件配合使用，记录历史 URL。"""
        try:
            url = request.url
            if request.resource_type == "document":
                self._last_document_url = url
                self._history.append(url)
                if len(self._history) > 32:
                    self._history.pop(0)
        except Exception:
            pass

    @property
    def referer(self) -> str | None:
        """返回用于下一个请求的 Referer（上一个页面 URL）。"""
        if len(self._history) >= 2:
            return self._history[-2]
        return self._last_document_url

    @property
    def current_url(self) -> str | None:
        return self._history[-1] if self._history else None

    # ---------- 按真实浏览器排序的请求头 ----------
    def build_ordered_headers(
        self,
        *,
        user_agent: str | None = None,
        accept: str | None = None,
        accept_language: str | None = None,
        accept_encoding: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> list[tuple[str, str]]:
        """按 self.header_order 中定义的顺序组装请求头。

        返回 list[tuple]，可直接转成 HTTP headers，保证字段顺序不被字典打乱。
        """
        defaults = BROWSER_ACCEPT_VALUES.get(self.browser, BROWSER_ACCEPT_VALUES["chrome"])
        effective_ua = user_agent or BROWSER_USER_AGENTS.get(
            self.browser, BROWSER_USER_AGENTS["chrome"]
        )

        values: dict[str, str] = {}
        values["User-Agent"] = effective_ua
        values["Accept"] = accept or defaults["accept"]
        values["Accept-Language"] = accept_language or defaults["accept_language"]
        values["Accept-Encoding"] = accept_encoding or defaults["accept_encoding"]
        ref = self.referer
        if ref:
            values["Referer"] = ref
        values["Connection"] = "keep-alive"
        values["Upgrade-Insecure-Requests"] = "1"
        # Safari 一般不发送 Sec-Fetch-* 头
        if self.browser not in ("safari", "safari_ios"):
            values["Sec-Fetch-Site"] = "same-origin" if ref and self._same_origin(ref) else "none"
            values["Sec-Fetch-Mode"] = "navigate"
            values["Sec-Fetch-User"] = "?1"
            values["Sec-Fetch-Dest"] = "document"
        if extra_headers:
            for k, v in extra_headers.items():
                values[k] = v

        # 按预定顺序输出（未提供的键忽略，额外键按字典字母顺序附加在末尾）
        seen: set[str] = set()
        ordered: list[tuple[str, str]] = []
        for key in self.header_order:
            canonical = _find_key_case_insensitive(values, key)
            if canonical is None:
                continue
            seen.add(canonical)
            ordered.append((key, values[canonical]))
        for key, value in values.items():
            if key in seen:
                continue
            ordered.append((key, value))
        return ordered

    @staticmethod
    def _same_origin(url_a: str) -> bool:
        # 简单的同源判断（仅判断域名部分）
        import re

        host_match = re.search(r"://([^/]+)", url_a)
        return bool(host_match)


def _find_key_case_insensitive(data: dict[str, str], key: str) -> str | None:
    lower = key.lower()
    for k in data:
        if k.lower() == lower:
            return k
    return None


def apply_anti_detection_script(context, page=None) -> None:
    """Inject anti-detection init script into a Playwright context."""
    if not ANTI_DETECTION_INIT_SCRIPT:
        return
    try:
        context.add_init_script(ANTI_DETECTION_INIT_SCRIPT)
    except Exception:
        if page is None:
            return
        try:
            page.add_init_script(ANTI_DETECTION_INIT_SCRIPT)
        except Exception:
            pass


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
    r"验证码|安全验证|身份验证|"
    r"登录异常|重新登录|扫码登录|登录后|请登录|账号异常|风控",
    re.IGNORECASE,
)
LOGIN_URL_PATTERN = re.compile(r"login|signin|passport|sso", re.IGNORECASE)
RISK_RESPONSE_STATUSES = {401, 403}


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


def get_browser_user_agent(browser: str) -> str:
    """返回对应浏览器的 UA 字符串（默认回退到 Chrome）。"""
    return BROWSER_USER_AGENTS.get(browser.lower(), BROWSER_USER_AGENTS["chrome"])


def detect_browser_type(executable_path: Path | None) -> str:
    """根据浏览器可执行文件名识别浏览器类型（chrome/edge/firefox/safari）。"""
    if executable_path is None:
        return "chrome"
    name = executable_path.name.lower()
    if "edge" in name or "msedge" in name:
        return "edge"
    if "firefox" in name:
        return "firefox"
    if "safari" in name:
        return "safari"
    return "chrome"


def _browser_executables() -> dict[str, Path]:
    """返回所有常见浏览器类型到可执行文件路径的映射。"""
    return {
        "edge": Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        "edge2": Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        "edge3": Path.home() / r"AppData\Local\Microsoft\Edge\Application\msedge.exe",
        "chrome": Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        "chrome2": Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    }


def get_browser_executable(browser: str | None = None) -> Path | None:
    """返回指定浏览器类型的可执行文件路径；若 browser=None，则返回第一个可用浏览器。"""
    candidates = _browser_executables()
    if browser is None:
        for _key, path in candidates.items():
            if path.is_file():
                return path
        return None
    key_root = browser.lower()
    for key, path in candidates.items():
        if key.startswith(key_root) and path.is_file():
            return path
    return None


def _browser_executable() -> Path | None:
    """向后兼容：返回任意一个可用浏览器的可执行文件（优先 edge，其次 chrome）。"""
    return get_browser_executable()


def browser_context_options(
    *,
    visible: bool,
    accept_downloads: bool = False,
    browser_type: str | None = None,
) -> dict:
    base_args = ["--start-maximized"] if visible else []
    # 去重：保持基础参数与反检测参数的顺序，使用 dict.fromkeys 去重
    extra_args: list[str] = []
    extra_args.extend(base_args)
    extra_args.extend(ANTI_DETECTION_ARGS)
    seen: dict[str, None] = dict.fromkeys(extra_args)
    # 移除典型自动化/测试参数
    automation_signals = {"--enable-automation", "--test-type", "--automation"}
    filtered_args = [arg for arg in seen.keys() if arg.split("=")[0] not in automation_signals]
    options = {
        "headless": not visible,
        "no_viewport": visible,
        "accept_downloads": accept_downloads,
        "args": filtered_args,
        # 禁止 Playwright 默认注入自动化参数，这是最基础的反爬指纹
        "ignore_default_args": ["--enable-automation", "--test-type"],
    }
    resolved_browser = (browser_type or "chrome").lower()
    ua = settings.browser_user_agent or get_browser_user_agent(resolved_browser)
    if ua:
        options["user_agent"] = ua
    if settings.browser_timezone:
        options["timezone_id"] = settings.browser_timezone
    return options


class TopicCandidateMixin:
    """Shared exact-first / conservative-fuzzy tag binding for every publisher."""

    topic_candidate_selectors = (
        "[role='listbox']:visible [role='option']:visible",
        ".semi-portal:visible .semi-select-option:visible",
        "[class*='suggest']:visible [class*='item']:visible",
        "[class*='topic']:visible [class*='item']:visible",
        "[class*='popover']:visible [class*='item']:visible",
        "[class*='dropdown']:visible [class*='item']:visible",
    )

    def _bind_hashtags_after_prefill(self, page, snapshot: PublishSnapshot) -> bool:
        hashtags, _mentions = split_interactive_tokens(snapshot.body)
        if not hashtags or self._hashtags_attempted:
            return True
        editor = self._find_visible(page, self.body_selectors)
        if editor is None:
            return False
        self._hashtags_attempted = True
        self._append_and_bind_hashtags(page, editor, hashtags, bool(self._body_for_prefill(snapshot)))
        return True

    def _append_and_bind_hashtags(self, page, editor, hashtags: list[str], has_plain_body: bool) -> None:
        cancel_event = getattr(self, "_active_cancel_event", None)
        self._check_runtime_pause(page, cancel_event)
        if not self._place_topic_cursor_at_end(page, editor, cancel_event):
            self._unresolved_hashtags.extend(hashtags)
            return
        if has_plain_body:
            editor.press("Enter")
            editor.press("Enter")
        for index, hashtag in enumerate(hashtags):
            self._check_runtime_pause(page, cancel_event)
            # A topic selection may move focus back to a previous token.  Reset and
            # verify the caret before every insert, particularly for Xiaohongshu's
            # contenteditable editor where this otherwise concatenates later tags.
            if not self._place_topic_cursor_at_end(page, editor, cancel_event):
                self._unresolved_hashtags.extend(hashtags[index:])
                break
            if index:
                self._insert_topic_text(page, editor, " ")
            # Do not type a tag character by character.  On Douyin, typing the
            # prefix of #cosplay reaches #cos first; the editor then selects #cos
            # and places the caret inside it before the remaining letters arrive.
            # One atomic insert makes the suggestion engine see only the complete
            # intended tag.
            self._insert_topic_text(page, editor, f"#{hashtag}")
            self._wait_for_topic_menu(page, cancel_event)
            match = self._select_topic_candidate(page, hashtag)
            if match:
                match_kind, candidate_name, similarity = match
                if match_kind == "exact":
                    self._bound_hashtags.append(hashtag)
                else:
                    self._fuzzy_bound_hashtags.append((hashtag, candidate_name, similarity))
            else:
                # Creator pages commonly keep the first suggestion keyboard-selected.
                # Leaving that menu open can replace the literal tag as the next text
                # is entered or when the editor loses focus.  Never let an unmatched
                # tag silently become a related-but-different platform topic.
                self._dismiss_topic_menu(page, cancel_event)
                self._unresolved_hashtags.append(hashtag)
            # Check immediately after the menu interaction as well.  If the page
            # cannot restore an end-of-editor caret, leave the remaining tags for
            # manual confirmation instead of inserting them into an earlier tag.
            if not self._place_topic_cursor_at_end(page, editor, cancel_event):
                self._unresolved_hashtags.extend(hashtags[index + 1:])
                break
        self._record_human_action(page, cancel_event)

    def _place_topic_cursor_at_end(self, page, editor, cancel_event) -> bool:
        """Focus an editor and verify that its caret is at its real end position."""
        try:
            self._check_runtime_pause(page, cancel_event)
            return bool(editor.evaluate("""element => {
                element.focus();
                if (typeof element.selectionStart === 'number' && 'value' in element) {
                    const end = element.value.length;
                    element.setSelectionRange(end, end);
                    return element.selectionStart === end && element.selectionEnd === end;
                }
                const selection = window.getSelection();
                if (!selection) return false;
                const range = document.createRange();
                range.selectNodeContents(element);
                range.collapse(false);
                selection.removeAllRanges();
                selection.addRange(range);
                return selection.rangeCount === 1
                    && selection.isCollapsed
                    && selection.anchorNode === range.startContainer
                    && selection.anchorOffset === range.startOffset;
            }"""))
        except RuntimeError:
            raise
        except Exception:
            return False

    @staticmethod
    def _insert_topic_text(page, editor, text: str) -> None:
        """Insert text in one input event so tag prefixes cannot be auto-selected."""
        try:
            page.keyboard.insert_text(text)
            return
        except Exception:
            # Fallback for browser drivers that do not expose Keyboard.insert_text.
            # Keep this a single DOM input event rather than falling back to type(),
            # which recreates the partial-prefix completion bug.
            editor.evaluate(
                """(element, value) => {
                    element.focus();
                    if (typeof element.setRangeText === 'function'
                        && typeof element.selectionStart === 'number') {
                        element.setRangeText(value, element.selectionStart, element.selectionEnd, 'end');
                    } else {
                        const selection = window.getSelection();
                        const range = selection && selection.rangeCount ? selection.getRangeAt(0) : document.createRange();
                        range.deleteContents();
                        const node = document.createTextNode(value);
                        range.insertNode(node);
                        range.setStartAfter(node);
                        range.collapse(true);
                        selection.removeAllRanges();
                        selection.addRange(range);
                    }
                    element.dispatchEvent(new InputEvent('input', {
                        bubbles: true, inputType: 'insertText', data: value,
                    }));
                }""",
                text,
            )

    def _select_topic_candidate(self, page, hashtag: str) -> tuple[str, str, float] | None:
        cancel_event = getattr(self, "_active_cancel_event", None)
        # Do not allow fuzzy matching until the full exact-match window has elapsed:
        # the platform often renders the closest candidate first and the exact one a
        # moment later, especially for Chinese and mixed-language tags.
        exact_deadline = time.monotonic() + 4
        while time.monotonic() < exact_deadline:
            self._check_runtime_pause(page, cancel_event)
            # Some Douyin revisions wrap the tag title and its statistics in one
            # row, so row.inner_text() cannot expose an exact name.  Search the
            # title element itself first; this remains text-based and deliberately
            # does not trust reused aria-label values.
            exact_matches = self._visible_exact_topic_text_candidates(page, hashtag)
            exact_matches.extend(
                (candidate, candidate_name)
                for candidate, candidate_name in self._visible_topic_candidates(page)
                if self._normalize_topic_name(candidate_name) == self._normalize_topic_name(hashtag)
            )
            if exact_matches:
                candidate, candidate_name = exact_matches[0]
                if self._click_topic_candidate(candidate, page, cancel_event):
                    return "exact", candidate_name, 1.0
            self._human_pause(page, 0.18, 0.3, cancel_event)

        # Re-read the currently visible list; do not retain locator handles from the
        # exact phase because a dropdown can be re-rendered while candidates arrive.
        fuzzy_matches: list[tuple[object, str, float]] = []
        for candidate, candidate_name in self._visible_topic_candidates(page):
            similarity = self._topic_similarity(candidate_name, hashtag)
            if self._is_safe_fuzzy_topic_match(candidate_name, hashtag, similarity):
                fuzzy_matches.append((candidate, candidate_name, similarity))
        if fuzzy_matches:
            candidate, candidate_name, similarity = max(fuzzy_matches, key=lambda item: item[2])
            if self._click_topic_candidate(candidate, page, cancel_event):
                return "fuzzy", candidate_name, similarity
        return None

    def _dismiss_topic_menu(self, page, cancel_event) -> None:
        try:
            self._check_runtime_pause(page, cancel_event)
            page.keyboard.press("Escape")
            self._human_pause(page, 0.12, 0.2, cancel_event)
        except RuntimeError:
            raise
        except Exception:
            # The literal text is already in the editor.  A failed dismissal must
            # not turn an unmatched topic into a failed publication preparation.
            return

    def _wait_for_topic_menu(self, page, cancel_event) -> None:
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            self._check_runtime_pause(page, cancel_event)
            if self._visible_topic_candidates(page):
                # Allow the visible list one further short render interval so exact
                # candidates are not hidden behind the first partial response.
                self._human_pause(page, 0.35, 0.55, cancel_event)
                return
            self._human_pause(page, 0.12, 0.2, cancel_event)

    def _visible_topic_candidates(self, page) -> list[tuple[object, str]]:
        result: list[tuple[object, str]] = []
        seen: set[str] = set()
        for selector in self.topic_candidate_selectors:
            candidates = page.locator(selector)
            for index in range(min(candidates.count(), 20)):
                candidate = candidates.nth(index)
                try:
                    if not candidate.is_visible():
                        continue
                    text = candidate.inner_text(timeout=500)
                    # The displayed candidate name is authoritative.  Some creator
                    # pages reuse aria-label on a list container/button, so trusting
                    # it before visible text can click a different tag.
                    candidate_name = self._topic_candidate_name(text)
                    if not candidate_name:
                        for attribute in ("data-topic-name", "data-name", "title"):
                            value = candidate.get_attribute(attribute)
                            if value:
                                candidate_name = self._topic_candidate_name(value)
                                if candidate_name:
                                    break
                    if candidate_name and candidate_name not in seen:
                        seen.add(candidate_name)
                        result.append((candidate, candidate_name))
                except RuntimeError:
                    raise
                except Exception:
                    continue
        return result

    def _visible_exact_topic_text_candidates(self, page, hashtag: str) -> list[tuple[object, str]]:
        """Return visible descendants whose displayed text is exactly the requested tag."""
        target = self._normalize_topic_name(hashtag)
        if not target:
            return []
        try:
            pattern = re.compile(rf"^\s*[#＃]?\s*{re.escape(hashtag)}\s*$", re.IGNORECASE)
            candidates = page.get_by_text(pattern)
            result: list[tuple[object, str]] = []
            for index in range(min(candidates.count(), 20)):
                candidate = candidates.nth(index)
                if not candidate.is_visible():
                    continue
                if self._normalize_topic_name(candidate.inner_text(timeout=500)) == target:
                    result.append((candidate, target))
            return result
        except RuntimeError:
            raise
        except Exception:
            return []

    def _click_topic_candidate(self, candidate, page, cancel_event) -> bool:
        try:
            if not candidate.is_visible():
                return False
            candidate.click(timeout=2000)
            self._record_human_action(page, cancel_event)
            self._human_pause(page, 0.2, 0.4, cancel_event)
            return True
        except RuntimeError:
            raise
        except Exception:
            return False

    @staticmethod
    def _topic_candidate_matches(candidate_text: str, hashtag: str) -> bool:
        return TopicCandidateMixin._topic_candidate_name(candidate_text) == TopicCandidateMixin._normalize_topic_name(hashtag)

    @staticmethod
    def _topic_candidate_name(candidate_text: str) -> str:
        value = unicodedata.normalize("NFKC", candidate_text or "")
        value = value.replace("\u200b", "").replace("\ufeff", "").strip().lstrip("#").strip()
        first_line = next((line.strip() for line in value.splitlines() if line.strip()), "")
        first_line = re.split(r"\s*[（(].*$", first_line, maxsplit=1)[0]
        first_line = re.split(r"\s*[·•]\s*(?=\d).*$", first_line, maxsplit=1)[0]
        first_line = re.split(r"\s+(?=\d)", first_line, maxsplit=1)[0]
        return TopicCandidateMixin._normalize_topic_name(first_line)

    @staticmethod
    def _normalize_topic_name(value: str) -> str:
        value = unicodedata.normalize("NFKC", value or "")
        value = value.replace("\u200b", "").replace("\ufeff", "")
        return re.sub(r"\s+", "", value.strip().lstrip("#＃").strip().casefold())

    @staticmethod
    def _topic_similarity(candidate: str, hashtag: str) -> float:
        left = TopicCandidateMixin._normalize_topic_name(candidate)
        right = TopicCandidateMixin._normalize_topic_name(hashtag)
        if not left or not right:
            return 0.0
        previous = list(range(len(right) + 1))
        for left_index, left_char in enumerate(left, start=1):
            current = [left_index]
            for right_index, right_char in enumerate(right, start=1):
                current.append(min(current[-1] + 1, previous[right_index] + 1,
                                   previous[right_index - 1] + (left_char != right_char)))
            previous = current
        return 1 - previous[-1] / max(len(left), len(right))

    @staticmethod
    def _is_safe_fuzzy_topic_match(candidate: str, hashtag: str, similarity: float) -> bool:
        candidate = TopicCandidateMixin._normalize_topic_name(candidate)
        hashtag = TopicCandidateMixin._normalize_topic_name(hashtag)
        maximum = max(len(candidate), len(hashtag))
        if not candidate or not hashtag or maximum <= 2 or candidate == hashtag:
            return False
        # A prefix/suffix expansion is a different topic (for example
        # “时崎狂三cos” → “时崎狂三cos假发”), not a typo.  Similarity alone made
        # these dangerous false positives appear exact enough.
        if candidate.startswith(hashtag) or hashtag.startswith(candidate):
            return False
        distance = TopicCandidateMixin._topic_edit_distance(candidate, hashtag)
        # Fuzzy selection is only a typo recovery path.  One changed character is
        # safe for short Chinese/mixed tags; longer tags may tolerate two changes.
        allowed_distance = 1 if maximum <= 8 else 2
        return distance <= allowed_distance and similarity >= (0.84 if maximum <= 5 else 0.88)

    @staticmethod
    def _topic_edit_distance(left: str, right: str) -> int:
        left = TopicCandidateMixin._normalize_topic_name(left)
        right = TopicCandidateMixin._normalize_topic_name(right)
        if len(left) < len(right):
            left, right = right, left
        previous = list(range(len(right) + 1))
        for left_index, left_char in enumerate(left, start=1):
            current = [left_index]
            for right_index, right_char in enumerate(right, start=1):
                current.append(min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                ))
            previous = current
        return previous[-1]

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


class BrowserPublisher(TopicCandidateMixin, ABC):
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

    def __init__(self) -> None:
        self._hashtags_attempted = False
        self._bound_hashtags: list[str] = []
        self._fuzzy_bound_hashtags: list[tuple[str, str, float]] = []
        self._unresolved_hashtags: list[str] = []
        self._native_schedule_warning: str | None = None

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
        self._upload_mode_selected = False
        self._native_schedule_warning = None

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                executable_path=str(executable),
                **browser_context_options(visible=True, accept_downloads=False),
            )
            page = None
            try:
                page = context.pages[0] if context.pages else context.new_page()
                apply_anti_detection_script(context, page)
                # 浏览路径追踪器：用于动态生成 Referer / 请求头顺序
                header_tracker = RequestHeaderTracker(browser="chrome")
                page.on("framenavigated", lambda frame: header_tracker.on_navigation(frame.url))
                page.on("request", header_tracker.on_request)
                self._active_page = page
                self._active_header_tracker = header_tracker
                page.on("close", lambda: self._page_closed_event.set())
                page.on("response", self._capture_risk_response)
                self._awaiting_login = True
                self._login_wait_reported = False
                media_label = "视频" if snapshot.assets[0].media_type == "video" else "图文"
                on_status(
                    "awaiting_login",
                    f"已检测到{media_label}素材，正在打开{self.display_name}{media_label}发布页并检查登录状态",
                )
                try:
                    page.goto(self.publish_url(snapshot), wait_until="load", timeout=90_000)
                except Exception:
                    if self._page_was_closed(page):
                        raise
                    # A stale login redirect can abort the original navigation while
                    # still leaving a usable login page in the visible browser.
                    on_status("awaiting_login", f"发布页尚未打开；请在{self.display_name}窗口中完成登录")
                self._wait_for_full_load(page, cancel_event)
                self._wait_for_publish_editor(page, snapshot, cancel_event, on_status)
                file_input = self._wait_for_file_input(page, snapshot, cancel_event)
                self._awaiting_login = False
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
                if snapshot.scheduled_at:
                    on_status("preparing", "正在自动勾选平台定时发布并填写预约时间")
                    try:
                        self._apply_native_schedule(page, snapshot, cancel_event)
                        visibility_message += (
                            f"；已自动设置平台定时发布："
                            f"{self._format_schedule_time(snapshot.scheduled_at)}"
                        )
                    except NativeScheduleSetupError as exc:
                        # Keep the uploaded work and the persistent browser open for
                        # the user to complete this platform-only control manually.
                        self._native_schedule_warning = str(exc)
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
                if snapshot.scheduled_at:
                    on_status("scheduling", "正在提交已设置的平台原生定时发布")
                    submit_message = "正在向平台提交原生定时发布"
                else:
                    on_status("publishing", "正在向平台提交作品")
                    submit_message = "正在向平台提交作品"
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
                on_status("scheduling" if snapshot.scheduled_at else "publishing", submit_message)
                self._human_pause(page, 0.8, 1.8, cancel_event)
                button.click(timeout=15_000)
                self._record_human_action(page, cancel_event)
                self._click_secondary_confirmation(page, cancel_event)
                return self._wait_for_result(page, cancel_event, scheduled_at=snapshot.scheduled_at)
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

    def _wait_for_publish_editor(
        self,
        page,
        snapshot: PublishSnapshot,
        cancel_event: threading.Event,
        on_status: StatusCallback,
    ) -> None:
        """Wait for the first-party editor without closing a stale login page.

        A failed session commonly redirects the creator URL to login.  That is not a
        publication failure: keep the visible persistent browser open for the user
        to authenticate, then retry the creator URL until it is actually reached.
        """
        last_retry_at = 0.0
        while True:
            self._check_runtime_pause(page, cancel_event, allow_login=True)
            if self._is_publish_editor_url(page.url):
                self._awaiting_login = False
                on_status("preparing", "已确认登录状态并进入官方发布页")
                return

            self._awaiting_login = True
            if not self._login_wait_reported:
                on_status(
                    "awaiting_login",
                    f"登录态已失效或未进入{self.display_name}发布页；请在保持打开的窗口中手动登录",
                )
                self._login_wait_reported = True

            # Platforms do not always return to the upload route after login.  Once
            # the login document has gone away, revisit the official upload URL.
            if not self._is_login_page(page) and time.monotonic() - last_retry_at >= 2:
                last_retry_at = time.monotonic()
                try:
                    page.goto(self.publish_url(snapshot), wait_until="domcontentloaded", timeout=30_000)
                except Exception:
                    pass
            self._human_pause(page, 0.8, 1.2, cancel_event, allow_login=True)

    def _wait_for_file_input(self, page, snapshot, cancel_event: threading.Event):
        while True:
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

    def _choose_upload_mode(self, page, snapshot: PublishSnapshot) -> None:
        if self._upload_mode_selected:
            return
        for label in self._upload_mode_labels(snapshot):
            button = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$"))
            text = page.get_by_text(label, exact=True)
            for locator in (button, text):
                for index in range(min(locator.count(), 5)):
                    item = locator.nth(index)
                    try:
                        if item.is_visible() and item.is_enabled():
                            item.click(timeout=2000)
                            self._upload_mode_selected = True
                            self._record_human_action(page, getattr(self, "_active_cancel_event", None))
                            return
                    except RuntimeError:
                        raise
                    except Exception:
                        continue

    def _upload_mode_labels(self, snapshot: PublishSnapshot) -> tuple[str, ...]:
        return (
            ("发布视频", "上传视频", "视频")
            if snapshot.assets[0].media_type == "video"
            else ("发布图文", "上传图文", "图文")
        )

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
        if self.platform not in {"douyin", "xiaohongshu", "bilibili"}:
            return snapshot.body
        # B站 does not currently bind @ candidates automatically, so retain mentions
        # there while removing only tags that will be reinserted through its topic UI.
        token_pattern = (
            re.compile(r"(?<!\w)[#＃][^\s#＃@＠,，。.!！?？;；:：]+")
            if self.platform == "bilibili" else INTERACTIVE_TOKEN_PATTERN
        )
        body = token_pattern.sub("", snapshot.body)
        lines = [re.sub(r"[ \t]{2,}", " ", line).strip() for line in body.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    @staticmethod
    def _interactive_tokens(body: str) -> list[str]:
        return list(dict.fromkeys(INTERACTIVE_TOKEN_PATTERN.findall(body)))

    def _review_message(self, snapshot: PublishSnapshot, visibility_message: str) -> str:
        hashtags, mentions = split_interactive_tokens(snapshot.body)
        topic_hints: list[str] = []
        if self._bound_hashtags:
            topic_hints.append("已自动关联平台话题：" + " ".join(f"#{tag}" for tag in self._bound_hashtags))
        if self._fuzzy_bound_hashtags:
            topic_hints.append(
                "以下话题使用相似候选，请核对："
                + "；".join(
                    f"#{source} → #{target}（相似度 {similarity:.0%}）"
                    for source, target, similarity in self._fuzzy_bound_hashtags
                )
            )
        if self._unresolved_hashtags:
            topic_hints.append("未找到可靠候选：" + " ".join(f"#{tag}" for tag in self._unresolved_hashtags))
        elif hashtags and not self._bound_hashtags and not self._fuzzy_bound_hashtags:
            topic_hints.append("请在官方页面确认 #tag 候选")
        if mentions:
            topic_hints.append("请在官方页面确认 @用户候选：" + " ".join(f"@{name}" for name in mentions))
        token_hint = f"；{'；'.join(topic_hints)}" if topic_hints else ""
        schedule_hint = self._schedule_review_hint(snapshot)
        return f"{visibility_message}{token_hint}{schedule_hint}；请检查封面、分区等选项，然后回到 Content Hub 确认发布"

    def _schedule_review_hint(self, snapshot: PublishSnapshot) -> str:
        if not snapshot.scheduled_at:
            return ""
        if self._native_schedule_warning:
            return (
                f"；未自动完成平台定时发布：{self._native_schedule_warning}。"
                "浏览器页面已保持打开，请手动打开定时发布、选择预约时间后再确认提交"
            )
        return (
            f"；已自动勾选平台原生定时发布：{self._format_schedule_time(snapshot.scheduled_at)}，"
            "确认后仅提交平台表单"
        )

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

    def _apply_native_schedule(
        self,
        page,
        snapshot: PublishSnapshot,
        cancel_event: threading.Event,
    ) -> None:
        """Set the creator-page's own timed-publication controls.

        This deliberately operates only on the visible, first-party creator page.
        The application never holds a timer or submits a publication at the requested
        time: once the platform confirms this form, its own scheduler owns the job.
        """
        if snapshot.scheduled_at is None:
            return
        self._check_runtime_pause(page, cancel_event)
        if not self._click_native_schedule_toggle(page):
            raise NativeScheduleSetupError(
                f"未找到{self.display_name}创作页的“定时发布”选项；请确认当前内容类型支持平台原生定时发布"
            )
        self._human_pause(page, 0.4, 0.8, cancel_event)
        if not self._fill_native_schedule_time(page, snapshot.scheduled_at):
            raise NativeScheduleSetupError(
                f"未找到{self.display_name}创作页的定时发布时间输入框；平台页面可能已改版"
            )
        self._record_human_action(page, cancel_event)
        self._confirm_native_schedule_form(page, cancel_event)

    def _click_native_schedule_toggle(self, page) -> bool:
        labels = ("定时发布", "预约发布", "定时")
        for label in labels:
            locator = page.get_by_text(label, exact=True)
            for index in range(min(locator.count(), 6)):
                item = locator.nth(index)
                try:
                    if item.is_visible():
                        item.click(timeout=2500)
                        return True
                except Exception:
                    continue
            locator = page.locator(
                f"label:has-text('{label}'), [role='radio']:has-text('{label}'), "
                f"[role='button']:has-text('{label}')"
            )
            for index in range(min(locator.count(), 6)):
                item = locator.nth(index)
                try:
                    if item.is_visible():
                        item.click(timeout=2500)
                        return True
                except Exception:
                    continue
        return False

    def _fill_native_schedule_time(self, page, scheduled_at: datetime) -> bool:
        local_time = scheduled_at.astimezone(_platform_schedule_timezone())
        datetime_value = local_time.strftime("%Y-%m-%dT%H:%M")
        display_value = local_time.strftime("%Y-%m-%d %H:%M")
        datetime_inputs = page.locator(
            "input[type='datetime-local'], input[placeholder*='发布时间'], "
            "input[placeholder*='选择时间'], input[placeholder*='选择日期时间']"
        )
        for index in range(min(datetime_inputs.count(), 8)):
            item = datetime_inputs.nth(index)
            try:
                if not item.is_visible():
                    continue
                item.fill(datetime_value if item.get_attribute("type") == "datetime-local" else display_value)
                return True
            except Exception:
                continue

        date_inputs = page.locator("input[type='date'], input[placeholder*='日期']")
        time_inputs = page.locator("input[type='time'], input[placeholder*='时间']")
        date_item = self._first_visible(date_inputs)
        time_item = self._first_visible(time_inputs)
        if date_item is None or time_item is None:
            return False
        try:
            date_item.fill(local_time.strftime("%Y-%m-%d"))
            time_item.fill(local_time.strftime("%H:%M"))
            return True
        except Exception:
            return False

    @staticmethod
    def _first_visible(locator):
        for index in range(min(locator.count(), 8)):
            item = locator.nth(index)
            try:
                if item.is_visible():
                    return item
            except Exception:
                continue
        return None

    def _confirm_native_schedule_form(self, page, cancel_event: threading.Event) -> None:
        for label in ("确定", "完成", "保存"):
            locator = page.get_by_role("button", name=re.compile(rf"^{label}$"))
            for index in range(min(locator.count(), 4)):
                item = locator.nth(index)
                try:
                    if item.is_visible() and item.is_enabled():
                        item.click(timeout=2500)
                        self._record_human_action(page, cancel_event)
                        return
                except Exception:
                    continue

    @staticmethod
    def _format_schedule_time(scheduled_at: datetime) -> str:
        return scheduled_at.astimezone(_platform_schedule_timezone()).strftime("%Y-%m-%d %H:%M")

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

    def _wait_for_result(
        self,
        page,
        cancel_event: threading.Event,
        *,
        scheduled_at: datetime | None = None,
    ) -> dict:
        starting_url = page.url
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            self._check_runtime_pause(page, cancel_event)
            if page.is_closed():
                break
            try:
                if scheduled_at and self._detect_scheduled_result(page):
                    result = BrowserPublisher._result("scheduled", page.url)
                    result["scheduled_at"] = scheduled_at
                    return result
                result = BrowserPublisher._detect_result(page, starting_url)
                if result:
                    if scheduled_at:
                        result["status"] = "scheduled"
                        result["scheduled_at"] = scheduled_at
                    return result
            except Exception:
                break
            self._human_pause(page, 0.8, 1.2, cancel_event)
        result = BrowserPublisher._result(
            "scheduled" if scheduled_at else "submitted",
            page.url if not page.is_closed() else starting_url,
        )
        if scheduled_at:
            result["scheduled_at"] = scheduled_at
        return result

    @staticmethod
    def _detect_scheduled_result(page) -> bool:
        try:
            result = page.get_by_text(re.compile(r"定时发布成功|已设置定时发布|预约发布成功|已预约发布"))
            return result.count() and any(
                result.nth(index).is_visible() for index in range(min(result.count(), 5))
            )
        except Exception:
            return False

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
            # A music/category request can legitimately be rate-limited while the
            # visible editor remains usable.  Never turn that into an exception:
            # doing so would close the user's page through the execute() finally.
            if status == 429:
                return
            if (
                status in RISK_RESPONSE_STATUSES
                and resource_type == "document"
                and (
                    getattr(self, "_awaiting_login", False)
                    or LOGIN_URL_PATTERN.search(response.url or "")
                )
            ):
                return
            if status in RISK_RESPONSE_STATUSES and resource_type == "document":
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
            if allow_login and self._is_login_page(page):
                return None
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
        if allow_login and self._is_login_page(page, text):
            return None
        return f"检测到页面提示“{token}”"

    @staticmethod
    def _is_login_page(page, text: str | None = None) -> bool:
        try:
            if LOGIN_URL_PATTERN.search(page.url or ""):
                return True
            content = text if text is not None else page.locator("body").inner_text(timeout=1000)
        except Exception:
            return False
        return bool(re.search(r"扫码|二维码|账号登录|请登录|登录后|密码登录", content[:20_000], re.IGNORECASE))

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
        if self.platform == "xiaohongshu":
            return "creator.xiaohongshu.com" in normalized and "/publish" in normalized
        if self.platform == "bilibili":
            return (
                "member.bilibili.com/platform/upload" in normalized
                or "t.bilibili.com" in normalized
            )
        return False

    @staticmethod
    def _is_closed_target_error(exc: Exception) -> bool:
        message = str(exc).casefold()
        return any(token in message for token in (
            "target page, context or browser has been closed",
            "page has been closed",
            "browser has been closed",
            "target closed",
        ))

    # ------------------------------------------------------------------
    # 坐标点击 / 视觉定位辅助：降低"精准 DOM 选择器"检测概率
    # ------------------------------------------------------------------
    def _click_by_selector_with_offset(
        self,
        page,
        selector: str,
        *,
        offset_x: float | None = None,
        offset_y: float | None = None,
        force: bool = False,
    ) -> bool:
        """优先用 DOM 选择器但以元素中心附近随机偏移位置点击。

        如果 `offset_x` / `offset_y` 为 None，则在元素可见区域内随机
        选一个 8px 内的偏移位置，模拟人眼对目标的非精确点击。
        """
        try:
            locator = page.locator(selector).first
            if not locator.is_visible():
                return False
            box = locator.bounding_box(timeout=2000)
            if box is None:
                locator.click(timeout=3000, force=force)
                return True
            if offset_x is None:
                offset_x = random.uniform(-8.0, 8.0)  # noqa: S311
            if offset_y is None:
                offset_y = random.uniform(-8.0, 8.0)  # noqa: S311
            click_x = box["x"] + box["width"] / 2 + offset_x
            click_y = box["y"] + box["height"] / 2 + offset_y
            page.mouse.click(click_x, click_y)
            return True
        except Exception:
            return False

    def _click_by_text_with_offset(
        self,
        page,
        text: str,
        *,
        offset_x: float | None = None,
        offset_y: float | None = None,
    ) -> bool:
        """通过文字内容定位，再以随机偏移位置点击。"""
        try:
            locator = page.get_by_text(text, exact=False)
            for i in range(min(locator.count(), 4)):
                item = locator.nth(i)
                if not item.is_visible():
                    continue
                box = item.bounding_box(timeout=2000)
                if box is None:
                    item.click(timeout=3000)
                    return True
                if offset_x is None:
                    offset_x = random.uniform(-6.0, 6.0)  # noqa: S311
                if offset_y is None:
                    offset_y = random.uniform(-6.0, 6.0)  # noqa: S311
                click_x = box["x"] + box["width"] / 2 + offset_x
                click_y = box["y"] + box["height"] / 2 + offset_y
                page.mouse.click(click_x, click_y)
                return True
        except Exception:
            return False
        return False

    def _click_by_role_with_offset(
        self,
        page,
        role: str,
        name: str | None = None,
        *,
        offset_x: float | None = None,
        offset_y: float | None = None,
    ) -> bool:
        """通过 ARIA role / 可访问名称定位，再以随机偏移位置点击。"""
        try:
            locator = page.get_by_role(role, name=name) if name else page.get_by_role(role)
            for i in range(min(locator.count(), 4)):
                item = locator.nth(i)
                if not item.is_visible():
                    continue
                box = item.bounding_box(timeout=2000)
                if box is None:
                    item.click(timeout=3000)
                    return True
                if offset_x is None:
                    offset_x = random.uniform(-6.0, 6.0)  # noqa: S311
                if offset_y is None:
                    offset_y = random.uniform(-6.0, 6.0)  # noqa: S311
                click_x = box["x"] + box["width"] / 2 + offset_x
                click_y = box["y"] + box["height"] / 2 + offset_y
                page.mouse.click(click_x, click_y)
                return True
        except Exception:
            return False
        return False

    # ------------------------------------------------------------------
    # 渐进式页面导航：先访问列表页、再进入目标页，
    # 确保 Referer / Cookie / 浏览历史自然累积。
    # ------------------------------------------------------------------
    def _progressive_navigate(
        self,
        page,
        target_url: str,
        *,
        landing_urls: list[str] | None = None,
        min_delay_seconds: float = 1.2,
        max_delay_seconds: float = 2.5,
        timeout: float = 30000.0,
        tracker: RequestHeaderTracker | None = None,
    ) -> None:
        """先依次访问 landing_urls（列表页、首页等），再进入 target_url。

        目的：保证 Referer 来源自然、Cookie 逐步累积、避免首次请求就是目标接口。
        """
        urls_to_visit = list(landing_urls or [])
        urls_to_visit.append(target_url)
        for idx, url in enumerate(urls_to_visit):
            is_last = idx == len(urls_to_visit) - 1
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            if tracker is not None:
                tracker.navigate_to(url)
            if not is_last:
                delay = random.uniform(min_delay_seconds, max_delay_seconds)  # noqa: S311
                time.sleep(delay)


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
        super().__init__()
        self._opened_mention: str | None = None

    def _upload_mode_labels(self, snapshot: PublishSnapshot) -> tuple[str, ...]:
        return ("发布视频", "视频") if snapshot.assets[0].media_type == "video" else ("发布图文", "图文")

    def _fill_metadata(self, page, snapshot: PublishSnapshot) -> bool:
        if not super()._fill_metadata(page, snapshot):
            return False
        return self._bind_hashtags_after_prefill(page, snapshot)

    def _review_message(self, snapshot: PublishSnapshot, visibility_message: str) -> str:
        _hashtags, mentions = split_interactive_tokens(snapshot.body)
        hints: list[str] = []
        if self._bound_hashtags:
            hints.append("已自动关联抖音话题：" + " ".join(f"#{tag}" for tag in self._bound_hashtags))
        if self._fuzzy_bound_hashtags:
            hints.append(
                "以下话题未找到完全一致候选，已使用相似候选，请在官方页面核对："
                + "；".join(
                    f"#{source} → #{target}（相似度 {similarity:.0%}）"
                    for source, target, similarity in self._fuzzy_bound_hashtags
                )
            )
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
        return f"{visibility_message}；{'；'.join(hints)}{self._schedule_review_hint(snapshot)}；请检查封面、分区等选项，然后回到 Content Hub 确认发布"

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

    def _click_native_schedule_toggle(self, page) -> bool:
        """Xiaohongshu hides its schedule fields behind a real switch control."""
        labels = page.get_by_text("定时发布", exact=True)
        for index in range(min(labels.count(), 6)):
            label = labels.nth(index)
            try:
                if not label.is_visible():
                    continue
            except Exception:
                continue
            scopes = [label]
            parent = label
            for _ in range(2):
                try:
                    parent = parent.locator("xpath=..")
                    scopes.append(parent)
                except Exception:
                    break
            for scope in scopes:
                try:
                    controls = scope.locator(
                        "input[type='checkbox'], [role='switch'], button[role='switch'], [class*='switch']"
                    )
                    for control_index in range(min(controls.count(), 4)):
                        control = controls.nth(control_index)
                        if not control.is_visible():
                            continue
                        state = self._native_switch_state(control)
                        if state is True:
                            return True
                        control.click(timeout=2500)
                        page.wait_for_timeout(300)
                        # Custom switches do not always expose an ARIA state; a
                        # successful click is enough to proceed to the field wait.
                        # Do not click again if the DOM has not reflected its state
                        # yet, or the second click could turn the switch back off.
                        return True
                except Exception:
                    continue
            try:
                # Some page revisions make the text label itself toggle the switch.
                label.click(timeout=2500)
                page.wait_for_timeout(300)
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _native_switch_state(control) -> bool | None:
        try:
            aria_checked = control.get_attribute("aria-checked")
            if aria_checked is not None:
                return aria_checked.strip().lower() == "true"
            if control.get_attribute("type") == "checkbox":
                return bool(control.is_checked())
        except Exception:
            return None
        return None

    def _fill_native_schedule_time(self, page, scheduled_at: datetime) -> bool:
        # The time picker is rendered only after the switch animation completes.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if super()._fill_native_schedule_time(page, scheduled_at):
                return True
            try:
                page.wait_for_timeout(250)
            except Exception:
                break
        return False

    def _fill_metadata(self, page, snapshot: PublishSnapshot) -> bool:
        if not super()._fill_metadata(page, snapshot):
            return False
        return self._bind_hashtags_after_prefill(page, snapshot)

    def _upload_mode_labels(self, snapshot: PublishSnapshot) -> tuple[str, ...]:
        return (
            ("发布视频", "上传视频", "视频")
            if snapshot.assets[0].media_type == "video"
            else ("发布图文", "上传图文", "图文")
        )


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

    def _upload_mode_labels(self, snapshot: PublishSnapshot) -> tuple[str, ...]:
        return (
            ("发布视频", "视频投稿", "投稿视频", "视频")
            if snapshot.assets[0].media_type == "video"
            else ("发布图文", "发布动态", "图文", "图片")
        )

    def _fill_metadata(self, page, snapshot: PublishSnapshot) -> bool:
        if snapshot.assets[0].media_type == "video":
            filled = super()._fill_metadata(page, snapshot)
        else:
            combined = "\n\n".join(part for part in (snapshot.title.strip(), self._body_for_prefill(snapshot)) if part)
            filled = self._fill_first(page, self.body_selectors, combined)
        if not filled:
            return False
        return self._bind_hashtags_after_prefill(page, snapshot)

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
