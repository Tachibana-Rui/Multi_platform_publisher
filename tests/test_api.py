import io
import atexit
import hashlib
import os
from pathlib import Path
import tempfile
import time

from PIL import Image, ImageDraw
from sqlalchemy import select


_temp_dir = tempfile.TemporaryDirectory()
os.environ.setdefault(
    "CONTENT_HUB_DATABASE_URL",
    f"sqlite:///{(Path(_temp_dir.name) / 'test.db').as_posix()}",
)
os.environ.setdefault("CONTENT_HUB_DATA_DIR", os.path.join(_temp_dir.name, "data"))
os.environ.setdefault("CONTENT_HUB_UPLOAD_DIR", os.path.join(_temp_dir.name, "uploads"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.database import engine  # noqa: E402
from app.content_matcher import parse_structured_folder  # noqa: E402
from app.xiaohongshu import normalize_source_url, parse_note_page  # noqa: E402
from app.douyin_importer import ParsedDouyinPost, normalize_douyin_url  # noqa: E402
from app.xiaohongshu import ParsedNote  # noqa: E402

atexit.register(engine.dispose)


def make_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), "#ef6a4c").save(buffer, format="PNG")
    return buffer.getvalue()


def make_fake_mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isomcontent-hub-video"


def wait_for_scan(client: TestClient, root_id: str, timeout: float = 5) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/api/library/roots/{root_id}/scan").json()
        if state["status"] == "completed":
            return state["result"]
        if state["status"] == "failed":
            raise AssertionError(state["error"])
        time.sleep(0.02)
    raise AssertionError("library scan did not finish")


def test_content_hub_crud_and_asset_flow():
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200

        created = client.post(
            "/api/posts",
            json={
                "title": "周末城市漫步",
                "body": "一份待整理的原始内容。",
                "tags": ["旅行", "摄影", "旅行"],
                "status": "draft",
                "content_type": "gallery",
            },
        )
        assert created.status_code == 201
        post = created.json()
        assert post["tags"] == ["旅行", "摄影"]

        uploaded = client.post(
            f"/api/posts/{post['id']}/assets",
            files=[("files", ("cover.png", make_png(), "image/png"))],
        )
        assert uploaded.status_code == 201
        post = uploaded.json()
        assert post["content_type"] == "image"
        assert post["assets"][0]["url"].endswith(f"/{post['id']}/originals/01_cover.png")
        assert post["assets"][0]["width"] == 32
        assert post["assets"][0]["height"] == 24

        updated = client.patch(
            f"/api/posts/{post['id']}",
            json={"status": "ready", "title": "城市漫步指南"},
        )
        assert updated.status_code == 200
        assert updated.json()["status"] == "ready"

        listing = client.get("/api/posts", params={"search": "指南", "status": "ready"})
        assert listing.status_code == 200
        assert len(listing.json()) == 1

        dashboard = client.get("/api/dashboard").json()
        assert dashboard["total_posts"] == 1
        assert dashboard["ready_posts"] == 1
        assert dashboard["total_assets"] == 1

        deleted = client.delete(f"/api/posts/{post['id']}")
        assert deleted.status_code == 204
        assert client.get("/api/posts").json() == []


def test_rejects_unsupported_asset():
    with TestClient(app) as client:
        post = client.post("/api/posts", json={"title": "测试内容"}).json()
        response = client.post(
            f"/api/posts/{post['id']}/assets",
            files=[("files", ("notes.txt", b"hello", "text/plain"))],
        )
        assert response.status_code == 415


def test_parses_public_xiaohongshu_note_content():
    note_id = "6411cf99000000001300b6d9"
    html = f"""
    <html><script>
    window.__INITIAL_STATE__ = {{
      "note": {{"noteDetailMap": {{"{note_id}": {{"note": {{
        "noteId": "{note_id}",
        "title": "城市散步路线",
        "desc": "沿着老街慢慢走。",
        "tagList": [{{"name": "城市漫步"}}, {{"name": "摄影"}}],
        "imageList": [
          {{"urlDefault": "https://sns-webpic-qc.xhscdn.com/a/first.jpg"}},
          {{"urlDefault": "https://sns-webpic-qc.xhscdn.com/a/second.jpg"}}
        ],
        "video": {{"media": {{"stream": {{"h264": [{{
          "masterUrl": "https://sns-video-bd.xhscdn.com/a/video.mp4",
          "width": 1080, "height": 1920, "videoBitrate": 4000000
        }}]}}}}}}
      }}}}}}}},
      "unused": undefined
    }};
    </script></html>
    """
    parsed = parse_note_page(html, f"https://www.xiaohongshu.com/explore/{note_id}")
    assert parsed.note_id == note_id
    assert parsed.title == "城市散步路线"
    assert parsed.body == "沿着老街慢慢走。"
    assert parsed.tags == ["城市漫步", "摄影"]
    assert [item.media_type for item in parsed.media] == ["video", "image", "image"]


