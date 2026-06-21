from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, Table, Text, Column, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


post_tags = Table(
    "post_tags",
    Base.metadata,
    Column("post_id", String(36), ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_platform: Mapped[str] = mapped_column(String(30), default="manual", index=True)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    content_type: Mapped[str] = mapped_column(String(20), default="gallery", index=True)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, index=True
    )

    tags: Mapped[list[Tag]] = relationship(secondary=post_tags, lazy="selectin")
    assets: Mapped[list[MediaAsset]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="MediaAsset.position, MediaAsset.created_at",
        lazy="selectin",
    )
    platform_versions: Mapped[list[PlatformVersion]] = relationship(
        back_populates="post", cascade="all, delete-orphan", passive_deletes=True
    )
    publications: Mapped[list[PlatformPublication]] = relationship(
        back_populates="post", cascade="all, delete-orphan", passive_deletes=True
    )


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, index=True)


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    post_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    media_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    post: Mapped[Post] = relationship(back_populates="assets")


class SourceRoot(Base):
    __tablename__ = "source_roots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    path: Mapped[str] = mapped_column(String(2000), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    folders: Mapped[list[LibraryFolder]] = relationship(
        back_populates="source_root", cascade="all, delete-orphan", passive_deletes=True
    )


class LibraryFolder(Base):
    __tablename__ = "library_folders"
    __table_args__ = (UniqueConstraint("source_root_id", "relative_path"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    source_root_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_roots.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(String(2000), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(1500), nullable=False)
    folder_name: Mapped[str] = mapped_column(String(255), nullable=False)
    coser_name: Mapped[str | None] = mapped_column(String(150), index=True)
    character_name: Mapped[str | None] = mapped_column(String(150), index=True)
    shoot_date: Mapped[str | None] = mapped_column(String(10), index=True)
    parse_status: Mapped[str] = mapped_column(String(20), default="parsed")
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    source_root: Mapped[SourceRoot] = relationship(back_populates="folders")
    assets: Mapped[list[OriginalAsset]] = relationship(
        back_populates="folder", cascade="all, delete-orphan", passive_deletes=True
    )


class OriginalAsset(Base):
    __tablename__ = "original_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    folder_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("library_folders.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(String(2000), unique=True, nullable=False)
    relative_path: Mapped[str] = mapped_column(String(1500), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    modified_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    phash: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    folder: Mapped[LibraryFolder] = relationship(back_populates="assets")


class AssetMatch(Base):
    __tablename__ = "asset_matches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    downloaded_asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_assets.id", ondelete="CASCADE"), unique=True, index=True
    )
    original_asset_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("original_assets.id", ondelete="SET NULL"), index=True
    )
    phash_distance: Mapped[int | None] = mapped_column(Integer)
    ssim_score: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(20), default="unmatched", index=True)
    candidates_json: Mapped[str] = mapped_column(Text, default="[]")
    copied_storage_name: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    downloaded_asset: Mapped[MediaAsset] = relationship()
    original_asset: Mapped[OriginalAsset | None] = relationship()


class PlatformVersion(Base):
    __tablename__ = "platform_versions"
    __table_args__ = (UniqueConstraint("post_id", "platform"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    post_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    selected_asset_ids_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    generation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_source: Mapped[str] = mapped_column(String(20), default="copied", nullable=False)
    last_prompt: Mapped[str | None] = mapped_column(Text)
    last_model: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    post: Mapped[Post] = relationship(back_populates="platform_versions")


class PlatformPublication(Base):
    """Immutable publish snapshot plus the live state of one platform attempt."""

    __tablename__ = "platform_publications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    post_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("platform_versions.id", ondelete="SET NULL"), index=True
    )
    platform: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    visibility: Mapped[str] = mapped_column(String(20), default="public", nullable=False)
    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    asset_ids_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False, index=True)
    validation_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    logs_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    platform_item_id: Mapped[str | None] = mapped_column(String(300))
    platform_url: Mapped[str | None] = mapped_column(String(2000))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, index=True
    )

    post: Mapped[Post] = relationship(back_populates="publications")
    platform_version: Mapped[PlatformVersion | None] = relationship()
