from __future__ import annotations

import io
import atexit
import os
from pathlib import Path
import tempfile
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from PIL import Image


_temp_dir: tempfile.TemporaryDirectory | None = None


def _load_app():
    global _temp_dir
    if "CONTENT_HUB_DATABASE_URL" not in os.environ:
        _temp_dir = tempfile.TemporaryDirectory()
        root = Path(_temp_dir.name)
        os.environ["CONTENT_HUB_DATABASE_URL"] = f"sqlite:///{(root / 'test.db').as_posix()}"
        os.environ["CONTENT_HUB_DATA_DIR"] = str(root / "data")
        os.environ["CONTENT_HUB_UPLOAD_DIR"] = str(root / "uploads")
    from app.main import app
    from app.database import engine

    atexit.register(engine.dispose)

    return app


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "#28764a").save(buffer, format="PNG")
    return buffer.getvalue()


def test_scheduled_publication_is_saved_and_never_started_by_an_app_timer(monkeypatch):
    app = _load_app()
    started: list[str] = []
    monkeypatch.setattr(
        "app.main.publication_agent.start", lambda publication_id: started.append(publication_id) or True
    )
    scheduled_at = datetime.now(timezone.utc) + timedelta(hours=2)

    with TestClient(app) as client:
        post = client.post("/api/posts", json={"title": "原生定时发布", "body": "正文"}).json()
        uploaded = client.post(
            f"/api/posts/{post['id']}/assets",
            files=[("files", ("scheduled.png", _png(), "image/png"))],
        )
        assert uploaded.status_code == 201
        created = client.post("/api/publications", json={
            "post_id": post["id"],
            "platform": "douyin",
            "scheduled_at": scheduled_at.isoformat(),
        })

        assert created.status_code == 201
        publication = created.json()
        assert publication["scheduled_at"] is not None
        assert publication["status"] == "pending"
        # Starting the short-lived upload/browser job is expected; the application
        # retains no timer that would publish at scheduled_at.
        assert started == [publication["id"]]

        from app.database import SessionLocal
        from app.models import PlatformPublication

        with SessionLocal() as db:
            record = db.get(PlatformPublication, publication["id"])
            record.status = "scheduled"
            db.commit()
        assert client.post(f"/api/publications/{publication['id']}/mark-unpublished").status_code == 409
        assert client.delete(f"/api/publications/{publication['id']}").status_code == 409


def test_scheduled_publication_requires_a_future_timezone_aware_time():
    from app.schemas import PublicationCreate
    from pydantic import ValidationError

    future = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert PublicationCreate(post_id="post", platform="douyin", scheduled_at=future).scheduled_at == future
    for value in (datetime.now(), datetime.now(timezone.utc) - timedelta(seconds=1)):
        try:
            PublicationCreate(post_id="post", platform="douyin", scheduled_at=value)
        except ValidationError:
            continue
        raise AssertionError("invalid scheduled_at must be rejected")
