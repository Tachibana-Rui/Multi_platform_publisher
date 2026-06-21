from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import unicodedata

from fastapi import HTTPException
import imagehash
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError
from skimage.metrics import structural_similarity
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import AssetMatch, LibraryFolder, MediaAsset, OriginalAsset, Post, SourceRoot


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic"}
IGNORED_DIRS = {".git", "__pycache__", "$recycle.bin", "system volume information"}
DATE_PATTERN = re.compile(r"(?<!\d)(20\d{6})(?!\d)")
SEPARATOR_PATTERN = re.compile(r"[+＋_\-—\s]+")
COS_SUFFIXES = ("cosplay", "coser", "cos正片", "cos", "正片")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in value if char.isalnum())


def normalize_character_tag(value: str) -> str:
    normalized = normalize_name(value)
    for suffix in COS_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            return normalized[:-len(suffix)]
    return normalized


def parse_structured_folder(folder_name: str) -> dict:
    normalized = unicodedata.normalize("NFKC", folder_name).strip()
    date_match = DATE_PATTERN.search(normalized)
    if not date_match:
        return {"coser_name": None, "character_name": None, "shoot_date": None, "parse_status": "unparsed"}
    raw_date = date_match.group(1)
    prefix = normalized[:date_match.start()].strip(" +＋_-—")
    parts = [part.strip() for part in SEPARATOR_PATTERN.split(prefix) if part.strip()]
    if len(parts) < 2:
        return {
            "coser_name": parts[0] if parts else None,
            "character_name": None,
            "shoot_date": f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}",
            "parse_status": "partial",
        }
    return {
        "coser_name": parts[0][:150],
        "character_name": " ".join(parts[1:])[:150],
        "shoot_date": f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}",
        "parse_status": "parsed",
    }


