import io
import os
from pathlib import Path
import tempfile
import time

from PIL import Image, ImageDraw


_temp_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("CONTENT_HUB_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CONTENT_HUB_DATA_DIR", os.path.join(_temp_dir.name, "data"))
os.environ.setdefault("CONTENT_HUB_UPLOAD_DIR", os.path.join(_temp_dir.name, "uploads"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.content_matcher import parse_structured_folder  # noqa: E402
from app.xiaohongshu import normalize_source_url, parse_note_page  # noqa: E402


def make_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), "#ef6a4c").save(buffer, format="PNG")
    return buffer.getvalue()


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
        assert post["assets"][0]["url"].startswith(f"/media/{post['id']}/")
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
        assert "api_key" not in payload
        assert "test-secret-api-key-1234" not in SETTINGS_PATH.read_text(encoding="utf-8")


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

        version = client.get(f"/api/posts/{post['id']}/platform-versions/douyin")
        assert version.status_code == 200
        version_data = version.json()
        assert version_data["title"] == "NIKKE 艾德兔女郎"
        assert version_data["body"] == "原始正文"
        assert version_data["content_source"] == "copied"
        assert version_data["selected_asset_ids"] == [uploaded["assets"][0]["id"]]
        assert version_data["suggested_prompt"] == "生成 NIKKE 艾德 cos 抖音 标题和文案。"

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
    manual_folder = Path(_temp_dir.name) / "manual-originals"
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
