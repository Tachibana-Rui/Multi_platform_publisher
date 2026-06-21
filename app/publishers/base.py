from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class PublishAsset:
    id: str
    path: Path
    media_type: str
    mime_type: str
    file_size: int
    width: int | None
    height: int | None
    duration_seconds: float | None


@dataclass(frozen=True)
class PublishSnapshot:
    id: str
    post_id: str
    platform: str
    visibility: str
    title: str
    body: str
    assets: list[PublishAsset]


StatusCallback = Callable[[str, str], None]
ContentCallback = Callable[[str, str], None]


class PublicationCancelled(RuntimeError):
    pass