def test_xiaohongshu_import_requires_rights_confirmation():
    with TestClient(app) as client:
        response = client.post(
            "/api/imports/xiaohongshu",
            json={"url": "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9", "confirm_rights": False},
        )
        assert response.status_code == 422
        assert "授权" in response.json()["detail"]


def test_only_accepts_xiaohongshu_source_hosts():
    assert normalize_source_url("复制 https://xhslink.com/a/example 看看") == "https://xhslink.com/a/example"
    try:
        normalize_source_url("http://127.0.0.1/private")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 422
    else:
        raise AssertionError("unsafe source URL was accepted")


def test_structured_library_search_and_compressed_image_matching():
    library_root = Path(_temp_dir.name) / "original-library"
    character_folder = library_root / "Alice+艾德+20260621"
    character_folder.mkdir(parents=True, exist_ok=True)
    original_path = character_folder / "portrait.jpg"

    original = Image.new("RGB", (1200, 900), "#eadfd5")
    draw = ImageDraw.Draw(original)
    draw.rectangle((80, 90, 510, 760), fill="#402f47")
    draw.ellipse((560, 110, 1080, 650), fill="#d96d5f")
    draw.polygon([(400, 800), (760, 340), (1100, 820)], fill="#517b72")
    draw.text((620, 700), "ED COSPLAY 2026", fill="white")
    original.save(original_path, "JPEG", quality=98)

    compressed_buffer = io.BytesIO()
    original.resize((600, 450), Image.Resampling.LANCZOS).save(
        compressed_buffer, "JPEG", quality=68, optimize=True
    )

    parsed = parse_structured_folder(character_folder.name)
    assert parsed == {
        "coser_name": "Alice",
        "character_name": "艾德",
        "shoot_date": "2026-06-21",
        "parse_status": "parsed",
    }

    with TestClient(app) as client:
        root = client.post("/api/library/roots", json={"path": str(library_root)}).json()
        scan = client.post(f"/api/library/roots/{root['id']}/scan")
        assert scan.status_code == 202
        duplicate_scan = client.post(f"/api/library/roots/{root['id']}/scan")
        assert duplicate_scan.status_code == 202
        assert wait_for_scan(client, root["id"])["assets"] == 1

        folders = client.get(
            "/api/library/folders",
            params={"coser_name": "Alice", "character_name": "艾德", "shoot_date": "2026-06-21"},
        ).json()
        assert len(folders) == 1
        assert folders[0]["asset_count"] == 1

        post = client.post(
            "/api/posts",
            json={"title": "艾德正片", "tags": ["艾德cos"], "content_type": "gallery"},
        ).json()
        uploaded = client.post(
            f"/api/posts/{post['id']}/assets",
            files=[("files", ("downloaded.jpg", compressed_buffer.getvalue(), "image/jpeg"))],
        ).json()

        result = client.post(
            f"/api/posts/{post['id']}/match-originals", json={}
        )
        assert result.status_code == 200
        payload = result.json()
        assert payload["folders"][0]["character_name"] == "艾德"
        assert payload["searched_assets"] == 1
        assert payload["matches"][0]["status"] == "matched"
        assert payload["matches"][0]["ssim_score"] > 0.94
        assert payload["matches"][0]["original_width"] == 1200
        assert payload["matches"][0]["original_url"].startswith(f"/media/{post['id']}/originals/")


def test_native_folder_picker_endpoint(monkeypatch):
    monkeypatch.setattr("app.main.pick_windows_folder", lambda: r"D:\Pictures\DCIM")
    with TestClient(app) as client:
        response = client.post("/api/system/pick-folder")
        assert response.status_code == 200
        assert response.json() == {"path": r"D:\Pictures\DCIM", "cancelled": False}


def test_llm_settings_are_encrypted_and_never_returned():
    from app.llm_settings import SETTINGS_PATH

    with TestClient(app) as client:
        saved = client.put(
            "/api/settings/llm",
            json={"api_key": "test-secret-api-key-1234", "model": "Doubao-Seed-2.0-lite"},
        )
        assert saved.status_code == 200
        payload = saved.json()
        assert payload["has_api_key"] is True
        assert payload["api_key_hint"] == "••••1234"
        assert payload["model"] == "doubao-seed-2-0-lite-260428"
        assert payload["enable_web_search"] is True
        assert payload["api_mode"] == "responses"
        assert "api_key" not in payload
        assert "test-secret-api-key-1234" not in SETTINGS_PATH.read_text(encoding="utf-8")

        disabled = client.put("/api/settings/llm", json={"enable_web_search": False})
        assert disabled.status_code == 200
        assert disabled.json()["enable_web_search"] is False
        assert disabled.json()["api_mode"] == "chat_completions"


