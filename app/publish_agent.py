from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import threading

from sqlalchemy import select

from .config import settings
from .database import SessionLocal
from .models import AssetMatch, MediaAsset, PlatformPublication
from .publishers import get_publisher
from .publishers.browser import PLATFORM_BROWSER_LOCKS
from .publishers.base import PublicationCancelled, PublishAsset, PublishSnapshot


ACTIVE_STATUSES = {
    "pending", "queued", "validating", "awaiting_login", "preparing", "review_pending", "publishing"
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_list(value: str | None) -> list:
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def serialize_publication(publication: PlatformPublication) -> dict:
    return {
        "id": publication.id,
        "post_id": publication.post_id,
        "platform": publication.platform,
        "visibility": publication.visibility,
        "title": publication.title,
        "body": publication.body,
        "asset_ids": [str(value) for value in _json_list(publication.asset_ids_json)],
        "status": publication.status,
        "validation": _json_list(publication.validation_json),
        "logs": _json_list(publication.logs_json),
        "error_message": publication.error_message,
        "platform_item_id": publication.platform_item_id,
        "platform_url": publication.platform_url,
        "attempt_count": publication.attempt_count,
        "prepared_at": publication.prepared_at,
        "published_at": publication.published_at,
        "created_at": publication.created_at,
        "updated_at": publication.updated_at,
    }


@dataclass
class _TaskHandle:
    confirm_event: threading.Event
    cancel_event: threading.Event
    thread: threading.Thread


class PublishAgent:
    """Runs visible, human-reviewed browser publication jobs in background threads."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, _TaskHandle] = {}
        self._platform_locks = PLATFORM_BROWSER_LOCKS

    def recover_interrupted(self) -> None:
        with SessionLocal() as db:
            publications = db.scalars(
                select(PlatformPublication).where(PlatformPublication.status.in_(ACTIVE_STATUSES))
            ).all()
            for publication in publications:
                publication.status = "failed"
                publication.error_message = "应用在发布过程中重启；请点击重试重新打开平台窗口"
                self._append_log(publication, "failed", publication.error_message)
            if publications:
                db.commit()

    def start(self, publication_id: str) -> bool:
        with self._lock:
            existing = self._tasks.get(publication_id)
            if existing and existing.thread.is_alive():
                return False
            confirm_event = threading.Event()
            cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._run,
                args=(publication_id, confirm_event, cancel_event),
                name=f"publish-{publication_id[:8]}",
                daemon=True,
            )
            self._tasks[publication_id] = _TaskHandle(confirm_event, cancel_event, thread)
            thread.start()
            return True

    def confirm(self, publication_id: str) -> bool:
        with self._lock:
            handle = self._tasks.get(publication_id)
            if not handle or not handle.thread.is_alive():
                return False
            handle.confirm_event.set()
            return True

    def cancel(self, publication_id: str) -> bool:
        with self._lock:
            handle = self._tasks.get(publication_id)
            if not handle or not handle.thread.is_alive():
                return False
            handle.cancel_event.set()
            return True

    def _run(
        self,
        publication_id: str,
        confirm_event: threading.Event,
        cancel_event: threading.Event,
    ) -> None:
        platform_lock: threading.Lock | None = None
        platform_lock_acquired = False
        try:
            self._set_status(publication_id, "validating", "正在校验发布素材和平台文案")
            snapshot = self._build_snapshot(publication_id)
            publisher = get_publisher(snapshot.platform)
            issues = publisher.validate(snapshot)
            self._save_validation(publication_id, issues)
            errors = [issue["message"] for issue in issues if issue.get("level") == "error"]
            if errors:
                raise RuntimeError("；".join(errors))

            platform_lock = self._platform_locks[snapshot.platform]
            if platform_lock.locked():
                self._set_status(publication_id, "queued", "同平台已有发布窗口，当前任务正在排队")
            while not platform_lock.acquire(timeout=0.5):
                if cancel_event.is_set():
                    raise PublicationCancelled("发布任务已取消")
            platform_lock_acquired = True

            result = publisher.execute(
                snapshot,
                confirm_event,
                cancel_event,
                lambda status, message: self._set_status(publication_id, status, message),
                lambda title, body: self._sync_content(publication_id, title, body),
            )
            self._finish(publication_id, result)
        except PublicationCancelled as exc:
            self._set_status(publication_id, "cancelled", str(exc), error=str(exc))
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            self._set_status(publication_id, "failed", message, error=message)
        finally:
            if platform_lock and platform_lock_acquired:
                platform_lock.release()
            with self._lock:
                self._tasks.pop(publication_id, None)

    def _build_snapshot(self, publication_id: str) -> PublishSnapshot:
        with SessionLocal() as db:
            publication = db.get(PlatformPublication, publication_id)
            if not publication:
                raise RuntimeError("发布任务不存在")
            publication.attempt_count += 1
            publication.error_message = None
            asset_ids = [str(value) for value in _json_list(publication.asset_ids_json)]
            assets = db.scalars(select(MediaAsset).where(MediaAsset.id.in_(asset_ids))).all() if asset_ids else []
            asset_map = {asset.id: asset for asset in assets}
            if len(asset_map) != len(set(asset_ids)):
                raise RuntimeError("部分发布素材已从 Content Hub 删除")

            matches = db.scalars(
                select(AssetMatch).where(AssetMatch.downloaded_asset_id.in_(asset_ids))
            ).all() if asset_ids else []
            match_map = {match.downloaded_asset_id: match for match in matches}
            publish_assets: list[PublishAsset] = []
            for asset_id in asset_ids:
                asset = asset_map[asset_id]
                match = match_map.get(asset.id)
                storage_name = (
                    match.copied_storage_name
                    if match and match.status == "matched" and match.copied_storage_name
                    else asset.storage_name
                )
                path = (settings.upload_dir / storage_name).resolve()
                if not path.is_relative_to(settings.upload_dir):
                    raise RuntimeError("检测到无效的素材路径")
                publish_assets.append(PublishAsset(
                    id=asset.id,
                    path=path,
                    media_type=asset.media_type,
                    mime_type=asset.mime_type,
                    file_size=path.stat().st_size if path.is_file() else asset.file_size,
                    width=asset.width,
                    height=asset.height,
                    duration_seconds=asset.duration_seconds,
                ))
            db.commit()
            return PublishSnapshot(
                id=publication.id,
                post_id=publication.post_id,
                platform=publication.platform,
                visibility=publication.visibility,
                title=publication.title,
                body=publication.body,
                assets=publish_assets,
            )

    def _set_status(
        self,
        publication_id: str,
        status: str,
        message: str,
        *,
        error: str | None = None,
    ) -> None:
        with SessionLocal() as db:
            publication = db.get(PlatformPublication, publication_id)
            if not publication:
                return
            publication.status = status
            publication.error_message = error
            if status == "review_pending":
                publication.prepared_at = _now()
            self._append_log(publication, status, message)
            db.commit()

    def _save_validation(self, publication_id: str, issues: list[dict]) -> None:
        with SessionLocal() as db:
            publication = db.get(PlatformPublication, publication_id)
            if publication:
                publication.validation_json = json.dumps(issues, ensure_ascii=False)
                db.commit()

    def _sync_content(self, publication_id: str, title: str, body: str) -> None:
        with SessionLocal() as db:
            publication = db.get(PlatformPublication, publication_id)
            if not publication or (publication.title == title and publication.body == body):
                return
            publication.title = title[:300]
            publication.body = body[:100_000]
            if publication.platform_version:
                publication.platform_version.title = publication.title
                publication.platform_version.body = publication.body
                publication.platform_version.content_source = "browser"
            self._append_log(publication, "content_synced", "已同步平台浏览器中的标题和正文修改")
            db.commit()

    def _finish(self, publication_id: str, result: dict) -> None:
        status = result.get("status") or "submitted"
        if result.get("manual"):
            message = "检测到用户已在平台页面完成发布"
        else:
            message = "平台已确认接收作品" if status == "published" else "已点击发布，等待平台处理或审核"
        with SessionLocal() as db:
            publication = db.get(PlatformPublication, publication_id)
            if not publication:
                return
            publication.status = status
            publication.platform_url = result.get("platform_url")
            publication.platform_item_id = result.get("platform_item_id")
            publication.published_at = _now()
            publication.error_message = None
            self._append_log(publication, status, message)
            db.commit()

    @staticmethod
    def _append_log(publication: PlatformPublication, status: str, message: str) -> None:
        logs = _json_list(publication.logs_json)
        logs.append({"at": _now().isoformat(), "status": status, "message": message[:1000]})
        publication.logs_json = json.dumps(logs[-100:], ensure_ascii=False)


publication_agent = PublishAgent()
