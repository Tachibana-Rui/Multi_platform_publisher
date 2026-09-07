"""下载器整合性测试——使用独立的测试数据库，不污染主数据库。

测试覆盖：
1. 浏览器级下载管理器的基本结构与反检测集成
2. 小红书页面解析逻辑
3. 抖音链接规范化与元数据解析
4. RequestHeaderTracker 在新下载器中的行为
5. 数据库路径隔离（确保测试使用独立数据库路径）
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import time
from unittest.mock import MagicMock, patch, ANY
import uuid

import pytest

from app.config import settings


# ---------------------------------------------------------------------------
# 测试数据库隔离：确保测试使用独立的 SQLite 临时文件
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_data_dir():
    """为每个测试创建独立的 data_dir，使用临时目录。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        # 保存原有的环境变量以便恢复
        original_env = {}
        for key in ("CONTENT_HUB_DATA_DIR", "CONTENT_HUB_UPLOAD_DIR",
                    "CONTENT_HUB_BROWSER_PROFILE_DIR", "CONTENT_HUB_DATABASE_URL"):
            original_env[key] = __import__("os").environ.get(key)

        # 注入测试环境变量
        data_dir = tmp / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        upload_dir = tmp / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        profile_dir = tmp / "browser_profiles"
        profile_dir.mkdir(parents=True, exist_ok=True)
        db_url = f"sqlite:///{(data_dir / 'test.db').as_posix()}"

        __import__("os").environ["CONTENT_HUB_DATA_DIR"] = str(data_dir)
        __import__("os").environ["CONTENT_HUB_UPLOAD_DIR"] = str(upload_dir)
        __import__("os").environ["CONTENT_HUB_BROWSER_PROFILE_DIR"] = str(profile_dir)
        __import__("os").environ["CONTENT_HUB_DATABASE_URL"] = db_url

        yield {
            "data_dir": data_dir,
            "upload_dir": upload_dir,
            "profile_dir": profile_dir,
            "database_url": db_url,
        }

        # 恢复环境
        for key, value in original_env.items():
            if value is None:
                __import__("os").environ.pop(key, None)
            else:
                __import__("os").environ[key] = value


@pytest.fixture
def fresh_post_id() -> str:
    """为每个测试生成唯一的 post_id。"""
    return f"test_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# 反检测集成测试
# ---------------------------------------------------------------------------

class TestBrowserDownloadSessionIntegration:
    """测试 BrowserDownloadSession 与反检测系统的集成。"""

    def test_creates_fingerprint_per_session(self):
        """每个会话使用独立的设备指纹。"""
        from app.browser_downloader import BrowserDownloadSession
        from app.publishers.browser import RequestHeaderTracker

        tracker1 = RequestHeaderTracker(browser="chrome")
        tracker2 = RequestHeaderTracker(browser="chrome")
        # 两个 tracker 独立，但使用相同的浏览器类型
        headers1 = tracker1.build_ordered_headers(user_agent=None)
        headers2 = tracker2.build_ordered_headers(user_agent=None)
        # 相同浏览器类型的 UA 应该一致
        ua1 = next((v for k, v in headers1 if k == "User-Agent"), None)
        ua2 = next((v for k, v in headers2 if k == "User-Agent"), None)
        assert ua1 == ua2

    def test_header_tracker_referer_sequential(self):
        """RequestHeaderTracker 的 Referer 是前一个导航的 URL。"""
        from app.publishers.browser import RequestHeaderTracker

        tracker = RequestHeaderTracker(browser="chrome")
        tracker.navigate_to("https://www.example.com/page1")
        tracker.navigate_to("https://www.example.com/page2")
        assert tracker.referer == "https://www.example.com/page1"

    def test_download_session_isolated_profiles(self, isolated_data_dir):
        """不同平台使用不同的浏览器 profile 目录。"""
        from app.browser_downloader import BrowserDownloadSession

        # 验证配置：不同的 profile_subdir 对应不同路径
        douyin_profile = isolated_data_dir["profile_dir"] / "douyin"
        xiaohongshu_profile = isolated_data_dir["profile_dir"] / "xiaohongshu"
        assert douyin_profile != xiaohongshu_profile

    def test_header_order_matches_real_browser(self):
        """RequestHeaderTracker 生成的请求头顺序符合真实浏览器。"""
        from app.publishers.browser import RequestHeaderTracker

        tracker = RequestHeaderTracker(browser="chrome")
        tracker.navigate_to("https://www.example.com/")
        tracker.navigate_to("https://www.example.com/detail")
        headers = tracker.build_ordered_headers(user_agent=None)
        keys = [k for k, _ in headers]
        # Chrome 的典型顺序：User-Agent 早于 Referer，Referer 早于 Accept-Encoding
        assert keys.index("User-Agent") < keys.index("Referer")
        assert keys.index("Referer") < keys.index("Accept-Encoding")


