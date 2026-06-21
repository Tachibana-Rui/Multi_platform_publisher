import hashlib
import json
from pathlib import Path
import subprocess
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

from .config import settings


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def classify_file(filename: str) -> tuple[str, str]:
    suffix = Path(filename).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image", suffix
    if suffix in VIDEO_EXTENSIONS:
        return "video", suffix
    raise HTTPException(status_code=415, detail="仅支持常见图片或视频格式")


async def save_upload(upload: UploadFile, post_id: str) -> dict:
    original_name = Path(upload.filename or "unnamed").name[:255]
    try:
        media_type, suffix = classify_file(original_name)
    except Exception:
        await upload.close()
        raise
    post_dir = settings.upload_dir / post_id
    post_dir.mkdir(parents=True, exist_ok=True)
    storage_name = f"{post_id}/{uuid4().hex}{suffix}"
    target = settings.upload_dir / storage_name
    digest = hashlib.sha256()
    size = 0

    try:
        with target.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(status_code=413, detail="文件超过允许的最大尺寸")
                digest.update(chunk)
                output.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

    width, height, duration = inspect_media(target, media_type)
    return {
        "original_name": original_name,
        "storage_name": storage_name,
        "media_type": media_type,
        "mime_type": upload.content_type or "application/octet-stream",
        "file_size": size,
        "checksum": digest.hexdigest(),
        "width": width,
        "height": height,
        "duration_seconds": duration,
    }


def inspect_media(path: Path, media_type: str) -> tuple[int | None, int | None, float | None]:
    if media_type == "image":
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                return image.width, image.height, None
        except (UnidentifiedImageError, OSError):
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail="图片文件无法识别或已经损坏")

    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "stream=width,height:format=duration", "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
        payload = json.loads(result.stdout)
        video_stream = next(
            (stream for stream in payload.get("streams", []) if stream.get("width")), {}
        )
        duration_value = payload.get("format", {}).get("duration")
        return (
            video_stream.get("width"),
            video_stream.get("height"),
            round(float(duration_value), 3) if duration_value else None,
        )
    except (FileNotFoundError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return None, None, None