def test_doubao_generation_uses_responses_web_search(monkeypatch):
    import asyncio
    import httpx

    from app.doubao import generate_copy
    from app.models import Post, Tag

    calls = []

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, headers, json):
            calls.append({"url": url, "headers": headers, "json": json})
            return httpx.Response(200, json={
                "output": [
                    {"type": "web_search_call", "status": "completed"},
                    {
                        "type": "message",
                        "content": [{
                            "type": "output_text",
                            "text": '{"title":"趋势标题","body":"结合最新趋势的正文。","tags":["趋势","豆包","内容创作","小红书","灵感"]}',
                        }],
                    },
                ],
            })

    monkeypatch.setattr("app.doubao.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.doubao.get_private_settings", lambda: {
        "model": "doubao-seed-2-0-lite-260428",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": "test-key",
        "enable_web_search": True,
    })

    post = Post(title="Seed 2.0 最新玩法", tags=[Tag(name="豆包")])
    result = asyncio.run(generate_copy(post, "xiaohongshu", [], "结合最近趋势生成文案"))

    assert result["title"] == "趋势标题"
    assert result["model"] == "doubao-seed-2-0-lite-260428 · Web Search"
    assert calls[0]["url"] == "https://ark.cn-beijing.volces.com/api/v3/responses"
    assert calls[0]["headers"]["Authorization"] == "Bearer test-key"
    assert calls[0]["json"]["tools"] == [{"type": "web_search"}]
    assert calls[0]["json"]["text"]["format"]["type"] == "json_object"
    assert calls[0]["json"]["input"][1]["content"][0] == {
        "type": "input_text",
        "text": "结合最近趋势生成文案",
    }


def test_platform_version_copies_source_and_can_regenerate(monkeypatch):
    calls = []

    async def fake_generate(post, platform, assets, custom_prompt=None):
        calls.append({"post": post.id, "platform": platform, "assets": len(assets), "prompt": custom_prompt})
        number = len(calls)
        return {
            "title": f"豆包标题 {number}",
            "body": f"豆包正文 {number}",
            "prompt": custom_prompt or f"生成 NIKKE 艾德 cos 抖音 标题和文案。",
            "model": "Doubao-Seed-2.0-lite",
        }

    monkeypatch.setattr("app.main.generate_copy", fake_generate)
    with TestClient(app) as client:
        post = client.post(
            "/api/posts",
            json={"title": "NIKKE 艾德兔女郎", "body": "原始正文", "tags": ["NIKKE", "艾德", "兔女郎cos", "二次元"]},
        ).json()
        uploaded = client.post(
            f"/api/posts/{post['id']}/assets",
            files=[("files", ("reference.png", make_png(), "image/png"))],
        ).json()

        assert client.get(f"/api/posts/{post['id']}/platform-versions").json() == []
        version = client.get(f"/api/posts/{post['id']}/platform-versions/douyin")
        assert version.status_code == 200
        version_data = version.json()
        assert version_data["title"] == "NIKKE 艾德兔女郎"
        assert version_data["body"] == "原始正文"
        assert version_data["content_source"] == "copied"
        assert version_data["selected_asset_ids"] == [uploaded["assets"][0]["id"]]
        assert version_data["suggested_prompt"] == "生成 NIKKE 艾德 cos 抖音 标题和文案，并生成5个相关标签追加在正文末尾。"
        available = client.get(f"/api/posts/{post['id']}/platform-versions").json()
        assert [item["platform"] for item in available] == ["douyin"]

        first = client.post(
            f"/api/posts/{post['id']}/platform-versions/douyin/generate",
            json={"selected_asset_ids": version_data["selected_asset_ids"]},
        ).json()
        assert first["title"] == "豆包标题 1"
        assert first["generation_count"] == 1
        assert first["content_source"] == "llm"

        second = client.post(
            f"/api/posts/{post['id']}/platform-versions/douyin/generate",
            json={"selected_asset_ids": version_data["selected_asset_ids"], "custom_prompt": "换一种轻松语气"},
        ).json()
        assert second["title"] == "豆包标题 2"
        assert second["generation_count"] == 2
        assert calls[1]["prompt"] == "换一种轻松语气"


def test_untitled_content_stays_blank_until_user_generates():
    with TestClient(app) as client:
        post = client.post("/api/posts", json={"tags": ["NIKKE", "艾德"]})
        assert post.status_code == 201
        assert post.json()["title"] == ""
        version = client.get(
            f"/api/posts/{post.json()['id']}/platform-versions/douyin"
        ).json()
        assert version["title"] == ""
        assert version["body"] == ""
        assert version["content_source"] == "copied"