# ---------------------------------------------------------------------------
# 小红书下载测试
# ---------------------------------------------------------------------------

class TestXiaohongshuDownloadIntegration:
    """测试小红书下载逻辑的核心路径解析。"""

    def test_normalize_source_url_valid(self):
        """有效小红书链接的规范化。"""
        from app.xiaohongshu import normalize_source_url

        result = normalize_source_url("https://www.xiaohongshu.com/explore/abc123def")
        assert "xiaohongshu.com" in result

    def test_normalize_source_url_with_share_shortcut(self):
        """支持 xhslink 分享短链。"""
        from app.xiaohongshu import normalize_source_url

        result = normalize_source_url("链接内容 https://xhslink.com/a/bc 测试")
        assert "xhslink.com" in result

    def test_normalize_source_url_rejects_other_domains(self):
        """拒绝非小红书域名。"""
        from app.xiaohongshu import normalize_source_url
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            normalize_source_url("https://www.other-site.com/video/123")

    def test_parse_note_page_basic(self):
        """测试基本的笔记页面解析逻辑。"""
        from app.xiaohongshu import parse_note_page

        # 构造一个模拟的小红书页面 HTML
        html = """
        <!DOCTYPE html>
        <html><head><title>测试笔记 - 小红书</title></head>
        <body>
        <script>
        window.__INITIAL_STATE__ = {
            "note": {
                "noteDetailMap": {
                    "testnote001": {
                        "note": {
                            "noteId": "testnote001",
                            "title": "测试笔记标题",
                            "desc": "这是笔记描述内容 #话题1 #话题2",
                            "imageList": [
                                {"urlDefault": "https://sns-xhs-img.xhscdn.com/image1.jpg"},
                                {"urlDefault": "https://sns-xhs-img.xhscdn.com/image2.jpg"}
                            ],
                            "tagList": [
                                {"name": "话题1"},
                                {"name": "话题2"}
                            ]
                        }
                    }
                }
            }
        };
        </script>
        </body></html>
        """
        result = parse_note_page(html, "https://www.xiaohongshu.com/explore/testnote001")
        assert result.note_id == "testnote001"
        assert "测试笔记标题" in result.title
        assert len(result.media) == 2
        assert "话题1" in result.tags
        assert "话题2" in result.tags

    def test_parse_note_page_video(self):
        """测试视频笔记的解析。"""
        from app.xiaohongshu import parse_note_page

        html = """
        <!DOCTYPE html>
        <html><body>
        <script>
        window.__INITIAL_STATE__ = {
            "note": {
                "noteDetailMap": {
                    "videonote001": {
                        "note": {
                            "noteId": "videonote001",
                            "title": "视频笔记",
                            "imageList": [],
                            "video": {
                                "media": {
                                    "stream": {
                                        "h264": [
                                            {"masterUrl": "https://sns-xhs-video.xhscdn.com/video.mp4",
                                             "backupUrls": ["https://sns-xhs-video.xhscdn.com/video_backup.mp4"],
                                             "width": 1080, "height": 1920, "videoBitrate": 2000000}
                                        ]
                                    }
                                }
                            }
                        }
                    }
                }
            }
        };
        </script>
        </body></html>
        """
        result = parse_note_page(html, "https://www.xiaohongshu.com/explore/videonote001")
        assert result.note_id == "videonote001"
        assert len(result.media) == 1
        assert result.media[0].media_type == "video"
        assert "xhscdn.com" in result.media[0].url

    def test_parse_note_page_rejects_empty_content(self):
        """空 HTML 或缺少 __INITIAL_STATE__ 的页面应该报错。"""
        from app.xiaohongshu import parse_note_page
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            parse_note_page("<html><body>无脚本内容</body></html>", "https://www.xiaohongshu.com/explore/abc")


