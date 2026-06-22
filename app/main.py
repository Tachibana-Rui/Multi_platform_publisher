from contextlib import asynccontextmanager
import asyncio
from copy import deepcopy
from collections.abc import Callable
import inspect
import json
from pathlib import Path
import re
import shutil
import threading
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .assets import save_upload
from .account_manager import ACCOUNT_PLATFORMS, account_manager
from .config import ROOT_DIR, settings
from .database import SessionLocal, get_db, init_db
from .library_tasks import get_scan_state, is_scanning, schedule_scan
from .content_matcher import (
    add_source_root,
    confirm_match,
    match_post_images,
    match_post_images_in_folder,
    serialize_match,
)
from .models import (
    AssetMatch, LibraryFolder, MediaAsset, OriginalAsset, PlatformPublication,
    Post, SourceRoot, Tag,
)
from .publish_agent import ACTIVE_STATUSES, publication_agent, serialize_publication
from .doubao import generate_copy
from .douyin_importer import import_public_douyin, normalize_douyin_url
from .llm_settings import get_public_settings, update_settings
from .platform_adapter import (
    get_or_create_version,
    resolve_selected_assets,
    selected_asset_ids,
    serialize_version,
    validate_platform,
)
from .schemas import (
    DashboardResponse,
    BatchImportRequest,
    PostCreate,
    PostResponse,
    PostUpdate,
    MatchOriginalsRequest,
    ManualMatchRequest,
    GenerateCopyRequest,
    LLMSettingsUpdate,
    PublicationBatchCreate,
    PublicationCreate,
    PlatformVersionUpdate,
    SourceRootCreate,
    StorageSettingsUpdate,
    XiaohongshuImportRequest,
)
from .xiaohongshu import import_public_note, normalize_source_url
from .system_dialogs import pick_windows_folder
from .storage_settings import get_storage_settings, update_storage_location


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    publication_agent.recover_interrupted()
    yield


app = FastAPI(title="Content Hub", version="0.4.1", lifespan=lifespan)


def serialize_asset(asset: MediaAsset) -> dict:
    return {
        "id": asset.id,
        "original_name": asset.original_name,
        "media_type": asset.media_type,
        "mime_type": asset.mime_type,
        "file_size": asset.file_size,
        "checksum": asset.checksum,
        "width": asset.width,
        "height": asset.height,
        "duration_seconds": asset.duration_seconds,
        "position": asset.position,
        "url": f"/media/{asset.storage_name}",
        "created_at": asset.created_at,
    }


def serialize_post(post: Post) -> dict:
    return {
        "id": post.id,
        "title": post.title,
        "body": post.body,
        "source_platform": post.source_platform,
        "source_url": post.source_url,
        "content_type": post.content_type,
        "status": post.status,
        "tags": [tag.name for tag in post.tags],
        "assets": [serialize_asset(asset) for asset in post.assets],
        "created_at": post.created_at,
        "updated_at": post.updated_at,
    }


def get_post_or_404(db: Session, post_id: str) -> Post:
    post = db.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="内容不存在")
    return post


def resolve_tags(db: Session, names: list[str]) -> list[Tag]:
    tags: list[Tag] = []
    for name in names:
        tag = db.scalar(select(Tag).where(func.lower(Tag.name) == name.lower()))
        if tag is None:
            tag = Tag(name=name)
            db.add(tag)
        tags.append(tag)
    return tags


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/accounts")
def list_accounts() -> list[dict]:
    return account_manager.list_statuses()


@app.post("/api/accounts/check", status_code=202)
def check_accounts() -> dict:
    account_manager.check_all()
    return {"accepted": True}


@app.post("/api/accounts/{platform}/login", status_code=202)
def login_account(platform: str) -> dict:
    if platform not in ACCOUNT_PLATFORMS:
        raise HTTPException(status_code=422, detail="暂不支持该平台账号")
    if not account_manager.start(platform, visible=True):
        raise HTTPException(status_code=409, detail="该平台正在检测、登录或发布中")
    return {"accepted": True, "platform": platform}