def test_video_platform_version_defaults_to_single_video_and_rejects_mixed_media():
    with TestClient(app) as client:
        post = client.post(
            "/api/posts",
            json={"title": "视频作品", "body": "视频正文", "content_type": "video"},
        ).json()
        post = client.post(
            f"/api/posts/{post['id']}/assets",
            files=[
                ("files", ("clip.mp4", make_fake_mp4(), "video/mp4")),
                ("files", ("cover.png", make_png(), "image/png")),
            ],
        ).json()
        video_id = next(asset["id"] for asset in post["assets"] if asset["media_type"] == "video")
        image_id = next(asset["id"] for asset in post["assets"] if asset["media_type"] == "image")

        version = client.get(f"/api/posts/{post['id']}/platform-versions/douyin")
        assert version.status_code == 200
        assert version.json()["selected_asset_ids"] == [video_id]

        mixed = client.put(
            f"/api/posts/{post['id']}/platform-versions/douyin",
            json={"title": "视频作品", "body": "视频正文", "selected_asset_ids": [video_id, image_id]},
        )
        assert mixed.status_code == 422
        assert "混合图片和视频" in mixed.json()["detail"]


def test_publication_snapshots_version_and_requires_final_review(monkeypatch):
    started = []
    confirmed = []
    monkeypatch.setattr("app.main.publication_agent.start", lambda publication_id: started.append(publication_id) or True)
    monkeypatch.setattr("app.main.publication_agent.confirm", lambda publication_id: confirmed.append(publication_id) or True)

    with TestClient(app) as client:
        post = client.post(
            "/api/posts",
            json={"title": "发布快照", "body": "准备发送到抖音"},
        ).json()
        post = client.post(
            f"/api/posts/{post['id']}/assets",
            files=[("files", ("publish.png", make_png(), "image/png"))],
        ).json()
        asset_id = post["assets"][0]["id"]
        saved = client.put(
            f"/api/posts/{post['id']}/platform-versions/douyin",
            json={"title": "抖音标题", "body": "抖音正文", "selected_asset_ids": [asset_id]},
        )
        assert saved.status_code == 200

        created = client.post(
            "/api/publications",
            json={"post_id": post["id"], "platform": "douyin", "visibility": "private"},
        )
        assert created.status_code == 201
        publication = created.json()
        assert publication["title"] == "抖音标题"
        assert publication["body"] == "抖音正文"
        assert publication["visibility"] == "private"
        assert publication["asset_ids"] == [asset_id]
        assert started == [publication["id"]]

        from app.database import SessionLocal
        from app.models import PlatformPublication

        with SessionLocal() as db:
            record = db.get(PlatformPublication, publication["id"])
            record.status = "review_pending"
            db.commit()

        accepted = client.post(f"/api/publications/{publication['id']}/confirm")
        assert accepted.status_code == 200
        assert confirmed == [publication["id"]]

        from app.publish_agent import publication_agent

        publication_agent._sync_content(publication["id"], "浏览器修改标题", "浏览器修改正文")
        synced = client.get(f"/api/posts/{post['id']}/platform-versions/douyin").json()
        assert synced["title"] == "浏览器修改标题"
        assert synced["body"] == "浏览器修改正文"
        assert synced["content_source"] == "browser"

        with SessionLocal() as db:
            record = db.get(PlatformPublication, publication["id"])
            record.status = "published"
            db.commit()
        already_done = client.post(f"/api/publications/{publication['id']}/confirm")
        assert already_done.status_code == 200
        assert already_done.json()["already_published"] is True


def test_publication_rejects_task_without_selected_media(monkeypatch):
    monkeypatch.setattr("app.main.publication_agent.start", lambda _publication_id: True)
    with TestClient(app) as client:
        post = client.post("/api/posts", json={"title": "没有素材"}).json()
        response = client.post(
            "/api/publications",
            json={"post_id": post["id"], "platform": "xiaohongshu"},
        )
        assert response.status_code == 422
        assert "选择素材" in response.json()["detail"]


def test_account_management_status_check_and_login(monkeypatch):
    checks = []
    logins = []
    monkeypatch.setattr("app.main.account_manager.check_all", lambda: checks.append(True))
    monkeypatch.setattr(
        "app.main.account_manager.start",
        lambda platform, visible: logins.append((platform, visible)) or True,
    )
    with TestClient(app) as client:
        accounts = client.get("/api/accounts")
        assert accounts.status_code == 200
        assert [item["platform"] for item in accounts.json()] == ["douyin", "xiaohongshu", "bilibili"]

        check = client.post("/api/accounts/check")
        assert check.status_code == 202
        assert checks == [True]

        login = client.post("/api/accounts/xiaohongshu/login")
        assert login.status_code == 202
        assert logins == [("xiaohongshu", True)]

        unsupported = client.post("/api/accounts/kuaishou/login")
        assert unsupported.status_code == 422