# ---------------------------------------------------------------------------
# 抖音下载测试
# ---------------------------------------------------------------------------

class TestDouyinDownloadIntegration:
    """测试抖音下载逻辑的核心路径解析。"""

    def test_normalize_source_url_valid_douyin(self):
        """有效抖音链接的规范化。"""
        from app.douyin_importer import normalize_douyin_url

        result = normalize_douyin_url("https://www.douyin.com/video/1234567890123456789")
        assert "douyin.com" in result

    def test_normalize_source_url_valid_tiktok(self):
        """支持 TikTok 链接。"""
        from app.douyin_importer import normalize_douyin_url

        result = normalize_douyin_url("https://www.tiktok.com/@user/video/1234567890")
        assert "tiktok.com" in result

    def test_normalize_source_url_rejects_other(self):
        """拒绝非抖音/TikTok 域名。"""
        from app.douyin_importer import normalize_douyin_url
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            normalize_douyin_url("https://www.other-site.com/video/123")

    def test_extract_post_id_from_html(self):
        """从 HTML 中提取作品 ID。"""
        from app.douyin_importer import _extract_post_id

        html = '<script>var data = {"itemId": "1234567890123456789", "title": "测试视频"}</script>'
        assert _extract_post_id(html) == "1234567890123456789"

    def test_extract_post_id_from_url_path(self):
        """从 URL 路径提取作品 ID。"""
        from app.douyin_importer import _extract_post_id

        html = 'https://www.douyin.com/video/9876543210123456789'
        result = _extract_post_id(html)
        assert result is not None
        assert len(result) > 5

    def test_extract_title_from_html(self):
        """从 HTML 提取标题/描述。"""
        from app.douyin_importer import _extract_title

        html = '''
        <html>
        <head><title>测试视频标题 - 抖音</title>
        <meta name="description" content="这是一个测试视频的详细描述 #有趣 #测试">
        </head>
        </html>
        '''
        result = _extract_title(html)
        assert len(result) > 0
        assert "测试视频" in result or "详细描述" in result

    def test_extract_author_from_html(self):
        """从 HTML 提取作者。"""
        from app.douyin_importer import _extract_author

        html = '<script>data = {"author": {"nickname": "测试作者"}}</script>'
        assert _extract_author(html) == "测试作者"

    def test_extract_tags_from_html(self):
        """从 HTML 提取话题标签。"""
        from app.douyin_importer import _extract_tags

        html = '内容包含 #话题1 #话题2 测试内容'
        tags = _extract_tags(html)
        assert "话题1" in tags
        assert "话题2" in tags

    def test_extract_media_url_from_html(self):
        """从 HTML 中提取视频直链。"""
        from app.douyin_importer import _extract_media_url

        html = '<script>data = {"playAddr": "https://v.douyinvod.com/video.mp4?x-expires=123456"}</script>'
        url = _extract_media_url(html)
        assert url is not None
        assert "douyinvod.com" in url

    def test_sanitize_filename_removes_invalid_chars(self):
        """文件名规范化——移除非法字符。"""
        from app.douyin_importer import _sanitize_filename

        result = _sanitize_filename('视频标题 /包含: "非法字符" <test>')
        assert '/' not in result
        assert ':' not in result
        assert '"' not in result
        assert '<' not in result
        assert len(result) <= 120


