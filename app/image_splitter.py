from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .media_storage import safe_filename
from .models import AssetMatch, MediaAsset, PlatformVersion, Post, utc_now
from .platform_adapter import resolve_selected_assets, validate_platform_assets


_split_lock = threading.Lock()


def _source_path(db: Session, asset: MediaAsset) -> Path:
    match = db.scalar(select(AssetMatch).where(AssetMatch.downloaded_asset_id == asset.id))
    name = (
        match.copied_storage_name
        if match and match.status == "matched" and match.copied_storage_name
        else asset.storage_name
    )
    path = (settings.upload_dir / name).resolve()
    if not path.is_relative_to(settings.upload_dir.resolve()):
        raise HTTPException(status_code=422, detail="检测到无效的素材路径")
    if not path.is_file():
        raise HTTPException(status_code=422, detail=f"图片文件不存在：{asset.original_name}")
    return path


def _split_image(
    db: Session, post: Post, asset: MediaAsset, created_paths: list[Path],
) -> list[MediaAsset]:
    if asset.media_type != "image":
        raise HTTPException(status_code=422, detail="只能切分横向图片")
    source = _source_path(db, asset)
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    try:
        with Image.open(source) as opened:
            if getattr(opened, "is_animated", False):
                raise HTTPException(status_code=422, detail="暂不支持切分动图，请选择静态横图")
            picture = ImageOps.exif_transpose(opened)
            width, height = picture.size
            if width <= height:
                raise HTTPException(status_code=422, detail=f"请选择横向图片：{asset.original_name}")
            # Lossless PNG preserves the source pixels; normalize palette/CMYK
            # images and apply EXIF orientation before deciding the split axis.
            picture = picture.convert("RGBA" if "A" in picture.getbands() or "transparency" in picture.info else "RGB")
            midpoint = width // 2
            larger_half = width - midpoint
            output_height = height if larger_half < height else (larger_half * 4 + 2) // 3
            parts = []
            for side, label, box in (
                ("left", "左半图", (0, 0, midpoint, height)),
                ("right", "右半图", (midpoint, 0, width, height)),
            ):
                storage_name = f"{post.id}/processed/{asset.id}_{digest}_{side}.png"
                target = settings.upload_dir / storage_name
                existing = next((item for item in post.assets if item.storage_name == storage_name), None)
                if existing and target.is_file():
                    parts.append(existing)
                    continue
                cropped = picture.crop(box)
                if output_height != height:
                    canvas = Image.new(picture.mode, (cropped.width, output_height), "white")
                    canvas.paste(cropped, (0, (output_height - height) // 2))
                    cropped = canvas
                target.parent.mkdir(parents=True, exist_ok=True)
                # Only roll back files created by this request. Existing originals
                # and cached crops are never removed on a failed batch.
                if target.exists():
                    raise HTTPException(status_code=409, detail="切图路径已存在，请刷新后重试")
                created_paths.append(target)
                cropped.save(target, format="PNG", icc_profile=opened.info.get("icc_profile"))
                if target.stat().st_size > settings.max_upload_bytes:
                    raise HTTPException(status_code=413, detail="切分后的图片超过允许的最大尺寸")
                if existing is None:
                    with target.open("rb") as stream:
                        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
                    existing = MediaAsset(
                        original_name=safe_filename(f"{Path(asset.original_name).stem}_{label}.png"),
                        storage_name=storage_name, media_type="image", mime_type="image/png",
                        file_size=target.stat().st_size, checksum=checksum,
                        width=cropped.width, height=cropped.height,
                    )
                    post.assets.append(existing)
                parts.append(existing)
            return parts
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(status_code=422, detail=f"图片无法切分：{asset.original_name}") from exc


def split_platform_landscapes(
    db: Session, post: Post, version: PlatformVersion, *,
    asset_ids: list[str], split_asset_ids: list[str], title: str, body: str,
) -> None:
    """Persist left/right/original triplets in the current platform's upload order."""
    if len(set(asset_ids)) != len(asset_ids) or len(set(split_asset_ids)) != len(split_asset_ids):
        raise HTTPException(status_code=422, detail="素材选择中包含重复项")
    selected = resolve_selected_assets(post, asset_ids)
    validate_platform_assets(version.platform, selected, require_assets=True)
    sources = resolve_selected_assets(post, split_asset_ids)
    if not set(split_asset_ids).issubset(asset_ids):
        raise HTTPException(status_code=422, detail="请先勾选需要切分的横图")
    created_paths: list[Path] = []
    with _split_lock:
        try:
            groups = {asset.id: _split_image(db, post, asset, created_paths) for asset in sources}
            db.flush()
            part_ids = {part.id for parts in groups.values() for part in parts}
            ordered = []
            for asset in selected:
                if asset.id in groups:
                    ordered.extend(groups[asset.id])
                if asset.id not in part_ids:
                    ordered.append(asset)
            validate_platform_assets(version.platform, ordered)
            # Keep new crops beside their source in the content library as well.
            library_order = []
            for asset in post.assets:
                if asset.id in groups:
                    library_order.extend(groups[asset.id])
                if asset.id not in part_ids:
                    library_order.append(asset)
            for position, asset in enumerate(library_order):
                asset.position = position
            version.selected_asset_ids_json = json.dumps([asset.id for asset in ordered])
            if version.title != title or version.body != body:
                version.content_source = "manual"
            version.title, version.body = title, body
            post.content_type = "gallery"
            post.updated_at = utc_now()
            db.commit()
        except Exception:
            db.rollback()
            for path in created_paths:
                path.unlink(missing_ok=True)
            raise
    db.expire(post, ["assets"])
