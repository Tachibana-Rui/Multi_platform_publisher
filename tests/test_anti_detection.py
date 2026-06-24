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
        ),
    )

    options = browser.browser_context_options(visible=True, accept_downloads=True)

    assert options["headless"] is False
    assert options["no_viewport"] is True
    assert options["accept_downloads"] is True
    assert options["ignore_default_args"] == ["--enable-automation"]
    assert options["args"][0] == "--start-maximized"
    assert options["args"].count("--disable-blink-features=AutomationControlled") == 1
    assert "--no-first-run" in options["args"]


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