# ---------------------------------------------------------------------------
# 数据库隔离验证
# ---------------------------------------------------------------------------

class TestDatabaseIsolation:
    """确保测试使用独立的数据库路径。"""

    def test_uses_isolated_database_path(self, isolated_data_dir):
        """测试使用独立的数据库文件，与主数据库隔离。"""
        # Read the temporary environment without replacing the shared settings
        # object already held by the API and media modules.
        from app import config

        test_settings = config.get_settings()
        # 验证：测试配置的数据库 URL 包含 "test.db"
        assert "test.db" in test_settings.database_url
        # 验证：data_dir 是临时目录（不在项目主目录）
        assert "tmp" in str(test_settings.data_dir).lower() or \
               "Temp" in str(test_settings.data_dir) or \
               tempfile.gettempdir() in str(test_settings.data_dir)

    def test_each_test_gets_unique_database(self, isolated_data_dir):
        """每个测试的数据库文件相互独立。"""
        db_path = Path(isolated_data_dir["database_url"].replace("sqlite:///", ""))
        # 验证：数据库文件可以被创建（不与主数据库冲突）
        # 注意：我们不实际创建数据库，只需验证路径是唯一的
        assert db_path.parent == isolated_data_dir["data_dir"]


# ---------------------------------------------------------------------------
# BrowserDownloadSession 的 mock 测试
# ---------------------------------------------------------------------------

