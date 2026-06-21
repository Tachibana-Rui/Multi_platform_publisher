from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import threading
from uuid import uuid4

from fastapi import HTTPException

from .config import settings


SETTINGS_PATH = settings.data_dir / "storage_settings.json"
_lock = threading.RLock()


def get_storage_settings() -> dict:
    path = settings.upload_dir.resolve()
    file_count = 0
    total_bytes = 0
    if path.is_dir():
        for item in path.rglob("*"):
            if item.is_file():
                file_count += 1
                try:
                    total_bytes += item.stat().st_size
                except OSError:
                    pass
    return {
        "path": str(path),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "environment_override": "CONTENT_HUB_UPLOAD_DIR" in os.environ,
    }


def update_storage_location(path_value: str) -> dict:
    with _lock:
        if "CONTENT_HUB_UPLOAD_DIR" in os.environ:
            raise HTTPException(
                status_code=409,
                detail="当前存储路径由 CONTENT_HUB_UPLOAD_DIR 环境变量控制，不能在界面中修改",
            )
        source = settings.upload_dir.expanduser().resolve()
        target = Path(path_value).expanduser().resolve()
        if source == target:
            return {**get_storage_settings(), "copied_files": 0}
        if target.is_relative_to(source) or source.is_relative_to(target):
            raise HTTPException(status_code=422, detail="新旧存储目录不能互相嵌套")
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / f".content-hub-write-test-{uuid4().hex}"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            raise HTTPException(status_code=422, detail="目标目录无法创建或没有写入权限") from exc

        copied_files = 0
        if source.is_dir():
            for item in source.rglob("*"):
                if not item.is_file():
                    continue
                relative = item.relative_to(source)
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    if destination.stat().st_size != item.stat().st_size:
                        raise HTTPException(
                            status_code=409,
                            detail=f"目标目录存在冲突文件：{relative.as_posix()}",
                        )
                    continue
                shutil.copy2(item, destination)
                copied_files += 1

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = SETTINGS_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"upload_dir": str(target)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(SETTINGS_PATH)
        settings.upload_dir = target
        return {**get_storage_settings(), "copied_files": copied_files}
