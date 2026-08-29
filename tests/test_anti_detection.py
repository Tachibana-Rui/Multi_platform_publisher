from __future__ import annotations

import atexit
import os
from pathlib import Path
import tempfile

import pytest


_env_temp_dir: tempfile.TemporaryDirectory | None = None


def _load_browser_module():
    global _env_temp_dir
    if "CONTENT_HUB_DATABASE_URL" not in os.environ:
        _env_temp_dir = tempfile.TemporaryDirectory()
        atexit.register(_env_temp_dir.cleanup)
        os.environ.setdefault(
            "CONTENT_HUB_DATABASE_URL",
            f"sqlite:///{(Path(_env_temp_dir.name) / 'test.db').as_posix()}",
        )
        os.environ.setdefault(
            "CONTENT_HUB_DATA_DIR",
            os.path.join(_env_temp_dir.name, "data"),
        )
        os.environ.setdefault(
            "CONTENT_HUB_UPLOAD_DIR",
            os.path.join(_env_temp_dir.name, "uploads"),
        )

    from app.publishers import browser

    return browser


def test_browser_context_options_disable_playwright_automation_flags(monkeypatch):
    browser = _load_browser_module()
    monkeypatch.setattr(
        browser,
        "ANTI_DETECTION_ARGS",
        (
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--disable-blink-features=AutomationControlled",
            "--test-type",
            "--enable-automation",
        ),
    )

    options = browser.browser_context_options(visible=True, accept_downloads=True)

    assert options["headless"] is False
    assert options["no_viewport"] is True
    assert options["accept_downloads"] is True
    # 典型自动化参数必须被完全移除，出现在 args 中即是失败
    for arg in options["args"]:
        assert not arg.startswith("--test-type"), f"--test-type 应该被移除，实际：{arg}"
        assert not arg.startswith("--enable-automation"), f"--enable-automation 应该被移除，实际：{arg}"
        assert not arg.startswith("--automation"), f"--automation 应该被移除，实际：{arg}"
    assert "--no-first-run" in options["args"]


def test_request_header_tracker_keeps_chrome_header_order():
    browser = _load_browser_module()
    tracker = browser.RequestHeaderTracker(browser="chrome")
    tracker.navigate_to("https://example.com/list")
    tracker.navigate_to("https://example.com/detail")

    headers = tracker.build_ordered_headers(user_agent="Mozilla/5.0 Fake Chrome/126")
    # 必须是 list[tuple]，保证键的顺序与 Chrome 的发送顺序一致
    assert isinstance(headers, list)
    keys = [k for k, _v in headers]
    # 以下字段必须按此顺序出现（非绝对完整，只验证 Chrome 的常见顺序趋势）
    for key in ("User-Agent", "Referer", "Accept-Encoding", "Accept-Language"):
        assert key in keys, f"{key} 应出现在请求头中，实际：{keys}"
    # User-Agent 必须早于 Referer；Referer 必须早于 Accept-Encoding；
    # Accept-Encoding 必须早于 Accept-Language
    assert keys.index("User-Agent") < keys.index("Referer")
    assert keys.index("Referer") < keys.index("Accept-Encoding")
    assert keys.index("Accept-Encoding") < keys.index("Accept-Language")
    # Referer 应该等于上一页 URL
    values = dict(headers)
    assert values["Referer"] == "https://example.com/list"


def test_request_header_tracker_updates_referer_after_navigation():
    browser = _load_browser_module()
    tracker = browser.RequestHeaderTracker(browser="chrome")
    # 首次请求前没有 Referer
    headers1 = tracker.build_ordered_headers(user_agent="Mozilla/5.0")
    values1 = dict(headers1)
    assert "Referer" not in values1 or values1["Referer"] is None

    # 访问列表页后，再访问详情页时 Referer 应为列表页
    tracker.navigate_to("https://example.com/list")
    tracker.navigate_to("https://example.com/detail")
    headers2 = tracker.build_ordered_headers(user_agent="Mozilla/5.0")
    assert dict(headers2)["Referer"] == "https://example.com/list"