def _image_metadata(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            perceptual_hash = str(imagehash.phash(image.convert("RGB")))
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError(f"无法读取图片：{path.name}") from exc
    stat = path.stat()
    return {
        "file_size": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
        "width": width,
        "height": height,
        "checksum": digest.hexdigest(),
        "phash": perceptual_hash,
        "indexed_at": utc_now(),
    }


def add_source_root(db: Session, path_value: str, name: str | None = None) -> SourceRoot:
    path = Path(path_value).expanduser().resolve()
    if not path.is_dir():
        raise HTTPException(status_code=422, detail="素材库目录不存在或不是文件夹")
    existing = db.scalar(select(SourceRoot).where(func.lower(SourceRoot.path) == str(path).lower()))
    if existing:
        return existing
    root = SourceRoot(name=(name or path.name or "素材库")[:100], path=str(path))
    db.add(root)
    db.commit()
    db.refresh(root)
    return root


def _commit_scan_batch(db: Session) -> None:
    # Keep write transactions short so normal Content Hub writes are not blocked by indexing.
    db.commit()


def scan_source_root(db: Session, root: SourceRoot, progress_callback=None) -> dict:
    root_path = Path(root.path)
    if not root_path.is_dir():
        raise HTTPException(status_code=422, detail="素材库目录当前不可访问")
    existing_folders = {folder.relative_path: folder for folder in root.folders}
    seen_folders: set[str] = set()
    indexed = skipped = removed = errors = 0
    files_seen = 0

    def report() -> None:
        if progress_callback:
            progress_callback({
                "folders": len(seen_folders), "files_seen": files_seen,
                "indexed": indexed, "skipped": skipped, "errors": errors,
            })

    for current_dir, dir_names, file_names in os.walk(root_path, followlinks=False):
        dir_names[:] = [
            name for name in dir_names
            if not name.startswith(".") and name.casefold() not in IGNORED_DIRS
        ]
        image_names = [name for name in file_names if Path(name).suffix.casefold() in IMAGE_EXTENSIONS]
        if not image_names:
            continue
        directory = Path(current_dir).resolve()
        try:
            relative_dir = directory.relative_to(root_path).as_posix() or "."
        except ValueError:
            continue
        seen_folders.add(relative_dir)
        parsed = parse_structured_folder(directory.name)
        folder = existing_folders.get(relative_dir)
        if folder is None:
            folder = LibraryFolder(
                source_root_id=root.id,
                path=str(directory),
                relative_path=relative_dir,
                folder_name=directory.name,
                **parsed,
            )
            db.add(folder)
            db.flush()
            _commit_scan_batch(db)
            existing_folders[relative_dir] = folder
        else:
            folder.path = str(directory)
            folder.folder_name = directory.name
            for key, value in parsed.items():
                setattr(folder, key, value)
            folder.indexed_at = utc_now()

        existing_assets = {asset.path: asset for asset in folder.assets}
        seen_assets: set[str] = set()
        for filename in image_names:
            files_seen += 1
            path = (directory / filename).resolve()
            try:
                path.relative_to(root_path)
                stat = path.stat()
            except (ValueError, OSError):
                errors += 1
                report()
                continue
            path_key = str(path)
            seen_assets.add(path_key)
            asset = existing_assets.get(path_key)
            if asset and asset.file_size == stat.st_size and asset.modified_ns == stat.st_mtime_ns:
                skipped += 1
                report()
                continue
            try:
                metadata = _image_metadata(path)
            except ValueError:
                errors += 1
                report()
                continue
            if asset is None:
                asset = OriginalAsset(
                    folder_id=folder.id,
                    path=path_key,
                    relative_path=path.relative_to(root_path).as_posix(),
                    filename=path.name,
                    **metadata,
                )
                db.add(asset)
            else:
                for key, value in metadata.items():
                    setattr(asset, key, value)
            indexed += 1
            if indexed % 25 == 0:
                _commit_scan_batch(db)
            report()
        for path_key, asset in existing_assets.items():
            if path_key not in seen_assets:
                db.delete(asset)
                removed += 1
        _commit_scan_batch(db)

    for relative_path, folder in existing_folders.items():
        if relative_path not in seen_folders:
            db.delete(folder)
    root.last_scanned_at = utc_now()
    _commit_scan_batch(db)
    report()
    folder_count = db.scalar(
        select(func.count()).select_from(LibraryFolder).where(LibraryFolder.source_root_id == root.id)
    ) or 0
    asset_count = db.scalar(
        select(func.count()).select_from(OriginalAsset)
        .join(LibraryFolder).where(LibraryFolder.source_root_id == root.id)
    ) or 0
    return {
        "indexed": indexed, "skipped": skipped, "removed": removed, "errors": errors,
        "folders": folder_count, "assets": asset_count,
    }


def _folder_matches_tags(folder: LibraryFolder, tags: list[str]) -> bool:
    character = normalize_name(folder.character_name or "")
    if not character:
        return False
    normalized_tags = [normalize_character_tag(tag) for tag in tags]
    return any(tag and (tag == character or tag in character or character in tag) for tag in normalized_tags)


def select_candidate_folders(
    db: Session,
    post: Post,
    source_root_id: str | None = None,
    coser_name: str | None = None,
    character_name: str | None = None,
    shoot_date: str | None = None,
) -> list[LibraryFolder]:
    statement = select(LibraryFolder)
    if source_root_id:
        statement = statement.where(LibraryFolder.source_root_id == source_root_id)
    if coser_name:
        statement = statement.where(LibraryFolder.coser_name.ilike(f"%{coser_name.strip()}%"))
    if character_name:
        statement = statement.where(LibraryFolder.character_name.ilike(f"%{character_name.strip()}%"))
    if shoot_date:
        statement = statement.where(LibraryFolder.shoot_date == shoot_date)
    folders = list(db.scalars(statement).all())
    if source_root_id or coser_name or character_name or shoot_date:
        return folders
    routed = [folder for folder in folders if _folder_matches_tags(folder, [tag.name for tag in post.tags])]
    return routed


def _ssim(query: Image.Image, original_path: str) -> float:
    with Image.open(original_path) as source:
        source = ImageOps.exif_transpose(source).convert("RGB")
        query_rgb = query.convert("RGB")
        if max(query_rgb.size) > 1200:
            ratio = 1200 / max(query_rgb.size)
            target_size = (max(1, round(query_rgb.width * ratio)), max(1, round(query_rgb.height * ratio)))
            query_rgb = query_rgb.resize(target_size, Image.Resampling.LANCZOS)
        source = source.resize(query_rgb.size, Image.Resampling.LANCZOS)
        return float(structural_similarity(
            np.asarray(query_rgb), np.asarray(source), channel_axis=2, data_range=255
        ))


def _copy_original(post_id: str, position: int, original: OriginalAsset) -> str:
    target_dir = settings.upload_dir / post_id / "originals"
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(original.filename).name
    target = target_dir / f"{position + 1:02d}_{safe_name}"
    shutil.copy2(original.path, target)
    return f"{post_id}/originals/{target.name}"


def serialize_match(match: AssetMatch) -> dict:
    original = match.original_asset
    return {
        "id": match.id,
        "downloaded_asset_id": match.downloaded_asset_id,
        "original_asset_id": match.original_asset_id,
        "original_path": original.path if original else None,
        "original_filename": original.filename if original else None,
        "original_width": original.width if original else None,
        "original_height": original.height if original else None,
        "phash_distance": match.phash_distance,
        "ssim_score": match.ssim_score,
        "confidence": match.confidence,
        "status": match.status,
        "candidates": json.loads(match.candidates_json or "[]"),
        "original_url": f"/media/{match.copied_storage_name}" if match.copied_storage_name else None,
    }


def match_post_images(
    db: Session,
    post: Post,
    source_root_id: str | None = None,
    coser_name: str | None = None,
    character_name: str | None = None,
    shoot_date: str | None = None,
) -> dict:
    folders = select_candidate_folders(
        db, post, source_root_id, coser_name, character_name, shoot_date
    )
    originals = [asset for folder in folders for asset in folder.assets]
    downloaded = [asset for asset in post.assets if asset.media_type == "image"]
    if not downloaded:
        raise HTTPException(status_code=422, detail="该内容没有可匹配的图片")
    used_originals: set[str] = set()
    results: list[dict] = []

    for position, asset in enumerate(downloaded):
        path = settings.upload_dir / asset.storage_name
        existing = db.scalar(select(AssetMatch).where(AssetMatch.downloaded_asset_id == asset.id))
        match = existing or AssetMatch(downloaded_asset_id=asset.id)
        if existing and match.copied_storage_name:
            (settings.upload_dir / match.copied_storage_name).unlink(missing_ok=True)
            match.copied_storage_name = None
        if not existing:
            db.add(match)
        ranked: list[tuple[int, OriginalAsset]] = []
        try:
            with Image.open(path) as image:
                query = ImageOps.exif_transpose(image).convert("RGB")
                query_hash = imagehash.phash(query)
                query_ratio = query.width / query.height
                for original in originals:
                    if original.id in used_originals:
                        continue
                    original_ratio = original.width / original.height
                    if abs(original_ratio - query_ratio) / query_ratio > 0.015:
                        continue
                    distance = query_hash - imagehash.hex_to_hash(original.phash)
                    if distance <= 12:
                        ranked.append((distance, original))
                ranked.sort(key=lambda item: item[0])
                scored = []
                for distance, original in ranked[:8]:
                    score = _ssim(query, original.path)
                    confidence = max(0.0, min(1.0, 0.35 * (1 - distance / 16) + 0.65 * score))
                    scored.append((confidence, distance, score, original))
        except (OSError, UnidentifiedImageError):
            scored = []

        scored.sort(key=lambda item: item[0], reverse=True)
        candidates = [{
            "original_asset_id": candidate.id,
            "path": candidate.path,
            "filename": candidate.filename,
            "width": candidate.width,
            "height": candidate.height,
            "phash_distance": distance,
            "ssim_score": round(ssim, 5),
            "confidence": round(confidence, 5),
        } for confidence, distance, ssim, candidate in scored[:5]]
        match.candidates_json = json.dumps(candidates, ensure_ascii=False)

        if scored:
            confidence, distance, ssim, best = scored[0]
            match.original_asset = best
            match.phash_distance = distance
            match.ssim_score = ssim
            match.confidence = confidence
            if distance <= 6 and ssim >= 0.94:
                match.status = "matched"
                match.copied_storage_name = _copy_original(post.id, position, best)
                used_originals.add(best.id)
            elif distance <= 10 and ssim >= 0.88:
                match.status = "review"
                match.copied_storage_name = None
            else:
                match.status = "unmatched"
                match.copied_storage_name = None
        else:
            match.original_asset = None
            match.phash_distance = None
            match.ssim_score = None
            match.confidence = 0
            match.status = "unmatched"
            match.copied_storage_name = None
        db.flush()
        results.append(serialize_match(match))
    db.commit()
    return {
        "folders": [{
            "id": folder.id, "name": folder.folder_name, "coser_name": folder.coser_name,
            "character_name": folder.character_name, "shoot_date": folder.shoot_date,
        } for folder in folders],
        "searched_assets": len(originals),
        "matches": results,
    }


def match_post_images_in_folder(db: Session, post: Post, path_value: str) -> dict:
    path = Path(path_value).expanduser().resolve()
    root = add_source_root(db, str(path), f"手动匹配 · {path.name or '原图目录'}")
    scan = scan_source_root(db, root)
    result = match_post_images(db, post, source_root_id=root.id)
    result["manual_folder"] = str(path)
    result["scan"] = scan
    return result


def confirm_match(db: Session, match: AssetMatch) -> dict:
    if not match.original_asset:
        raise HTTPException(status_code=422, detail="该匹配没有可确认的原始素材")
    downloaded = match.downloaded_asset
    position = downloaded.position
    match.copied_storage_name = _copy_original(downloaded.post_id, position, match.original_asset)
    match.status = "matched"
    db.commit()
    db.refresh(match)
    return serialize_match(match)