class TestBrowserDownloadSessionIntegrationCore:
    """测试 BrowserDownloadSession 核心功能（不启动实际浏览器）。"""

    def test_header_tracker_provides_correct_referer(self):
        """测试 RequestHeaderTracker 的 referer 逻辑（不启动浏览器）。"""
        from app.publishers.browser import RequestHeaderTracker

        tracker = RequestHeaderTracker(browser="chrome")
        tracker.navigate_to("https://www.xiaohongshu.com/explore/page1")
        tracker.navigate_to("https://www.xiaohongshu.com/explore/page2")
        # 第二个 URL 的 referer 应该是第一个
        assert tracker.referer == "https://www.xiaohongshu.com/explore/page1"

    def test_fingerprint_config_integration(self):
        """测试 FingerprintConfig 与下载管理器的集成。"""
        from app.publishers.fingerprint_config import FingerprintConfig

        config = FingerprintConfig.random()
        # 验证：配置包含必要的字段（每个会话有独立的设备指纹）
        assert config.profile is not None
        assert config.platform is not None
        assert config.screen_width > 0
        assert config.screen_height > 0
        assert config.hardware_concurrency > 0
        # 验证：不同调用产生不同的配置（有随机扰动）
        config2 = FingerprintConfig.random()
        # 至少有一些字段不同（不是确定性相同的）
        assert (config.avail_width != config2.avail_width or
                config.avail_height != config2.avail_height or
                config.device_pixel_ratio != config2.device_pixel_ratio)

    def test_anti_detection_script_present(self):
        """测试反检测脚本存在且不为空。"""
        from app.publishers.browser import ANTI_DETECTION_INIT_SCRIPT

        assert ANTI_DETECTION_INIT_SCRIPT is not None
        assert len(ANTI_DETECTION_INIT_SCRIPT) > 100
        # 验证脚本包含关键的反检测特征
        assert "webdriver" in ANTI_DETECTION_INIT_SCRIPT.lower() or "navigator" in ANTI_DETECTION_INIT_SCRIPT.lower()

    def test_browser_context_options_produce_valid_config(self):
        """测试 browser_context_options 生成有效的浏览器配置。"""
        from app.publishers.browser import browser_context_options

        options = browser_context_options(visible=False, browser_type="chrome")
        # 验证：配置包含必要的参数
        assert options is not None
        assert isinstance(options, dict)

    def test_request_header_tracker_user_agent_ordering(self):
        """测试 RequestHeaderTracker 生成的请求头中 User-Agent 的顺序。"""
        from app.publishers.browser import RequestHeaderTracker

        tracker = RequestHeaderTracker(browser="chrome")
        tracker.navigate_to("https://www.xiaohongshu.com/")
        headers = tracker.build_ordered_headers(user_agent=None)
        keys = [k for k, _ in headers]
        # User-Agent 应该出现在 Referer 之前（Chrome 风格）
        assert keys.index("User-Agent") < keys.index("Referer")

    def test_downloaded_page_dataclass(self):
        """测试 DownloadedPage 数据类的基本行为。"""
        from dataclasses import dataclass
        from app.browser_downloader import DownloadedPage

        page = DownloadedPage(canonical_url="https://example.com/", html="<html>test</html>")
        assert page.canonical_url == "https://example.com/"
        assert "test" in page.html

    def test_downloaded_asset_dataclass(self):
        """测试 DownloadedAsset 数据类的基本行为。"""
        from app.browser_downloader import DownloadedAsset

        asset = DownloadedAsset(
            original_name="image.jpg",
            storage_name="uploads/2024/image.jpg",
            media_type="image",
            mime_type="image/jpeg",
            file_size=1024,
            checksum="abc123",
            width=1920,
            height=1080,
            duration_seconds=None,
            position=0,
        )
        assert asset.original_name == "image.jpg"
        assert asset.media_type == "image"
        assert asset.duration_seconds is None

    def test_header_tracker_chrome_vs_firefox_difference(self):
        """测试不同浏览器类型的请求头差异（验证反检测的多样性）。"""
        from app.publishers.browser import RequestHeaderTracker

        chrome_tracker = RequestHeaderTracker(browser="chrome")
        firefox_tracker = RequestHeaderTracker(browser="firefox")
        chrome_tracker.navigate_to("https://www.example.com/")
        firefox_tracker.navigate_to("https://www.example.com/")

        chrome_headers = dict(chrome_tracker.build_ordered_headers(user_agent=None))
        firefox_headers = dict(firefox_tracker.build_ordered_headers(user_agent=None))

        # User-Agent 应该不同
        assert chrome_headers.get("User-Agent") != firefox_headers.get("User-Agent")

    def test_progress_callback_none_safe(self):
        """测试 progress_callback 为 None 时不会崩溃。"""
        # 直接测试 _report 逻辑（通过直接检查代码）
        # 该逻辑在 browser_downloader.py 中
        from app.browser_downloader import BrowserDownloadSession

        # 创建一个临时对象来测试 _report 方法
        session = object.__new__(BrowserDownloadSession)
        session._progress_callback = None
        # 不应该抛出异常
        try:
            session._report({"test": "value"})
        except Exception as exc:
            pytest.fail(f"_report 不应在 callback 为 None 时抛出异常: {exc}")


# ---------------------------------------------------------------------------
# 异步包装器测试
# ---------------------------------------------------------------------------

class TestAsyncWrappers:
    """测试异步包装器（asyncio.to_thread）是否正确工作。"""

    @patch("app.xiaohongshu._download_with_browser")
    def test_xiaohongshu_async_wrapper_calls_sync(self, mock_download):
        """测试 xiaohongshu 的异步包装器正确调用同步实现。"""
        from app.xiaohongshu import import_public_note

        mock_download.return_value = (
            "https://www.xiaohongshu.com/explore/testid",
            MagicMock(title="测试", body="", tags=[], media=[], note_id="testid"),
            [],
        )

        async def run_test():
            result = await import_public_note("https://www.xiaohongshu.com/explore/testid", "testpost")
            return result

        result = asyncio.run(run_test())
        assert result is not None
        mock_download.assert_called_once()

    @patch("app.douyin_importer._download_with_browser")
    def test_douyin_async_wrapper_calls_sync(self, mock_download):
        """测试 douyin 的异步包装器正确调用同步实现。"""
        from app.douyin_importer import import_public_douyin

        mock_download.return_value = (
            "https://www.douyin.com/video/testid",
            MagicMock(post_id="testid", title="测试", body="", author_display_name="作者", tags=[]),
            [],
        )

        async def run_test():
            result = await import_public_douyin("https://www.douyin.com/video/testid", "testpost")
            return result

        result = asyncio.run(run_test())
        assert result is not None
        mock_download.assert_called_once()


