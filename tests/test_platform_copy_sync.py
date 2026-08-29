from __future__ import annotations

import atexit
import os
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient


_temp_dir: tempfile.TemporaryDirectory | None = None


def _load_app():
    global _temp_dir
    if "CONTENT_HUB_DATABASE_URL" not in os.environ:
        _temp_dir = tempfile.TemporaryDirectory()
        root = Path(_temp_dir.name)
        os.environ["CONTENT_HUB_DATABASE_URL"] = f"sqlite:///{(root / 'test.db').as_posix()}"
        os.environ["CONTENT_HUB_DATA_DIR"] = str(root / "data")
        os.environ["CONTENT_HUB_UPLOAD_DIR"] = str(root / "uploads")
    from app.database import engine
    from app.main import app

    atexit.register(engine.dispose)
    return app


def test_syncing_platform_copy_overwrites_only_the_target_text():
    app = _load_app()
    with TestClient(app) as client:
        post = client.post("/api/posts", json={
            "title": "原始标题", "body": "原始正文",
        }).json()
        source = client.put(
            f"/api/posts/{post['id']}/platform-versions/douyin",
            json={"title": "抖音标题", "body": "抖音正文", "selected_asset_ids": []},
        )
        assert source.status_code == 200
        target = client.put(
            f"/api/posts/{post['id']}/platform-versions/xiaohongshu",
            json={"title": "小红书标题", "body": "小红书正文", "selected_asset_ids": []},
        )
        assert target.status_code == 200

        synced = client.post(
            f"/api/posts/{post['id']}/platform-versions/xiaohongshu/sync-copy",
            json={"source_platform": "douyin"},
        )
        assert synced.status_code == 200
        assert synced.json()["title"] == "抖音标题"
        assert synced.json()["body"] == "抖音正文"
        assert synced.json()["selected_asset_ids"] == []
        assert synced.json()["content_source"] == "synced"

        # A platform not opened before is still selectable: its default platform
        # draft is the original copy and can be used as a source immediately.
        default_source = client.post(
            f"/api/posts/{post['id']}/platform-versions/xiaohongshu/sync-copy",
            json={"source_platform": "bilibili"},
        )
        assert default_source.status_code == 200
        assert default_source.json()["title"] == "原始标题"
        assert default_source.json()["body"] == "原始正文"

        same_platform = client.post(
            f"/api/posts/{post['id']}/platform-versions/douyin/sync-copy",
            json={"source_platform": "douyin"},
        )
        assert same_platform.status_code == 422