def test_firefox_header_order_is_distinct_from_chrome():
    browser = _load_browser_module()
    chrome = browser.RequestHeaderTracker(browser="chrome")
    firefox = browser.RequestHeaderTracker(browser="firefox")
    chrome_headers = [(k, v) for k, v in chrome.build_ordered_headers(user_agent="ua/chrome")]
    firefox_headers = [(k, v) for k, v in firefox.build_ordered_headers(user_agent="ua/firefox")]
    # 浏览器不同，至少 headers 的顺序不应完全一致
    assert [k for k, _v in chrome_headers] != [k for k, _v in firefox_headers]


def test_anti_detection_script_masks_browser_automation_signals(tmp_path):
    browser = _load_browser_module()
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as exc:
        pytest.skip(f"Playwright is not installed: {exc}")

    if not browser.ANTI_DETECTION_INIT_SCRIPT:
        pytest.skip("anti-detection init script is not available")

    executable = browser._browser_executable()
    launch_options = browser.browser_context_options(visible=False, accept_downloads=False)
    if executable is not None:
        launch_options["executable_path"] = str(executable)

    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(tmp_path / "profile"),
                **launch_options,
            )
        except Error as exc:
            pytest.skip(f"Chromium browser is not available for this test: {exc}")
        try:
            page = context.pages[0] if context.pages else context.new_page()
            browser.apply_anti_detection_script(context, page)
            page.goto("data:text/html,<html><body>anti detection probe</body></html>")

            signals = page.evaluate(
                """() => ({
                    webdriver: navigator.webdriver,
                    chromeRuntime: Boolean(window.chrome && window.chrome.runtime),
                    automationGlobals: Object.keys(window).filter((key) =>
                        /^cdc_/.test(key) ||
                        /^\\$cdc_/.test(key) ||
                        /^__webdriver_/.test(key) ||
                        /^selenium/.test(key)
                    ),
                    language: navigator.language,
                    languages: Array.from(navigator.languages || []),
                    platform: navigator.platform,
                    hardwareConcurrency: navigator.hardwareConcurrency,
                    pluginsLength: navigator.plugins ? navigator.plugins.length : 0,
                    mimeTypesLength: navigator.mimeTypes ? navigator.mimeTypes.length : 0,
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone
                })"""
            )
        finally:
            context.close()

    assert signals["webdriver"] is False
    assert signals["chromeRuntime"] is True
    assert signals["automationGlobals"] == []
    assert signals["language"] == "zh-CN"
    assert signals["languages"][0] == "zh-CN"
    assert any(language in signals["languages"] for language in ("zh", "zh-Hans"))
    assert signals["platform"]
    assert signals["hardwareConcurrency"] >= 4
    assert signals["pluginsLength"] >= 3
    assert signals["mimeTypesLength"] >= 3
    assert signals["timezone"] == "Asia/Shanghai"


def test_edge_header_tracker_uses_edge_ua_and_accept():
    browser = _load_browser_module()
    edge = browser.RequestHeaderTracker(browser="edge")
    chrome = browser.RequestHeaderTracker(browser="chrome")
    edge.navigate_to("https://example.com/a")
    edge.navigate_to("https://example.com/b")
    chrome.navigate_to("https://example.com/a")
    chrome.navigate_to("https://example.com/b")
    # 不指定 user_agent 时，RequestHeaderTracker 应自动注入浏览器默认 UA
    edge_headers = edge.build_ordered_headers()
    chrome_headers = chrome.build_ordered_headers()
    edge_values = dict(edge_headers)
    chrome_values = dict(chrome_headers)
    # Edge 应包含 Edg/，Chrome 应包含 Chrome/ 而不是 Edg/
    assert "Edg/" in edge_values["User-Agent"]
    assert "Chrome/" in chrome_values["User-Agent"]
    assert "Edg/" not in chrome_values["User-Agent"]
    # Edge 的 Accept-Language 应与 Chrome 略有差异（Edge 优先 en-US，而我们的配置可能不一样）
    # 实际我们的配置中 Edge 使用 "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7"
    assert edge_values.get("Accept-Language")
    # Referer 应为上一页
    assert edge_values["Referer"] == "https://example.com/a"
    assert chrome_values["Referer"] == "https://example.com/a"
    # Edge 与 Chrome 都是基于 Chromium，Sec-Fetch-* 头应出现
    for key in ("Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest"):
        assert key in edge_values, f"Edge 应有 {key}"
        assert key in chrome_values, f"Chrome 应有 {key}"