def test_manual_folder_match_bypasses_tag_routing():
    manual_library = Path(_temp_dir.name) / "manual-library"
    manual_folder = manual_library / "manual-originals"
    manual_folder.mkdir(parents=True, exist_ok=True)
    original_path = manual_folder / "manual-source.jpg"
    original = Image.new("RGB", (900, 1200), "#d8c8ba")
    draw = ImageDraw.Draw(original)
    draw.rectangle((70, 80, 420, 1050), fill="#3d506a")
    draw.ellipse((450, 130, 820, 600), fill="#cc685d")
    original.save(original_path, "JPEG", quality=98)
    compressed = io.BytesIO()
    original.resize((450, 600), Image.Resampling.LANCZOS).save(compressed, "JPEG", quality=65)

    with TestClient(app) as client:
        root = client.post("/api/library/roots", json={"path": str(manual_library)}).json()
        client.post(f"/api/library/roots/{root['id']}/scan")
        assert wait_for_scan(client, root["id"])["assets"] == 1
        post = client.post(
            "/api/posts", json={"title": "手动匹配", "tags": ["完全不相关角色"]}
        ).json()
        post = client.post(
            f"/api/posts/{post['id']}/assets",
            files=[("files", ("compressed.jpg", compressed.getvalue(), "image/jpeg"))],
        ).json()
        automatic = client.post(f"/api/posts/{post['id']}/match-originals", json={}).json()
        assert automatic["searched_assets"] == 0
        assert automatic["matches"][0]["status"] == "unmatched"

        manual = client.post(
            f"/api/posts/{post['id']}/match-originals/manual",
            json={"path": str(manual_folder)},
        )
        assert manual.status_code == 200
        payload = manual.json()
        assert payload["manual_folder"] == str(manual_folder.resolve())
        assert payload["searched_assets"] == 1
        assert payload["matches"][0]["status"] == "matched"


def test_storage_location_can_be_changed_without_breaking_media(monkeypatch):
    from app.config import settings

    source = settings.upload_dir
    sample = source / "storage-test" / "sample.bin"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_bytes(b"content-hub-storage")
    target = Path(_temp_dir.name) / "custom-storage"
    monkeypatch.delenv("CONTENT_HUB_UPLOAD_DIR", raising=False)
    monkeypatch.setattr(settings, "upload_dir", source)

    with TestClient(app) as client:
        changed = client.put("/api/settings/storage", json={"path": str(target)})
        assert changed.status_code == 200
        assert changed.json()["path"] == str(target.resolve())
        assert (target / "storage-test" / "sample.bin").read_bytes() == b"content-hub-storage"
        served = client.get("/media/storage-test/sample.bin")
        assert served.status_code == 200
        assert served.content == b"content-hub-storage"


def _fake_import_asset(post_id: str) -> list[dict]:
    from app.config import settings

    target = settings.upload_dir / post_id / "downloads"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "import.png"
    path.write_bytes(make_png())
    return [{
        "original_name": "import.png",
        "storage_name": f"{post_id}/downloads/import.png",
        "media_type": "image",
        "mime_type": "image/png",
        "file_size": path.stat().st_size,
        "checksum": "a" * 64,
        "width": 32,
        "height": 24,
        "duration_seconds": None,
        "position": 0,
    }]


def test_batch_xiaohongshu_import_accepts_spaces_newlines_and_duplicates(monkeypatch):
    async def fake_import(url, post_id):
        note_id = url.rsplit("/", 1)[-1]
        return url, ParsedNote(note_id, f"笔记 {note_id}", "正文", ["批量"], []), _fake_import_asset(post_id)

    monkeypatch.setattr("app.main.import_public_note", fake_import)
    first = "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9"
    second = "https://www.xiaohongshu.com/explore/6411cf99000000001300b6da"
    with TestClient(app) as client:
        response = client.post("/api/imports/batch", json={
            "platform": "xiaohongshu",
            "text": f"{first}  {second}\n{first}",
            "confirm_rights": True,
        })
        assert response.status_code == 207
        payload = response.json()
        assert payload["imported"] == 2
        assert payload["skipped"] == 1
        assert payload["failed"] == 0


def test_batch_douyin_import_keeps_success_when_another_link_fails(monkeypatch):
    async def fake_import(url, post_id):
        if url.endswith("2"):
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail="模拟无法解析")
        return url, ParsedDouyinPost("1", "抖音作品", "抖音正文", ["抖音"]), _fake_import_asset(post_id)

    monkeypatch.setattr("app.main.import_public_douyin", fake_import)
    assert normalize_douyin_url("复制 https://v.douyin.com/example/ 看看") == "https://v.douyin.com/example/"
    with TestClient(app) as client:
        response = client.post("/api/imports/batch", json={
            "platform": "douyin",
            "text": "https://www.douyin.com/video/1\nhttps://www.douyin.com/video/2",
            "confirm_rights": True,
        })
        assert response.status_code == 207
        payload = response.json()
        assert payload["imported"] == 1
        assert payload["failed"] == 1
        assert len(client.get("/api/posts", params={"search": "抖音作品"}).json()) == 1


def test_wechat_moments_publication_is_removed():
    with TestClient(app) as client:
        post = client.post("/api/posts", json={"title": "不再发布朋友圈"}).json()
        version = client.get(
            f"/api/posts/{post['id']}/platform-versions/wechat_moments"
        )
        assert version.status_code == 422
        created = client.post("/api/publications", json={
            "post_id": post["id"], "platform": "wechat_moments", "visibility": "friends",
        })
        assert created.status_code == 422