@app.get("/api/settings/llm")
def read_llm_settings() -> dict:
    return get_public_settings()


@app.put("/api/settings/llm")
def save_llm_settings(payload: LLMSettingsUpdate) -> dict:
    values = payload.model_dump(exclude_unset=True)
    return update_settings(
        api_key=values.get("api_key"),
        model=values.get("model"),
        clear_api_key=values.get("clear_api_key", False),
    )


@app.get("/api/dashboard", response_model=DashboardResponse)
def dashboard(db: Session = Depends(get_db)) -> dict:
    total_posts = db.scalar(select(func.count()).select_from(Post)) or 0
    draft_posts = db.scalar(select(func.count()).select_from(Post).where(Post.status == "draft")) or 0
    ready_posts = db.scalar(select(func.count()).select_from(Post).where(Post.status == "ready")) or 0
    total_assets = db.scalar(select(func.count()).select_from(MediaAsset)) or 0
    total_bytes = db.scalar(select(func.coalesce(func.sum(MediaAsset.file_size), 0))) or 0
    return {
        "total_posts": total_posts,
        "draft_posts": draft_posts,
        "ready_posts": ready_posts,
        "total_assets": total_assets,
        "total_bytes": total_bytes,
    }


def serialize_source_root(root: SourceRoot) -> dict:
    return {
        "id": root.id,
        "name": root.name,
        "path": root.path,
        "last_scanned_at": root.last_scanned_at,
        "folder_count": len(root.folders),
        "asset_count": sum(len(folder.assets) for folder in root.folders),
        "scan": get_scan_state(root.id),
    }


@app.post("/api/system/pick-folder")
def pick_folder() -> dict:
    selected = pick_windows_folder()
    return {"path": selected, "cancelled": selected is None}


@app.post("/api/system/pick-storage-folder")
def pick_storage_folder() -> dict:
    selected = pick_windows_folder("选择 Content Hub 素材存储目录")
    return {"path": selected, "cancelled": selected is None}


@app.post("/api/system/pick-original-folder")
def pick_original_folder() -> dict:
    selected = pick_windows_folder("选择本次原图匹配目录")
    return {"path": selected, "cancelled": selected is None}


@app.get("/api/settings/storage")
def read_storage_settings() -> dict:
    return get_storage_settings()


@app.put("/api/settings/storage")
def save_storage_settings(
    payload: StorageSettingsUpdate,
    db: Session = Depends(get_db),
) -> dict:
    active = db.scalar(select(func.count()).select_from(PlatformPublication).where(
        PlatformPublication.status.in_(ACTIVE_STATUSES)
    )) or 0
    if active:
        raise HTTPException(status_code=409, detail="有发布任务正在运行，请完成或取消后再更改存储目录")
    return update_storage_location(payload.path)


def serialize_library_folder(folder: LibraryFolder) -> dict:
    return {
        "id": folder.id,
        "source_root_id": folder.source_root_id,
        "path": folder.path,
        "relative_path": folder.relative_path,
        "folder_name": folder.folder_name,
        "coser_name": folder.coser_name,
        "character_name": folder.character_name,
        "shoot_date": folder.shoot_date,
        "parse_status": folder.parse_status,
        "asset_count": len(folder.assets),
    }


@app.get("/api/library/roots")
def list_source_roots(db: Session = Depends(get_db)) -> list[dict]:
    roots = db.scalars(select(SourceRoot).order_by(SourceRoot.created_at)).all()
    return [serialize_source_root(root) for root in roots]


@app.post("/api/library/roots", status_code=201)
def create_source_root(payload: SourceRootCreate, db: Session = Depends(get_db)) -> dict:
    return serialize_source_root(add_source_root(db, payload.path, payload.name))


@app.delete("/api/library/roots/{root_id}", status_code=204)
def delete_source_root(root_id: str, db: Session = Depends(get_db)) -> None:
    root = db.get(SourceRoot, root_id)
    if not root:
        raise HTTPException(status_code=404, detail="素材库目录不存在")
    if is_scanning(root_id):
        raise HTTPException(status_code=409, detail="目录正在扫描，请等待扫描完成后再移除")
    db.delete(root)
    db.commit()


