from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


PostStatus = Literal["draft", "ready", "archived"]
ContentType = Literal["image", "video", "gallery"]


class PostCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=100_000)
    source_platform: str = Field(default="manual", max_length=30)
    source_url: str | None = Field(default=None, max_length=1000)
    content_type: ContentType = "gallery"
    status: PostStatus = "draft"
    tags: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        return value.strip()

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            value = value.strip().lstrip("#")[:50]
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                result.append(value)
        return result


class PostUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=100_000)
    source_platform: str | None = Field(default=None, max_length=30)
    source_url: str | None = Field(default=None, max_length=1000)
    content_type: ContentType | None = None
    status: PostStatus | None = None
    tags: list[str] | None = Field(default=None, max_length=30)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, values: list[str] | None) -> list[str] | None:
        return PostCreate.clean_tags(values) if values is not None else None


class AssetResponse(BaseModel):
    id: str
    original_name: str
    media_type: str
    mime_type: str
    file_size: int
    checksum: str
    width: int | None
    height: int | None
    duration_seconds: float | None
    position: int
    url: str
    created_at: datetime


class AssetOrderUpdate(BaseModel):
    asset_ids: list[str] = Field(min_length=1, max_length=300)


class PostResponse(BaseModel):
    id: str
    title: str
    body: str
    source_platform: str
    source_url: str | None
    content_type: str
    status: str
    tags: list[str]
    assets: list[AssetResponse]
    created_at: datetime
    updated_at: datetime


class DashboardResponse(BaseModel):
    total_posts: int
    draft_posts: int
    ready_posts: int
    total_assets: int
    total_bytes: int


class XiaohongshuImportRequest(BaseModel):
    url: str = Field(min_length=10, max_length=2000)
    confirm_rights: bool


ImportPlatform = Literal["xiaohongshu", "douyin"]


class BatchImportRequest(BaseModel):
    platform: ImportPlatform
    text: str = Field(min_length=10, max_length=50_000)
    confirm_rights: bool


class SourceRootCreate(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    name: str | None = Field(default=None, max_length=100)


class MatchOriginalsRequest(BaseModel):
    source_root_id: str | None = None
    coser_name: str | None = Field(default=None, max_length=150)
    character_name: str | None = Field(default=None, max_length=150)
    shoot_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ManualMatchRequest(BaseModel):
    path: str = Field(min_length=1, max_length=2000)


class StorageSettingsUpdate(BaseModel):
    path: str = Field(min_length=1, max_length=2000)


PlatformName = Literal[
    "douyin", "xiaohongshu", "bilibili", "kuaishou", "wechat_channels"
]


class LLMSettingsUpdate(BaseModel):
    api_key: str | None = Field(default=None, min_length=8, max_length=1000)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    enable_web_search: bool | None = None
    clear_api_key: bool = False


class PlatformVersionUpdate(BaseModel):
    title: str = Field(default="", max_length=300)
    body: str = Field(default="", max_length=100_000)
    selected_asset_ids: list[str] = Field(default_factory=list, max_length=30)


class PlatformVersionCopyRequest(BaseModel):
    source_platform: PlatformName


class GenerateCopyRequest(BaseModel):
    selected_asset_ids: list[str] = Field(default_factory=list, max_length=30)
    custom_prompt: str | None = Field(default=None, max_length=4000)
    generate_title: bool = True
    generate_body: bool = True

    @model_validator(mode="after")
    def require_generation_target(self) -> "GenerateCopyRequest":
        if not self.generate_title and not self.generate_body:
            raise ValueError("请至少选择生成标题或生成正文")
        return self


PublishPlatform = Literal["douyin", "xiaohongshu", "bilibili"]
PublicationVisibility = Literal["public", "friends", "private"]


def _future_schedule(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("预约发布时间必须包含时区")
    value = value.astimezone(timezone.utc)
    if value <= datetime.now(timezone.utc):
        raise ValueError("预约发布时间必须晚于当前时间")
    return value


class PublicationCreate(BaseModel):
    post_id: str
    platform: PublishPlatform
    visibility: PublicationVisibility = "public"
    scheduled_at: datetime | None = None

    _validate_scheduled_at = field_validator("scheduled_at")(_future_schedule)


class PublicationBatchCreate(BaseModel):
    post_id: str
    platforms: list[PublishPlatform] = Field(min_length=1, max_length=4)
    visibility: PublicationVisibility = "public"
    scheduled_at: datetime | None = None

    @field_validator("platforms")
    @classmethod
    def unique_platforms(cls, values: list[PublishPlatform]) -> list[PublishPlatform]:
        return list(dict.fromkeys(values))

    _validate_scheduled_at = field_validator("scheduled_at")(_future_schedule)
