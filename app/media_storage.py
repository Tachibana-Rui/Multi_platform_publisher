from __future__ import annotations

import hashlib
from pathlib import Path
import re

from sqlalchemy import select

from .config import settings
from .database import SessionLocal
from .models import AssetMatch, MediaAsset


DOWNLOADED_PLATFORMS = {"xiaohongshu", "douyin"}
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(value: str, *, max_length: int = 180) -> str:
    name = Path(value or "unnamed").name
    name = INVALID_FILENAME_CHARS.sub("_", name).strip(" .")
    if not name:
        name = "unnamed"
    suffix = Path(name).suffix
    stem_limit = max(1, max_length - len(suffix))
    return f"{Path(name).stem[:stem_limit]}{suffix}"[:max_length]


def original_storage_name(post_id: str, position: int, original_name: str) -> str:
    filename = f"{position + 1:02d}_{safe_filename(original_name)}"
    return f"{post_id}/originals/{filename}"


def download_storage_name(post_id: str, filename: str) -> str:
    return f"{post_id}/downloads/{safe_filename(filename, max_length=200)}"


def prune_empty_media_dirs(post_id: str) -> None:
    post_root = (settings.upload_dir / post_id).resolve()
    if not post_root.is_relative_to(settings.upload_dir.resolve()):
        return
    for name in ("originals", "downloads"):
        folder = post_root / name
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass
    try:
        if post_root.is_dir() and not any(post_root.iterdir()):
            post_root.rmdir()
    except OSError:
        pass


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_path(storage_name: str) -> Path:
    root = settings.upload_dir.resolve()
    path = (root / storage_name).resolve()
    if not path.is_relative_to(root):
        raise RuntimeError(f"检测到无效媒体路径：{storage_name}")
    return path


def _available_destination(relative_name: str, expected_checksum: str | None) -> tuple[str, Path]:
    destination = _safe_path(relative_name)
    if not destination.exists() or (
        destination.is_file() and expected_checksum and _checksum(destination) == expected_checksum
    ):
        return relative_name, destination

    relative = Path(relative_name)
    for number in range(2, 1000):
        candidate_name = f"{relative.stem}_({number}){relative.suffix}"
        candidate_relative = (relative.parent / candidate_name).as_posix()
        candidate = _safe_path(candidate_relative)
        if not candidate.exists() or (
            candidate.is_file() and expected_checksum and _checksum(candidate) == expected_checksum
        ):
            return candidate_relative, candidate
    raise RuntimeError(f"无法为媒体文件生成无冲突路径：{relative_name}")


def _move_and_update(
    source_relative: str,
    desired_relative: str,
    expected_checksum: str | None,
) -> tuple[str, bool]:
    source = _safe_path(source_relative)
    desired_relative, destination = _available_destination(desired_relative, expected_checksum)
    if source == destination:
        return desired_relative, False

    source_existed = source.is_file()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and expected_checksum and _checksum(destination) == expected_checksum:
        if source_existed:
            source.unlink()
    elif source_existed:
        source.replace(destination)
    elif not destination.is_file():
        return source_relative, False

    try:
        parent = source.parent
        post_root = settings.upload_dir.resolve() / Path(source_relative).parts[0]
        if parent != post_root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except (OSError, IndexError):
        pass
    return desired_relative, source_existed


def migrate_media_layout() -> dict:
    """Move tracked media into per-post originals/downloads folders and repair paths."""
    moved_assets = 0
    repaired_assets = 0
    moved_matches = 0
    missing: list[str] = []
    with SessionLocal() as db:
        assets = db.scalars(select(MediaAsset).order_by(MediaAsset.post_id, MediaAsset.position)).all()
        for asset in assets:
            post = asset.post
            normalized = asset.storage_name.replace("\\", "/")
            if "/originals/" in normalized or "/downloads/" in normalized:
                continue
            if post.source_platform in DOWNLOADED_PLATFORMS:
                basename = Path(asset.storage_name).name or asset.original_name
                desired = download_storage_name(post.id, basename)
            else:
                desired = original_storage_name(post.id, asset.position, asset.original_name)
            if asset.storage_name == desired:
                continue
            new_name, moved = _move_and_update(asset.storage_name, desired, asset.checksum)
            if new_name == asset.storage_name:
                missing.append(asset.storage_name)
                continue
            if moved:
                moved_assets += 1
            else:
                repaired_assets += 1
            asset.storage_name = new_name
            db.commit()

        matches = db.scalars(select(AssetMatch).where(AssetMatch.copied_storage_name.is_not(None))).all()
        for match in matches:
            current = match.copied_storage_name
            if not current or "/originals/" in current.replace("\\", "/"):
                continue
            post_id = match.downloaded_asset.post_id
            desired = f"{post_id}/originals/{safe_filename(Path(current).name, max_length=200)}"
            new_name, moved = _move_and_update(current, desired, None)
            if new_name != current:
                match.copied_storage_name = new_name
                moved_matches += int(moved)
                db.commit()
            else:
                missing.append(current)

    return {
        "moved_assets": moved_assets,
        "repaired_assets": repaired_assets,
        "moved_matches": moved_matches,
        "missing": missing,
    }