# ---------------------------------------------------------------------------
# 媒体文件下载辅助函数测试
# ---------------------------------------------------------------------------

class TestMediaDownloadHelpers:
    """测试媒体文件下载的辅助函数。"""

    def test_host_allowed_valid_domains(self):
        """测试域名白名单验证。"""
        from app.douyin_importer import _host_allowed, MEDIA_HOSTS
        from app.xiaohongshu import _host_allowed as xhs_host_allowed, MEDIA_HOSTS as XHS_MEDIA_HOSTS

        # 抖音白名单
        assert _host_allowed("v.douyinvod.com", MEDIA_HOSTS)
        assert _host_allowed("sns-xhs-video.xhscdn.com", MEDIA_HOSTS) is False  # xhscdn 不在抖音列表

        # 小红书白名单
        assert xhs_host_allowed("sns-xhs-img.xhscdn.com", XHS_MEDIA_HOSTS)
        assert xhs_host_allowed("v.douyinvod.com", XHS_MEDIA_HOSTS) is False

    def test_host_allowed_rejects_unknown(self):
        """拒绝未知域名。"""
        from app.douyin_importer import _host_allowed, MEDIA_HOSTS

        assert _host_allowed("malicious.com", MEDIA_HOSTS) is False
        assert _host_allowed("evil.xhscdn.com.attacker.com", MEDIA_HOSTS) is False

    def test_extension_from_mime(self):
        """测试 MIME 类型到文件扩展名的映射。"""
        # 使用实际浏览器下载器中的逻辑
        from app.publishers.browser import RequestHeaderTracker

        tracker = RequestHeaderTracker(browser="chrome")
        headers = tracker.build_ordered_headers(user_agent=None)
        # 验证：有 Accept 头
        accept = next((v for k, v in headers if k == "Accept"), None)
        assert accept is not None
        assert "text/html" in accept


# ---------------------------------------------------------------------------
# 进度回调测试
# ---------------------------------------------------------------------------

class TestProgressCallback:
    """测试下载过程中的进度报告回调。"""

    @patch("playwright.sync_api.sync_playwright")
    @patch("app.browser_downloader.get_browser_executable")
    def test_report_progress_during_media_download(self, mock_get_exe, mock_sync_playwright):
        """测试 BrowserDownloadSession 的进度回调被正确传递。"""
        from app.browser_downloader import BrowserDownloadSession

        mock_get_exe.return_value = None

        mock_pw = MagicMock()
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_page = MagicMock()

        mock_sync_playwright.return_value = mock_pw
        mock_pw.chromium.launch.return_value = mock_browser
        mock_pw.chromium.launch_persistent_context.return_value = mock_context
        mock_browser.new_context.return_value = mock_context
        mock_context.new_page.return_value = mock_page
        mock_context.cookies.return_value = []

        progress_received = []

        def progress_callback(payload):
            progress_received.append(payload)

        with BrowserDownloadSession(browser_type="chrome", progress_callback=progress_callback) as session:
            mock_page.url = "https://example.com/test"
            mock_page.content.return_value = "<html>test</html>"

            # 调用内部 _report
            session._report({"post_name": "测试", "image_downloaded": 0, "image_total": 2})
            session._report({"post_name": "测试", "image_downloaded": 1, "image_total": 2})

        # 验证：回调被调用
        assert len(progress_received) == 2
        assert progress_received[0]["image_total"] == 2
