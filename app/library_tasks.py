from __future__ import annotations

from datetime import datetime, timezone
import threading
import traceback

from .content_matcher import scan_source_root
from .database import SessionLocal
from .models import SourceRoot


_lock = threading.Lock()
_states: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_scan_state(root_id: str) -> dict:
    with _lock:
        state = _states.get(root_id)
        return dict(state) if state else {"status": "idle", "progress": {}}


def is_scanning(root_id: str) -> bool:
    return get_scan_state(root_id)["status"] in {"queued", "scanning"}


def schedule_scan(root_id: str) -> dict:
    with _lock:
        current = _states.get(root_id)
        if current and current["status"] in {"queued", "scanning"}:
            return dict(current)
        _states[root_id] = {
            "status": "queued",
            "progress": {"folders": 0, "files_seen": 0, "indexed": 0, "skipped": 0, "errors": 0},
            "started_at": _now(),
            "finished_at": None,
            "result": None,
            "error": None,
        }

    thread = threading.Thread(target=_scan_worker, args=(root_id,), daemon=True)
    thread.start()
    return get_scan_state(root_id)


def _update_progress(root_id: str, progress: dict) -> None:
    with _lock:
        if root_id in _states:
            _states[root_id]["progress"] = dict(progress)


def _scan_worker(root_id: str) -> None:
    with _lock:
        _states[root_id]["status"] = "scanning"
    db = SessionLocal()
    try:
        root = db.get(SourceRoot, root_id)
        if not root:
            raise RuntimeError("素材库目录记录不存在")
        result = scan_source_root(db, root, lambda progress: _update_progress(root_id, progress))
        with _lock:
            _states[root_id].update(
                status="completed", result=result, finished_at=_now(), error=None
            )
    except Exception as exc:
        db.rollback()
        traceback.print_exc()
        with _lock:
            _states[root_id].update(
                status="failed", error=str(exc), finished_at=_now(), result=None
            )
    finally:
        db.close()