def test_batch_import_job_reports_post_and_image_progress(monkeypatch):
    async def fake_import(url, post_id, progress_callback=None):
        if progress_callback:
            progress_callback({"post_name": "进度测试作品", "image_downloaded": 0, "image_total": 1})
        assets = _fake_import_asset(post_id)
        if progress_callback:
            progress_callback({"post_name": "进度测试作品", "image_downloaded": 1, "image_total": 1})
        return url, ParsedNote("progress-note", "进度测试作品", "正文", ["进度"], []), assets

    monkeypatch.setattr("app.main.import_public_note", fake_import)
    with TestClient(app) as client:
        created = client.post("/api/imports/batch-jobs", json={
            "platform": "xiaohongshu",
            "text": "https://www.xiaohongshu.com/explore/progress0001",
            "confirm_rights": True,
        })
        assert created.status_code == 202
        job_id = created.json()["id"]
        deadline = time.time() + 3
        while time.time() < deadline:
            job = client.get(f"/api/imports/batch-jobs/{job_id}").json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert job["status"] == "completed"
        assert job["current_name"] == "进度测试作品"
        assert job["current_index"] == 1
        assert job["image_downloaded"] == 1
        assert job["image_total"] == 1
        assert job["progress"] == 100


def test_multi_platform_batch_publication_and_published_metadata(monkeypatch):
    started = []
    monkeypatch.setattr(
        "app.main.publication_agent.start",
        lambda publication_id: started.append(publication_id) or True,
    )
    with TestClient(app) as client:
        post = client.post("/api/posts", json={"title": "多平台批量发布", "body": "批量正文"}).json()
        post = client.post(
            f"/api/posts/{post['id']}/assets",
            files=[("files", ("batch-publish.png", make_png(), "image/png"))],
        ).json()
        response = client.post("/api/publications/batch", json={
            "post_id": post["id"],
            "platforms": ["douyin", "xiaohongshu", "bilibili"],
            "visibility": "private",
        })
        assert response.status_code == 207
        publications = response.json()["created"]
        assert len(publications) == 3
        assert len(started) == 3
        assert {item["platform"] for item in publications} == {"douyin", "xiaohongshu", "bilibili"}
        assert all(item["progress"] == 5 for item in publications)
        assert all(item["post_title"] == "多平台批量发布" for item in publications)
        assert all(item["cover_url"] == post["assets"][0]["url"] for item in publications)

        from app.database import SessionLocal
        from app.models import PlatformPublication

        with SessionLocal() as db:
            published = db.get(PlatformPublication, publications[0]["id"])
            published.status = "published"
            published.published_at = published.updated_at
            db.commit()
        listing = client.get("/api/publications", params={"status": "published"}).json()
        record = next(item for item in listing if item["id"] == publications[0]["id"])
        assert record["post_title"] == "多平台批量发布"
        assert record["progress"] == 100
        assert record["published_at"] is not None


def test_multi_platform_batch_video_publication_uses_video_asset(monkeypatch):
    started = []
    monkeypatch.setattr(
        "app.main.publication_agent.start",
        lambda publication_id: started.append(publication_id) or True,
    )
    with TestClient(app) as client:
        post = client.post(
            "/api/posts",
            json={"title": "多平台视频发布", "body": "视频批量正文", "content_type": "video"},
        ).json()
        post = client.post(
            f"/api/posts/{post['id']}/assets",
            files=[
                ("files", ("multi-platform.mp4", make_fake_mp4(), "video/mp4")),
                ("files", ("video-cover.png", make_png(), "image/png")),
            ],
        ).json()
        video = next(asset for asset in post["assets"] if asset["media_type"] == "video")
        response = client.post("/api/publications/batch", json={
            "post_id": post["id"],
            "platforms": ["douyin", "xiaohongshu", "bilibili"],
            "visibility": "public",
        })
        assert response.status_code == 207
        publications = response.json()["created"]
        assert len(publications) == 3
        assert len(started) == 3
        assert {item["platform"] for item in publications} == {"douyin", "xiaohongshu", "bilibili"}
        assert all(item["asset_ids"] == [video["id"]] for item in publications)
        assert all(item["cover_media_type"] == "video" for item in publications)
        assert all(item["cover_url"] == video["url"] for item in publications)