def test_safari_header_order_does_not_emit_sec_fetch():
    browser = _load_browser_module()
    safari = browser.RequestHeaderTracker(browser="safari")
    safari.navigate_to("https://example.com/list")
    safari.navigate_to("https://example.com/detail")
    headers = safari.build_ordered_headers(user_agent="Mozilla/5.0 Safari/605.1.15")
    keys = [k for k, _v in headers]
    # Safari 不发送 Sec-Fetch-Site / Sec-Fetch-Mode / Sec-Fetch-User / Sec-Fetch-Dest
    for key in (
        "Sec-Fetch-Site",
        "Sec-Fetch-Mode",
        "Sec-Fetch-User",
        "Sec-Fetch-Dest",
    ):
        assert key not in keys, f"Safari 不应发送 {key}"
    # Referer 应该等于上一页
    assert dict(headers)["Referer"] == "https://example.com/list"
    # Safari 的 Accept 不应该包含 image/avif（Safari 默认 Accept 与 Chrome 不同）
    assert "image/avif" not in dict(headers).get("Accept", "")


def test_get_browser_user_agent_returns_brand_specific_ua():
    browser = _load_browser_module()
    chrome_ua = browser.get_browser_user_agent("chrome")
    edge_ua = browser.get_browser_user_agent("edge")
    firefox_ua = browser.get_browser_user_agent("firefox")
    safari_ua = browser.get_browser_user_agent("safari")
    ios_safari_ua = browser.get_browser_user_agent("safari_ios")
    assert "Chrome/" in chrome_ua and "Edg/" not in chrome_ua
    assert "Edg/" in edge_ua
    assert "Firefox/" in firefox_ua or "Gecko/" in firefox_ua
    assert "Safari/" in safari_ua and "Chrome/" not in safari_ua
    assert "iPhone" in ios_safari_ua and "Safari/" in ios_safari_ua


