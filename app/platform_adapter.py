from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import MediaAsset, PlatformVersion, Post
from .doubao import build_generation_prompt


SUPPORTED_PLATFORMS = {
    "douyin", "xiaohongshu", "bilibili", "kuaishou", "wechat_channels"
}


def validate_platform(platform: str) -> None:
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=422, detail="暂不支持该目标平台")


def get_or_create_version(db: Session, post: Post, platform: str) -> PlatformVersion:
    validate_platform(platform)
    version = db.scalar(select(PlatformVersion).where(
        PlatformVersion.post_id == post.id,
        PlatformVersion.platform == platform,
    ))
    if version:
        return version
    default_assets = [asset.id for asset in post.assets][:30]
    version = PlatformVersion(
        post_id=post.id,
        platform=platform,
        title=post.title,
        body=post.body,
        selected_asset_ids_json=json.dumps(default_assets),
        content_source="copied",
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def selected_asset_ids(version: PlatformVersion) -> list[str]:
    try:
        values = json.loads(version.selected_asset_ids_json or "[]")
    except json.JSONDecodeError:
        return []
    return [str(value) for value in values if value]


def resolve_selected_assets(post: Post, asset_ids: list[str]) -> list[MediaAsset]:
    available = {asset.id: asset for asset in post.assets}
    invalid = [asset_id for asset_id in asset_ids if asset_id not in available]
    if invalid:
        raise HTTPException(status_code=422, detail="选择的素材不属于当前内容")
    return [available[asset_id] for asset_id in asset_ids]


def serialize_version(version: PlatformVersion, post: Post) -> dict:
    selected_ids = selected_asset_ids(version)
    selected_set = set(selected_ids)
    image_count = sum(
        1 for asset in post.assets if asset.id in selected_set and asset.media_type == "image"
    )
    return {
        "id": version.id,
        "post_id": post.id,
        "platform": version.platform,
        "title": version.title,
        "body": version.body,
        "selected_asset_ids": selected_ids,
        "generation_count": version.generation_count,
        "content_source": version.content_source,
        "last_prompt": version.last_prompt,
        "last_model": version.last_model,
        "suggested_prompt": build_generation_prompt(
            post, version.platform, min(image_count, 4)
        ),
        "assets": [{
            "id": asset.id,
            "original_name": asset.original_name,
            "media_type": asset.media_type,
            "width": asset.width,
            "height": asset.height,
            "url": f"/media/{asset.storage_name}",
        } for asset in post.assets],
    }
