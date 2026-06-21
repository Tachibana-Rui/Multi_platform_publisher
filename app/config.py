from dataclasses import dataclass
from pathlib import Path
import os
import json


ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    data_dir: Path
    upload_dir: Path
    browser_profile_dir: Path
    database_url: str
    max_upload_bytes: int
    max_import_assets: int
    max_import_total_bytes: int


def get_settings() -> Settings:
    data_dir = Path(os.getenv("CONTENT_HUB_DATA_DIR", ROOT_DIR / "data")).resolve()
    configured_upload_dir = None
    storage_settings_path = data_dir / "storage_settings.json"
    if "CONTENT_HUB_UPLOAD_DIR" not in os.environ and storage_settings_path.is_file():
        try:
            configured_upload_dir = json.loads(
                storage_settings_path.read_text(encoding="utf-8")
            ).get("upload_dir")
        except (OSError, json.JSONDecodeError, AttributeError):
            configured_upload_dir = None
    upload_dir = Path(os.getenv(
        "CONTENT_HUB_UPLOAD_DIR",
        configured_upload_dir or ROOT_DIR / "storage" / "uploads",
    )).expanduser().resolve()
    browser_profile_dir = Path(
        os.getenv("CONTENT_HUB_BROWSER_PROFILE_DIR", data_dir / "browser_profiles")
    ).resolve()
    database_url = os.getenv(
        "CONTENT_HUB_DATABASE_URL",
        f"sqlite:///{(data_dir / 'content_hub.db').as_posix()}",
    )
    return Settings(
        data_dir=data_dir,
        upload_dir=upload_dir,
        browser_profile_dir=browser_profile_dir,
        database_url=database_url,
        max_upload_bytes=int(os.getenv("CONTENT_HUB_MAX_UPLOAD_BYTES", 2 * 1024**3)),
        max_import_assets=int(os.getenv("CONTENT_HUB_MAX_IMPORT_ASSETS", 30)),
        max_import_total_bytes=int(
            os.getenv("CONTENT_HUB_MAX_IMPORT_TOTAL_BYTES", 4 * 1024**3)
        ),
    )


settings = get_settings()