@app.post("/api/library/roots/{root_id}/scan", status_code=202)
def scan_library_root(root_id: str, db: Session = Depends(get_db)) -> dict:
    root = db.get(SourceRoot, root_id)
    if not root:
        raise HTTPException(status_code=404, detail="素材库目录不存在")
    return schedule_scan(root_id)


@app.get("/api/library/roots/{root_id}/scan")
def get_library_scan_status(root_id: str, db: Session = Depends(get_db)) -> dict:
    if not db.get(SourceRoot, root_id):
        raise HTTPException(status_code=404, detail="素材库目录不存在")
    return get_scan_state(root_id)


@app.get("/api/library/folders")
def search_library_folders(
    search: str | None = Query(default=None, max_length=100),
    coser_name: str | None = Query(default=None, max_length=150),
    character_name: str | None = Query(default=None, max_length=150),
    shoot_date: str | None = Query(default=None, max_length=10),
    source_root_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[dict]:
    statement = select(LibraryFolder).order_by(LibraryFolder.shoot_date.desc(), LibraryFolder.folder_name)
    if source_root_id:
        statement = statement.where(LibraryFolder.source_root_id == source_root_id)
    if search:
        pattern = f"%{search.strip()}%"
        statement = statement.where(or_(
            LibraryFolder.folder_name.ilike(pattern),
            LibraryFolder.coser_name.ilike(pattern),
            LibraryFolder.character_name.ilike(pattern),
        ))
    if coser_name:
        statement = statement.where(LibraryFolder.coser_name.ilike(f"%{coser_name.strip()}%"))
    if character_name:
        statement = statement.where(LibraryFolder.character_name.ilike(f"%{character_name.strip()}%"))
    if shoot_date:
        statement = statement.where(LibraryFolder.shoot_date == shoot_date)
    return [serialize_library_folder(folder) for folder in db.scalars(statement).all()]


@app.get("/api/library/folders/{folder_id}/assets")
def list_library_assets(folder_id: str, db: Session = Depends(get_db)) -> list[dict]:
    folder = db.get(LibraryFolder, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="素材目录不存在")
    return [{
        "id": asset.id,
        "filename": asset.filename,
        "path": asset.path,
        "width": asset.width,
        "height": asset.height,
        "file_size": asset.file_size,
        "phash": asset.phash,
    } for asset in folder.assets]


@app.get("/api/posts", response_model=list[PostResponse])
def list_posts(
    search: str | None = Query(default=None, max_length=100),
    status: str | None = Query(default=None),
    content_type: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[dict]:
    statement = select(Post).order_by(Post.updated_at.desc())
    if search:
        pattern = f"%{search.strip()}%"
        statement = statement.where(or_(Post.title.ilike(pattern), Post.body.ilike(pattern)))
    if status:
        statement = statement.where(Post.status == status)
    if content_type:
        statement = statement.where(Post.content_type == content_type)
    return [serialize_post(post) for post in db.scalars(statement).unique().all()]


@app.post("/api/posts", response_model=PostResponse, status_code=201)
def create_post(payload: PostCreate, db: Session = Depends(get_db)) -> dict:
    values = payload.model_dump(exclude={"tags"})
    post = Post(**values, tags=resolve_tags(db, payload.tags))
    db.add(post)
    db.commit()
    db.refresh(post)
    return serialize_post(post)


@app.post("/api/imports/xiaohongshu", response_model=PostResponse, status_code=201)
async def import_xiaohongshu(
    payload: XiaohongshuImportRequest,
    db: Session = Depends(get_db),
) -> dict:
    if not payload.confirm_rights:
        raise HTTPException(status_code=422, detail="请确认该作品由你原创或已获得导入授权")
    return await import_one_post(db, "xiaohongshu", payload.url)


async def import_one_post(
    db: Session,
    platform: str,
    source_text: str,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    normalizer = normalize_source_url if platform == "xiaohongshu" else normalize_douyin_url
    submitted_url = normalizer(source_text)
    if db.scalar(select(Post).where(Post.source_url == submitted_url)):
        raise HTTPException(status_code=409, detail="该作品已经导入 Content Hub")

    post_id = str(uuid4())
    importer = import_public_note if platform == "xiaohongshu" else import_public_douyin
    parameters = inspect.signature(importer).parameters
    supports_progress = "progress_callback" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    if progress_callback and supports_progress:
        canonical_url, note, asset_values = await importer(
            submitted_url, post_id, progress_callback=progress_callback
        )
    else:
        canonical_url, note, asset_values = await importer(submitted_url, post_id)
    if db.scalar(select(Post).where(Post.source_url == canonical_url)):
        shutil.rmtree(settings.upload_dir / post_id, ignore_errors=True)
        raise HTTPException(status_code=409, detail="该作品已经导入 Content Hub")

    media_types = {asset["media_type"] for asset in asset_values}
    content_type = (
        "video" if "video" in media_types
        else "image" if len(asset_values) == 1
        else "gallery"
    )
    post = Post(
        id=post_id,
        title=note.title,
        body=note.body,
        source_platform=platform,
        source_url=canonical_url,
        content_type=content_type,
        status="draft",
        tags=resolve_tags(db, note.tags),
        assets=[MediaAsset(**values) for values in asset_values],
    )
    try:
        db.add(post)
        db.commit()
        db.refresh(post)
        return serialize_post(post)
    except Exception:
        db.rollback()
        shutil.rmtree(settings.upload_dir / post_id, ignore_errors=True)
        raise


def extract_batch_urls(payload: BatchImportRequest) -> list[str]:
    if not payload.confirm_rights:
        raise HTTPException(status_code=422, detail="请确认这些作品由你原创或已获得导入授权")
    urls = re.findall(r"https?://[^\s<>\"']+", payload.text)
    if not urls:
        raise HTTPException(status_code=422, detail="没有找到有效链接")
    if len(urls) > 30:
        raise HTTPException(status_code=422, detail="每次最多批量导入 30 个链接")
    return urls


async def process_batch_import(
    payload: BatchImportRequest,
    db: Session,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    urls = extract_batch_urls(payload)

    results = []
    seen: set[str] = set()
    for current_index, raw_url in enumerate(urls, start=1):
        if progress_callback:
            progress_callback({
                "status": "running",
                "current_index": current_index,
                "current_name": raw_url,
                "image_downloaded": 0,
                "image_total": 0,
            })
        try:
            normalizer = normalize_source_url if payload.platform == "xiaohongshu" else normalize_douyin_url
            normalized = normalizer(raw_url)
            if normalized in seen:
                results.append({
                    "url": normalized, "status": "skipped", "error": "本批次中的重复链接",
                })
                if progress_callback:
                    progress_callback({"results": deepcopy(results), "item_complete": True})
                continue
            seen.add(normalized)
            def report_post(update: dict) -> None:
                if progress_callback:
                    progress_callback({
                        "current_index": current_index,
                        "current_name": update.get("post_name") or normalized,
                        "image_downloaded": int(update.get("image_downloaded") or 0),
                        "image_total": int(update.get("image_total") or 0),
                    })

            post = await import_one_post(
                db, payload.platform, normalized, progress_callback=report_post
            )
            results.append({"url": normalized, "status": "imported", "post": post})
        except HTTPException as exc:
            db.rollback()
            results.append({
                "url": raw_url,
                "status": "failed" if exc.status_code != 409 else "skipped",
                "error": str(exc.detail),
                "status_code": exc.status_code,
            })
        except Exception as exc:
            db.rollback()
            results.append({"url": raw_url, "status": "failed", "error": str(exc)[:500]})
        if progress_callback:
            progress_callback({"results": deepcopy(results), "item_complete": True})
    return {
        "platform": payload.platform,
        "total": len(results),
        "imported": sum(item["status"] == "imported" for item in results),
        "skipped": sum(item["status"] == "skipped" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "results": results,
    }


@app.post("/api/imports/batch", status_code=207)
async def import_links_batch(
    payload: BatchImportRequest,
    db: Session = Depends(get_db),
) -> dict:
    return await process_batch_import(payload, db)


_IMPORT_JOBS: dict[str, dict] = {}
_IMPORT_JOBS_LOCK = threading.RLock()


def update_import_job(job_id: str, values: dict) -> None:
    with _IMPORT_JOBS_LOCK:
        job = _IMPORT_JOBS.get(job_id)
        if not job:
            return
        job.update(values)
        results = job.get("results", [])
        job["imported"] = sum(item.get("status") == "imported" for item in results)
        job["skipped"] = sum(item.get("status") == "skipped" for item in results)
        job["failed"] = sum(item.get("status") == "failed" for item in results)
        total = max(int(job.get("total") or 0), 1)
        completed = len(results)
        current_total = int(job.get("image_total") or 0)
        current_done = int(job.get("image_downloaded") or 0)
        current_fraction = current_done / current_total if current_total else 0
        job["progress"] = min(100, round((completed + current_fraction) / total * 100, 1))
        if values.get("item_complete"):
            job["progress"] = min(100, round(completed / total * 100, 1))
        job.pop("item_complete", None)


def run_import_job(job_id: str, payload: BatchImportRequest) -> None:
    async def run() -> None:
        with SessionLocal() as db:
            try:
                result = await process_batch_import(
                    payload, db, lambda update: update_import_job(job_id, update)
                )
                update_import_job(job_id, {
                    **result,
                    "status": "completed",
                    "progress": 100,
                    "results": result["results"],
                })
            except Exception as exc:
                update_import_job(job_id, {
                    "status": "failed",
                    "error": str(getattr(exc, "detail", exc))[:500],
                })

    asyncio.run(run())


@app.post("/api/imports/batch-jobs", status_code=202)
def create_import_job(payload: BatchImportRequest) -> dict:
    urls = extract_batch_urls(payload)
    job_id = str(uuid4())
    job = {
        "id": job_id,
        "platform": payload.platform,
        "status": "queued",
        "total": len(urls),
        "current_index": 0,
        "current_name": "等待开始",
        "image_downloaded": 0,
        "image_total": 0,
        "progress": 0,
        "imported": 0,
        "skipped": 0,
        "failed": 0,
        "results": [],
        "error": None,
    }
    with _IMPORT_JOBS_LOCK:
        if len(_IMPORT_JOBS) >= 100:
            finished_ids = [
                key for key, value in _IMPORT_JOBS.items()
                if value.get("status") in {"completed", "failed"}
            ]
            for key in finished_ids[: max(1, len(_IMPORT_JOBS) - 99)]:
                _IMPORT_JOBS.pop(key, None)
        _IMPORT_JOBS[job_id] = job
    threading.Thread(
        target=run_import_job,
        args=(job_id, payload),
        name=f"import-{job_id[:8]}",
        daemon=True,
    ).start()
    return deepcopy(job)


@app.get("/api/imports/batch-jobs/{job_id}")
def get_import_job(job_id: str) -> dict:
    with _IMPORT_JOBS_LOCK:
        job = _IMPORT_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="导入批次不存在或已过期")
        return deepcopy(job)


@app.get("/api/posts/{post_id}", response_model=PostResponse)
def get_post(post_id: str, db: Session = Depends(get_db)) -> dict:
    return serialize_post(get_post_or_404(db, post_id))


@app.get("/api/posts/{post_id}/platform-versions/{platform}")
def get_platform_version(post_id: str, platform: str, db: Session = Depends(get_db)) -> dict:
    post = get_post_or_404(db, post_id)
    version = get_or_create_version(db, post, platform)
    return serialize_version(version, post)


@app.put("/api/posts/{post_id}/platform-versions/{platform}")
def update_platform_version(
    post_id: str,
    platform: str,
    payload: PlatformVersionUpdate,
    db: Session = Depends(get_db),
) -> dict:
    post = get_post_or_404(db, post_id)
    version = get_or_create_version(db, post, platform)
    resolve_selected_assets(post, payload.selected_asset_ids)
    text_changed = payload.title != version.title or payload.body != version.body
    version.title = payload.title
    version.body = payload.body
    version.selected_asset_ids_json = json.dumps(payload.selected_asset_ids)
    if text_changed:
        version.content_source = "manual"
    db.commit()
    db.refresh(version)
    return serialize_version(version, post)


@app.post("/api/posts/{post_id}/platform-versions/{platform}/generate")
async def generate_platform_version(
    post_id: str,
    platform: str,
    payload: GenerateCopyRequest,
    db: Session = Depends(get_db),
) -> dict:
    validate_platform(platform)
    post = get_post_or_404(db, post_id)
    version = get_or_create_version(db, post, platform)
    requested_ids = (
        payload.selected_asset_ids
        if "selected_asset_ids" in payload.model_fields_set
        else selected_asset_ids(version)
    )
    assets = resolve_selected_assets(post, requested_ids)
    image_assets = [asset for asset in assets if asset.media_type == "image"][:4]
    generated = await generate_copy(post, platform, image_assets, payload.custom_prompt)
    version.title = generated["title"]
    version.body = generated["body"]
    version.selected_asset_ids_json = json.dumps(requested_ids)
    version.generation_count += 1
    version.content_source = "llm"
    version.last_prompt = generated["prompt"]
    version.last_model = generated["model"]
    db.commit()
    db.refresh(version)
    return serialize_version(version, post)


@app.post("/api/posts/{post_id}/match-originals")
def run_original_matcher(
    post_id: str,
    payload: MatchOriginalsRequest,
    db: Session = Depends(get_db),
) -> dict:
    post = get_post_or_404(db, post_id)
    return match_post_images(
        db,
        post,
        source_root_id=payload.source_root_id,
        coser_name=payload.coser_name,
        character_name=payload.character_name,
        shoot_date=payload.shoot_date,
    )


@app.post("/api/posts/{post_id}/match-originals/manual")
def run_manual_original_matcher(
    post_id: str,
    payload: ManualMatchRequest,
    db: Session = Depends(get_db),
) -> dict:
    post = get_post_or_404(db, post_id)
    return match_post_images_in_folder(db, post, payload.path)


@app.get("/api/posts/{post_id}/matches")
def list_post_matches(post_id: str, db: Session = Depends(get_db)) -> list[dict]:
    get_post_or_404(db, post_id)
    matches = db.scalars(
        select(AssetMatch).join(MediaAsset).where(MediaAsset.post_id == post_id)
        .order_by(MediaAsset.position)
    ).all()
    return [serialize_match(match) for match in matches]


@app.post("/api/matches/{match_id}/confirm")
def confirm_original_match(match_id: str, db: Session = Depends(get_db)) -> dict:
    match = db.get(AssetMatch, match_id)
    if not match:
        raise HTTPException(status_code=404, detail="匹配记录不存在")
    return confirm_match(db, match)


@app.patch("/api/posts/{post_id}", response_model=PostResponse)
def update_post(post_id: str, payload: PostUpdate, db: Session = Depends(get_db)) -> dict:
    post = get_post_or_404(db, post_id)
    values = payload.model_dump(exclude_unset=True)
    tag_names = values.pop("tags", None)
    for key, value in values.items():
        setattr(post, key, value)
    if tag_names is not None:
        post.tags = resolve_tags(db, tag_names)
    db.commit()
    db.refresh(post)
    return serialize_post(post)


@app.delete("/api/posts/{post_id}", status_code=204)
def delete_post(post_id: str, db: Session = Depends(get_db)) -> None:
    post = get_post_or_404(db, post_id)
    paths = [settings.upload_dir / asset.storage_name for asset in post.assets]
    db.delete(post)
    db.commit()
    for path in paths:
        path.unlink(missing_ok=True)
    post_dir = settings.upload_dir / post_id
    if post_dir.parent == settings.upload_dir:
        shutil.rmtree(post_dir, ignore_errors=True)


@app.post("/api/posts/{post_id}/assets", response_model=PostResponse, status_code=201)
async def upload_assets(
    post_id: str,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
) -> dict:
    post = get_post_or_404(db, post_id)
    if len(files) > 30:
        raise HTTPException(status_code=400, detail="每次最多上传 30 个文件")
    created_paths: list[Path] = []
    try:
        for index, upload in enumerate(files, start=len(post.assets)):
            details = await save_upload(upload, post_id)
            created_paths.append(settings.upload_dir / details["storage_name"])
            post.assets.append(MediaAsset(**details, position=index))
        if post.assets:
            types = {asset.media_type for asset in post.assets}
            post.content_type = "video" if types == {"video"} else "image" if types == {"image"} and len(post.assets) == 1 else "gallery"
        db.commit()
        db.refresh(post)
        return serialize_post(post)
    except Exception:
        db.rollback()
        for path in created_paths:
            path.unlink(missing_ok=True)
        post_dir = settings.upload_dir / post_id
        if post_dir.is_dir() and not any(post_dir.iterdir()):
            post_dir.rmdir()
        raise


@app.delete("/api/posts/{post_id}/assets/{asset_id}", response_model=PostResponse)
def delete_asset(post_id: str, asset_id: str, db: Session = Depends(get_db)) -> dict:
    post = get_post_or_404(db, post_id)
    asset = next((item for item in post.assets if item.id == asset_id), None)
    if not asset:
        raise HTTPException(status_code=404, detail="素材不存在")
    path = settings.upload_dir / asset.storage_name
    match = db.scalar(select(AssetMatch).where(AssetMatch.downloaded_asset_id == asset.id))
    copied_path = settings.upload_dir / match.copied_storage_name if match and match.copied_storage_name else None
    db.delete(asset)
    db.commit()
    path.unlink(missing_ok=True)
    if copied_path:
        copied_path.unlink(missing_ok=True)
    post_dir = settings.upload_dir / post_id
    if post_dir.is_dir() and not any(post_dir.iterdir()):
        post_dir.rmdir()
    db.refresh(post)
    return serialize_post(post)


@app.get("/api/publications")
def list_publications(
    post_id: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[dict]:
    statement = select(PlatformPublication).order_by(PlatformPublication.created_at.desc())
    if post_id:
        statement = statement.where(PlatformPublication.post_id == post_id)
    if platform:
        validate_platform(platform)
        statement = statement.where(PlatformPublication.platform == platform)
    if status:
        statement = statement.where(PlatformPublication.status == status)
    return [serialize_publication(item) for item in db.scalars(statement).all()]


@app.post("/api/publications", status_code=201)
def create_publication(payload: PublicationCreate, db: Session = Depends(get_db)) -> dict:
    post = get_post_or_404(db, payload.post_id)
    publication = build_publication(db, post, payload.platform, payload.visibility)
    db.add(publication)
    db.commit()
    db.refresh(publication)
    publication_agent.start(publication.id)
    return serialize_publication(publication)


def build_publication(
    db: Session,
    post: Post,
    platform: str,
    visibility: str,
) -> PlatformPublication:
    version = get_or_create_version(db, post, platform)
    asset_ids = selected_asset_ids(version)
    resolve_selected_assets(post, asset_ids)
    if not asset_ids:
        raise HTTPException(status_code=422, detail="请先在平台发布窗口中选择素材")
    active = db.scalar(select(PlatformPublication).where(
        PlatformPublication.post_id == post.id,
        PlatformPublication.platform == platform,
        PlatformPublication.status.in_(ACTIVE_STATUSES),
    ))
    if active:
        raise HTTPException(status_code=409, detail="该内容在此平台已有进行中的发布任务")
    publication = PlatformPublication(
        post_id=post.id,
        platform_version_id=version.id,
        platform=platform,
        visibility=visibility,
        title=version.title,
        body=version.body,
        asset_ids_json=json.dumps(asset_ids),
        logs_json=json.dumps([{
            "at": publication_time().isoformat(),
            "status": "pending",
            "message": "发布任务已创建",
        }], ensure_ascii=False),
    )
    return publication


@app.post("/api/publications/batch", status_code=207)
def create_publications_batch(
    payload: PublicationBatchCreate,
    db: Session = Depends(get_db),
) -> dict:
    post = get_post_or_404(db, payload.post_id)
    # Create missing drafts before adding publications because the adapter's
    # first-time draft creation commits its own transaction.
    for platform in payload.platforms:
        get_or_create_version(db, post, platform)
    publications: list[PlatformPublication] = []
    skipped: list[dict] = []
    for platform in payload.platforms:
        try:
            publication = build_publication(db, post, platform, payload.visibility)
            db.add(publication)
            publications.append(publication)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            skipped.append({"platform": platform, "error": str(exc.detail)})
    db.commit()
    for publication in publications:
        db.refresh(publication)
        publication_agent.start(publication.id)
    return {
        "created": [serialize_publication(publication) for publication in publications],
        "skipped": skipped,
        "total": len(payload.platforms),
    }


def publication_time():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


@app.get("/api/publications/{publication_id}")
def get_publication(publication_id: str, db: Session = Depends(get_db)) -> dict:
    publication = db.get(PlatformPublication, publication_id)
    if not publication:
        raise HTTPException(status_code=404, detail="发布任务不存在")
    return serialize_publication(publication)


@app.post("/api/publications/{publication_id}/confirm")
def confirm_publication(publication_id: str, db: Session = Depends(get_db)) -> dict:
    publication = db.get(PlatformPublication, publication_id)
    if not publication:
        raise HTTPException(status_code=404, detail="发布任务不存在")
    if publication.status in {"published", "submitted"}:
        return {"accepted": True, "already_published": True, "publication_id": publication_id}
    if publication.status != "review_pending":
        raise HTTPException(status_code=409, detail="任务尚未准备好，不能确认发布")
    if not publication_agent.confirm(publication_id):
        raise HTTPException(status_code=409, detail="发布窗口已断开，请重试任务")
    return {"accepted": True, "publication_id": publication_id}


@app.post("/api/publications/{publication_id}/retry", status_code=202)
def retry_publication(publication_id: str, db: Session = Depends(get_db)) -> dict:
    publication = db.get(PlatformPublication, publication_id)
    if not publication:
        raise HTTPException(status_code=404, detail="发布任务不存在")
    if publication.status not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="只有失败或已取消的任务可以重试")
    publication.status = "pending"
    publication.error_message = None
    db.commit()
    if not publication_agent.start(publication_id):
        raise HTTPException(status_code=409, detail="任务已经在运行")
    return serialize_publication(publication)


@app.post("/api/publications/{publication_id}/cancel")
def cancel_publication(publication_id: str, db: Session = Depends(get_db)) -> dict:
    publication = db.get(PlatformPublication, publication_id)
    if not publication:
        raise HTTPException(status_code=404, detail="发布任务不存在")
    if publication.status not in ACTIVE_STATUSES and publication.status != "pending":
        raise HTTPException(status_code=409, detail="当前任务不能取消")
    if not publication_agent.cancel(publication_id):
        publication.status = "cancelled"
        publication.error_message = "发布任务已取消"
        db.commit()
    return {"accepted": True, "publication_id": publication_id}


@app.get("/media/{storage_path:path}")
def serve_media(storage_path: str):
    target = (settings.upload_dir / storage_path).resolve()
    if not target.is_relative_to(settings.upload_dir) or not target.is_file():
        raise HTTPException(status_code=404, detail="素材文件不存在")
    return FileResponse(target)


app.mount("/", StaticFiles(directory=ROOT_DIR / "app" / "static", html=True), name="frontend")
