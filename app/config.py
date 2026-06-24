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
    import_media_delay_min_seconds: float
    import_media_delay_max_seconds: float
    import_media_retry_attempts: int
    browser_user_agent: str | None
    browser_timezone: str | None
    publish_upload_delay_min_seconds: float
    publish_upload_delay_max_seconds: float
    publish_typing_pause_min_seconds: float
    publish_typing_pause_max_seconds: float
    publish_rest_every_actions: int
    publish_rest_min_seconds: float
    publish_rest_max_seconds: float
    daily_publish_limit: int
    publish_day_timezone: str


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
    import_delay_min = _float_env("CONTENT_HUB_IMPORT_MEDIA_DELAY_MIN_SECONDS", 1.0)
    import_delay_max = max(
        import_delay_min,
        _float_env("CONTENT_HUB_IMPORT_MEDIA_DELAY_MAX_SECONDS", 3.0),
    )
    upload_delay_min = _float_env("CONTENT_HUB_PUBLISH_UPLOAD_DELAY_MIN_SECONDS", 1.0)
    upload_delay_max = max(
        upload_delay_min,
        _float_env("CONTENT_HUB_PUBLISH_UPLOAD_DELAY_MAX_SECONDS", 3.0),
    )
    typing_pause_min = _float_env("CONTENT_HUB_PUBLISH_TYPING_PAUSE_MIN_SECONDS", 0.2)
    typing_pause_max = max(
        typing_pause_min,
        _float_env("CONTENT_HUB_PUBLISH_TYPING_PAUSE_MAX_SECONDS", 0.5),
    )
    publish_rest_min = _float_env("CONTENT_HUB_PUBLISH_REST_MIN_SECONDS", 4.0)
    publish_rest_max = max(
        publish_rest_min,
        _float_env("CONTENT_HUB_PUBLISH_REST_MAX_SECONDS", 8.0),
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
        import_media_delay_min_seconds=import_delay_min,
        import_media_delay_max_seconds=import_delay_max,
        import_media_retry_attempts=max(
            1,
            int(os.getenv("CONTENT_HUB_IMPORT_MEDIA_RETRY_ATTEMPTS", 3)),
        ),
        browser_user_agent=os.getenv("CONTENT_HUB_BROWSER_USER_AGENT") or None,
        browser_timezone=os.getenv("CONTENT_HUB_BROWSER_TIMEZONE") or None,
        publish_upload_delay_min_seconds=upload_delay_min,
        publish_upload_delay_max_seconds=upload_delay_max,
        publish_typing_pause_min_seconds=typing_pause_min,
        publish_typing_pause_max_seconds=typing_pause_max,
        publish_rest_every_actions=max(
            0,
            int(os.getenv("CONTENT_HUB_PUBLISH_REST_EVERY_ACTIONS", 6)),
        ),
        publish_rest_min_seconds=publish_rest_min,
        publish_rest_max_seconds=publish_rest_max,
        daily_publish_limit=max(
            0,
            int(os.getenv("CONTENT_HUB_DAILY_PUBLISH_LIMIT", 20)),
        ),
        publish_day_timezone=os.getenv("CONTENT_HUB_PUBLISH_DAY_TIMEZONE", "Asia/Shanghai"),
    )


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


settings = get_settings()