def test_detect_browser_type_from_executable_path():
    browser = _load_browser_module()
    from pathlib import Path
    assert browser.detect_browser_type(Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")) == "edge"
    assert browser.detect_browser_type(Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")) == "chrome"
    assert browser.detect_browser_type(Path(r"C:\Program Files\Mozilla Firefox\firefox.exe")) == "firefox"
    assert browser.detect_browser_type(None) == "chrome"


def test_browser_context_options_accepts_browser_type_for_ua(monkeypatch):
    browser = _load_browser_module()
    # 用 monkeypatch 禁用 settings 中的全局 UA，确保 UA 来自 browser_type
    monkeypatch.setattr(browser.settings, "browser_user_agent", None)
    opts_edge = browser.browser_context_options(visible=True, browser_type="edge")
    assert "Edg/" in opts_edge["user_agent"]
    opts_safari = browser.browser_context_options(visible=True, browser_type="safari")
    assert "Safari/" in opts_safari["user_agent"]
    assert "Chrome/" not in opts_safari["user_agent"]


def test_fingerprint_config_random_supports_edge_and_safari_profiles():
    import tempfile, os, atexit
    from pathlib import Path
    temp_dir: tempfile.TemporaryDirectory | None = None
    if "CONTENT_HUB_DATABASE_URL" not in os.environ:
        temp_dir = tempfile.TemporaryDirectory()
        atexit.register(temp_dir.cleanup)
        os.environ.setdefault(
            "CONTENT_HUB_DATABASE_URL",
            f"sqlite:///{(Path(temp_dir.name) / 'test.db').as_posix()}",
        )
    from app.publishers import fingerprint_config as fp
    edge = fp.FingerprintConfig.random(profile="desktop_edge_1080p")
    assert edge.profile == "desktop_edge_1080p"
    assert edge.platform == "Win32"
    assert "Microsoft" in edge.webgl_vendor or "Intel" in edge.webgl_renderer
    safari = fp.FingerprintConfig.random(profile="desktop_safari_sonoma")
    assert safari.profile == "desktop_safari_sonoma"
    assert safari.platform == "MacIntel"
    assert "Apple" in safari.webgl_vendor
    ipad = fp.FingerprintConfig.random(profile="mobile_ipad_safari")
    assert ipad.profile == "mobile_ipad_safari"
    assert ipad.ontouchstart_present is True


def test_publishers_distinguish_login_redirects_from_creator_pages():
    browser = _load_browser_module()

    assert browser.DouyinPublisher()._is_publish_editor_url(
        "https://creator.douyin.com/creator-micro/content/upload"
    )
    assert not browser.DouyinPublisher()._is_publish_editor_url(
        "https://creator.douyin.com/login"
    )
    assert browser.XiaohongshuPublisher()._is_publish_editor_url(
        "https://creator.xiaohongshu.com/publish/publish"
    )
    assert not browser.XiaohongshuPublisher()._is_publish_editor_url(
        "https://www.xiaohongshu.com/login"
    )
    assert browser.BilibiliPublisher()._is_publish_editor_url(
        "https://member.bilibili.com/platform/upload/video/frame"
    )

    class LoginPage:
        url = "https://passport.bilibili.com/login"

    assert browser.BilibiliPublisher()._is_login_page(LoginPage())


def test_rate_limited_music_or_category_requests_do_not_abort_the_browser_job():
    import threading

    browser = _load_browser_module()
    publisher = browser.DouyinPublisher()
    publisher._risk_event = threading.Event()
    publisher._risk_message = ""

    class Request:
        resource_type = "xhr"

    class Response:
        status = 429
        request = Request()
        url = "https://creator.douyin.com/creator-micro/music/category"

    publisher._capture_risk_response(Response())
    assert not publisher._risk_event.is_set()


def test_douyin_topic_matching_prefers_normalized_exact_names_then_safe_fuzzy_matches():
    browser = _load_browser_module()
    publisher = browser.DouyinPublisher()

    # NFKC/case/spacing differences should still count as the same Chinese-English tag.
    assert publisher._topic_candidate_matches("#NIKKE AI绘画\n128.6万次播放", "nikke ai 绘画")
    assert publisher._topic_candidate_matches("摄影（话题）", "摄影")
    assert not publisher._topic_candidate_matches("摄影技巧 128.6万次播放", "摄影")

    similarity = publisher._topic_similarity("NIKKE胜利之神", "NIKKE胜利女神")
    assert similarity >= 0.84
    assert publisher._is_safe_fuzzy_topic_match("NIKKE胜利之神", "NIKKE胜利女神", similarity)
    # Two-character Chinese tags are too ambiguous for fuzzy auto-selection.
    assert not publisher._is_safe_fuzzy_topic_match("摄像", "摄影", publisher._topic_similarity("摄像", "摄影"))
    # An expanded topic is not a typo: it must remain an unresolved literal tag.
    assert not publisher._is_safe_fuzzy_topic_match(
        "时崎狂三cos假发", "时崎狂三cos",
        publisher._topic_similarity("时崎狂三cos假发", "时崎狂三cos"),
    )


def test_topic_text_is_inserted_atomically_to_avoid_prefix_auto_completion():
    browser = _load_browser_module()

    class Keyboard:
        def __init__(self):
            self.inserted: list[str] = []

        def insert_text(self, value: str) -> None:
            self.inserted.append(value)

    class Page:
        keyboard = Keyboard()

    class Editor:
        def type(self, *_args, **_kwargs):
            raise AssertionError("topic text must not be typed character by character")

    browser.DouyinPublisher._insert_topic_text(Page(), Editor(), "#cosplay")
    assert Page.keyboard.inserted == ["#cosplay"]


def test_topic_cursor_must_be_verified_at_editor_end_before_the_next_tag():
    browser = _load_browser_module()
    publisher = browser.XiaohongshuPublisher()
    publisher._check_runtime_pause = lambda *_args: None

    class Editor:
        def __init__(self, at_end: bool):
            self.at_end = at_end
            self.calls = 0

        def evaluate(self, _script):
            self.calls += 1
            return self.at_end

    assert publisher._place_topic_cursor_at_end(object(), Editor(True), None)
    assert not publisher._place_topic_cursor_at_end(object(), Editor(False), None)


def test_schedule_time_uses_configured_publishing_timezone(monkeypatch):
    from datetime import datetime, timezone

    browser = _load_browser_module()
    monkeypatch.setattr(browser.settings, "publish_day_timezone", "Asia/Shanghai")
    scheduled_at = datetime(2026, 8, 3, 1, 0, tzinfo=timezone.utc)
    assert browser.BrowserPublisher._format_schedule_time(scheduled_at) == "2026-08-03 09:00"


def test_xiaohongshu_schedule_switch_is_clicked_before_waiting_for_time_picker():
    browser = _load_browser_module()

    class Locator:
        def __init__(self, items):
            self.items = items

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class Control:
        def __init__(self):
            self.clicked = False

        def is_visible(self):
            return True

        def get_attribute(self, name):
            return "false" if name == "aria-checked" else None

        def click(self, **_kwargs):
            self.clicked = True

    control = Control()

    class Label:
        def is_visible(self):
            return True

        def locator(self, selector):
            if selector == "xpath=..":
                return self
            return Locator([control])

        def click(self, **_kwargs):
            raise AssertionError("the switch control should be preferred over its label")

    class Page:
        def get_by_text(self, text, exact):
            assert (text, exact) == ("定时发布", True)
            return Locator([Label()])

        def wait_for_timeout(self, _milliseconds):
            pass

    assert browser.XiaohongshuPublisher()._click_native_schedule_toggle(Page())
    assert control.clicked


def test_schedule_setup_error_becomes_a_manual_review_hint(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    browser = _load_browser_module()
    publisher = browser.XiaohongshuPublisher()
    publisher._check_runtime_pause = lambda *_args: None
    monkeypatch.setattr(publisher, "_click_native_schedule_toggle", lambda _page: False)
    snapshot = SimpleNamespace(scheduled_at=datetime(2026, 8, 3, 1, 0, tzinfo=timezone.utc))

    with pytest.raises(browser.NativeScheduleSetupError):
        publisher._apply_native_schedule(object(), snapshot, None)

    publisher._native_schedule_warning = "未找到定时发布开关"
    hint = publisher._schedule_review_hint(snapshot)
    assert "浏览器页面已保持打开" in hint
    assert "手动打开定时发布" in hint


def test_xiaohongshu_and_bilibili_use_the_same_topic_matcher():
    browser = _load_browser_module()
    for publisher in (browser.XiaohongshuPublisher(), browser.BilibiliPublisher()):
        assert publisher._topic_candidate_matches("#AI 绘画\n128.6万次浏览", "ai绘画")
        assert not publisher._topic_candidate_matches("AI绘画教程", "AI绘画")
        similarity = publisher._topic_similarity("原神Cosply", "原神Cosplay")
        assert publisher._is_safe_fuzzy_topic_match("原神Cosply", "原神Cosplay", similarity)


def test_every_publisher_selects_a_video_or_image_mode_before_upload():
    from types import SimpleNamespace

    browser = _load_browser_module()
    video = SimpleNamespace(assets=[SimpleNamespace(media_type="video")])
    image = SimpleNamespace(assets=[SimpleNamespace(media_type="image")])
    for publisher in (browser.DouyinPublisher(), browser.XiaohongshuPublisher(), browser.BilibiliPublisher()):
        assert publisher._upload_mode_labels(video)[0] == "发布视频"
        assert publisher._upload_mode_labels(image)[0] == "发布图文"


def test_topic_candidate_uses_visible_tag_text_not_a_reused_aria_label():
    browser = _load_browser_module()
    publisher = browser.DouyinPublisher()

    class Candidate:
        def is_visible(self):
            return True

        def inner_text(self, timeout):
            return "摄影技巧\n128.6万次播放"

        def get_attribute(self, name):
            return "摄影" if name == "aria-label" else None

    class Locator:
        def count(self):
            return 1

        def nth(self, index):
            return Candidate()

    class Page:
        def locator(self, selector):
            return Locator()

    candidates = publisher._visible_topic_candidates(Page())
    assert candidates[0][1] == "摄影技巧"
    assert not publisher._topic_candidate_matches(candidates[0][1], "摄影")