def test_publication_record_can_be_marked_and_deleted(monkeypatch):
    monkeypatch.setattr("app.main.publication_agent.start", lambda _publication_id: True)
    with TestClient(app) as client:
        post = client.post("/api/posts", json={"title": "人工修正发布状态"}).json()
        post = client.post(
            f"/api/posts/{post['id']}/assets",
            files=[("files", ("manual-status.png", make_png(), "image/png"))],
        ).json()
        publication = client.post("/api/publications", json={
            "post_id": post["id"], "platform": "douyin",
        }).json()

        active_delete = client.delete(f"/api/publications/{publication['id']}")
        assert active_delete.status_code == 409

        from app.database import SessionLocal
        from app.models import PlatformPublication

        with SessionLocal() as db:
            record = db.get(PlatformPublication, publication["id"])
            record.status = "failed"
            record.error_message = "平台已发布但回传检测失败"
            db.commit()

        published = client.post(
            f"/api/publications/{publication['id']}/mark-published"
        )
        assert published.status_code == 200
        assert published.json()["status"] == "published"
        assert published.json()["published_at"] is not None
        assert published.json()["error_message"] is None

        unpublished = client.post(
            f"/api/publications/{publication['id']}/mark-unpublished"
        )
        assert unpublished.status_code == 200
        assert unpublished.json()["status"] == "unpublished"
        assert unpublished.json()["published_at"] is None
        assert unpublished.json()["progress"] == 100

        deleted = client.delete(f"/api/publications/{publication['id']}")
        assert deleted.status_code == 204
        assert client.get(f"/api/publications/{publication['id']}").status_code == 404


def test_doubao_copy_appends_exactly_five_tags():
    from app.doubao import _parse_copy

    result = _parse_copy(
        '{"title":"夜景人像","body":"今晚的光影很温柔。","tags":["夜景人像","城市摄影","氛围感","夜拍","摄影分享"]}',
        platform="douyin",
    )
    assert result["tags"] == ["夜景人像", "城市摄影", "氛围感", "夜拍", "摄影分享"]
    assert result["body"].endswith("#夜景人像 #城市摄影 #氛围感 #夜拍 #摄影分享")


def test_legacy_media_files_migrate_to_originals_and_downloads():
    from app.config import settings
    from app.database import SessionLocal
    from app.media_storage import migrate_media_layout
    from app.models import MediaAsset

    with TestClient(app) as client:
        manual = client.post("/api/posts", json={"title": "旧本地素材", "source_platform": "manual"}).json()
        downloaded = client.post("/api/posts", json={"title": "旧下载素材", "source_platform": "xiaohongshu"}).json()

    payload = make_png()
    checksum = hashlib.sha256(payload).hexdigest()
    manual_old = settings.upload_dir / manual["id"] / "legacy-manual.png"
    download_old = settings.upload_dir / downloaded["id"] / "legacy-download.png"
    manual_old.parent.mkdir(parents=True, exist_ok=True)
    download_old.parent.mkdir(parents=True, exist_ok=True)
    manual_old.write_bytes(payload)
    download_old.write_bytes(payload)
    with SessionLocal() as db:
        db.add_all([
            MediaAsset(
                post_id=manual["id"], original_name="portrait：source.png",
                storage_name=f"{manual['id']}/legacy-manual.png", media_type="image",
                mime_type="image/png", file_size=len(payload), checksum=checksum,
                width=32, height=24, position=0,
            ),
            MediaAsset(
                post_id=downloaded["id"], original_name="download.png",
                storage_name=f"{downloaded['id']}/legacy-download.png", media_type="image",
                mime_type="image/png", file_size=len(payload), checksum=checksum,
                width=32, height=24, position=0,
            ),
        ])
        db.commit()

    result = migrate_media_layout()
    assert result["moved_assets"] == 2
    with SessionLocal() as db:
        manual_asset = db.scalar(select(MediaAsset).where(MediaAsset.post_id == manual["id"]))
        download_asset = db.scalar(select(MediaAsset).where(MediaAsset.post_id == downloaded["id"]))
        assert manual_asset.storage_name == f"{manual['id']}/originals/01_portrait：source.png"
        assert download_asset.storage_name == f"{downloaded['id']}/downloads/legacy-download.png"
        assert (settings.upload_dir / manual_asset.storage_name).is_file()
        assert (settings.upload_dir / download_asset.storage_name).is_file()
    assert not manual_old.exists()
    assert not download_old.exists()


def test_douyin_prefill_separates_topics_and_mentions_for_platform_selection():
    from app.publishers.base import PublishSnapshot
    from app.publishers.browser import (
        DouyinPublisher,
        XiaohongshuPublisher,
        split_interactive_tokens,
    )

    snapshot = PublishSnapshot(
        id="publication", post_id="post", platform="douyin", visibility="public",
        title="标题", body="普通正文 @Alice @alice\n\n#摄影 ＃夜景 #摄影", assets=[],
    )
    hashtags, mentions = split_interactive_tokens(snapshot.body)
    assert hashtags == ["摄影", "夜景"]
    assert mentions == ["Alice"]

    publisher = DouyinPublisher()
    assert publisher._body_for_prefill(snapshot) == "普通正文"
    publisher._bound_hashtags = ["摄影"]
    publisher._unresolved_hashtags = ["夜景"]
    message = publisher._review_message(snapshot, "已设置为公开可见")
    assert "已自动关联抖音话题：#摄影" in message
    assert "未找到精确候选" in message
    assert "@Alice" in message
    assert "下拉列表确认" in message

    message = XiaohongshuPublisher()._review_message(snapshot, "已设置为公开可见")
    assert "@Alice" in message
    assert "#摄影" in message
    assert "点击下拉候选" in message


def test_douyin_topic_candidate_requires_an_exact_name_boundary():
    from app.publishers.browser import DouyinPublisher

    assert DouyinPublisher._topic_candidate_matches("#摄影 128.6亿次播放", "摄影")
    assert DouyinPublisher._topic_candidate_matches("摄影（话题）", "摄影")
    assert not DouyinPublisher._topic_candidate_matches("摄影技巧", "摄影")


def test_closed_douyin_page_is_treated_as_a_cancelled_publication():
    import threading

    from app.publishers.base import PublicationCancelled
    from app.publishers.browser import DouyinPublisher

    class ClosedPage:
        @staticmethod
        def is_closed():
            return True

    publisher = DouyinPublisher()
    publisher._page_closed_event = threading.Event()
    try:
        publisher._ensure_page_available(ClosedPage())
    except PublicationCancelled as exc:
        assert "可以直接重试" in str(exc)
    else:
        raise AssertionError("closed platform page should cancel the publication")

    assert publisher._is_publish_editor_url(
        "https://creator.douyin.com/creator-micro/content/upload"
    )
    assert publisher._is_publish_editor_url(
        "https://creator.douyin.com/creator-micro/content/post/video"
    )
    assert not publisher._is_publish_editor_url(
        "https://creator.douyin.com/creator-micro/content/manage"
    )


def test_publish_agent_releases_platform_lock_after_page_cancellation(monkeypatch):
    import threading

    from app.database import SessionLocal
    from app.models import PlatformPublication
    from app.publish_agent import publication_agent
    from app.publishers.base import PublicationCancelled
    from app.publishers.browser import PLATFORM_BROWSER_LOCKS

    monkeypatch.setattr("app.main.publication_agent.start", lambda _publication_id: True)
    with TestClient(app) as client:
        post = client.post("/api/posts", json={"title": "关闭发布窗口"}).json()
        post = client.post(
            f"/api/posts/{post['id']}/assets",
            files=[("files", ("cancel-window.png", make_png(), "image/png"))],
        ).json()
        publication = client.post("/api/publications", json={
            "post_id": post["id"], "platform": "douyin",
        }).json()

    class CancelledPublisher:
        @staticmethod
        def validate(_snapshot):
            return []

        @staticmethod
        def execute(*_args, **_kwargs):
            raise PublicationCancelled("抖音发布页已关闭，任务已取消，可以直接重试")

    monkeypatch.setattr("app.publish_agent.get_publisher", lambda _platform: CancelledPublisher())
    platform_lock = PLATFORM_BROWSER_LOCKS["douyin"]
    assert not platform_lock.locked()
    publication_agent._run(publication["id"], threading.Event(), threading.Event())
    assert not platform_lock.locked()
    with SessionLocal() as db:
        record = db.get(PlatformPublication, publication["id"])
        assert record.status == "cancelled"
        assert "可以直接重试" in record.error_message


def test_publication_daily_limit_is_enforced(monkeypatch):
    from datetime import datetime, timezone

    from app.config import settings
    from app.database import SessionLocal
    from app.models import PlatformPublication
    from app.publish_agent import publication_agent
    from app.publishers.base import PublishSnapshot

    monkeypatch.setattr(settings, "daily_publish_limit", 1)
    monkeypatch.setattr(settings, "publish_day_timezone", "UTC")

    with TestClient(app) as client:
        post = client.post("/api/posts", json={"title": "每日上限"}).json()

    platform = "limit_test"
    with SessionLocal() as db:
        db.add(PlatformPublication(
            post_id=post["id"],
            platform=platform,
            status="published",
            published_at=datetime.now(timezone.utc),
            logs_json="[]",
        ))
        db.commit()

    snapshot = PublishSnapshot(
        id="daily-limit",
        post_id=post["id"],
        platform=platform,
        visibility="public",
        title="标题",
        body="正文",
        assets=[],
    )
    try:
        publication_agent._enforce_daily_limit("daily-limit", snapshot, "测试平台")
    except RuntimeError as exc:
        assert "每日发布上限" in str(exc)
    else:
        raise AssertionError("daily publication limit should stop the task")


def test_batch_import_pause_detection():
    from fastapi import HTTPException

    from app.main import should_pause_import_batch

    assert should_pause_import_batch(HTTPException(status_code=429, detail="Too Many Requests"))
    assert should_pause_import_batch(HTTPException(status_code=422, detail="触发平台验证码"))
    assert not should_pause_import_batch(HTTPException(status_code=409, detail="该作品已经导入"))
