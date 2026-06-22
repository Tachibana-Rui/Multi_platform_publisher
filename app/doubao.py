from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
import re

from fastapi import HTTPException
import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import settings
from .llm_settings import get_private_settings
from .models import MediaAsset, Post


PLATFORM_NAMES = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "bilibili": "B站",
    "kuaishou": "快手",
    "wechat_channels": "视频号",
}
SYSTEM_PROMPT = """你是专业的跨平台社交媒体文案编辑。根据用户提供的角色、标签和图片，为指定平台生成自然、有辨识度且不过度营销的中文标题、正文和标签。
不得虚构图片中无法确认的信息，不添加站外导流，不使用夸大承诺。
必须生成正好 5 个与内容相关、适合目标平台的中文标签。正文 body 中不要自行添加标签。
只输出一个 JSON 对象，格式为 {"title":"标题","body":"正文","tags":["标签1","标签2","标签3","标签4","标签5"]}，不要输出 Markdown 或额外解释。"""


GENERIC_TAGS = {
    "cos", "cosplay", "正片", "二次元", "摄影", "人像", "写真", "角色扮演",
    "场照", "漫展", "返图",
}


def _content_keywords(post: Post) -> list[str]:
    tags = [tag.name.strip() for tag in post.tags if tag.name.strip()]
    title = post.title.casefold()
    ip_name = next((
        tag for tag in tags
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9:_-]{1,20}", tag) and tag.casefold() in title
    ), None)
    character = next((
        tag for tag in tags
        if tag.casefold() in title
        and tag.casefold() not in GENERIC_TAGS
        and not re.fullmatch(r"[A-Za-z0-9:_-]+", tag)
        and not re.search(r"(?:cosplay|cos|正片)$", tag, re.IGNORECASE)
    ), None)
    if not character:
        for tag in tags:
            match = re.match(r"^(.+?)(?:cosplay|cos|正片)$", tag, re.IGNORECASE)
            candidate = match.group(1).strip() if match else ""
            if candidate and candidate.casefold() not in GENERIC_TAGS and candidate.casefold() in title:
                character = candidate
                break
    keywords = [value for value in (ip_name, character, "cos") if value]
    if len(keywords) == 1:
        keywords = [
            tag for tag in tags
            if tag.casefold() not in GENERIC_TAGS
        ][:3] + ["cos"]
    return list(dict.fromkeys(keywords))


def build_generation_prompt(
    post: Post, platform: str, image_count: int, custom_prompt: str | None = None
) -> str:
    if custom_prompt and custom_prompt.strip():
        return custom_prompt.strip()
    keywords = _content_keywords(post)
    keyword_text = " ".join(keywords) or post.title or "cos作品"
    return f"生成 {keyword_text} {PLATFORM_NAMES.get(platform, platform)} 标题和文案，并生成5个相关标签追加在正文末尾。"


def _image_data_url(asset: MediaAsset) -> str:
    path = settings.upload_dir / asset.storage_name
    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            image.save(buffer, "JPEG", quality=82, optimize=True)
    except (OSError, UnidentifiedImageError) as exc:
        raise HTTPException(status_code=422, detail=f"无法读取图片 {asset.original_name}") from exc
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def build_messages(post: Post, platform: str, assets: list[MediaAsset], prompt: str) -> list[dict]:
    content: list[dict] = [{"type": "text", "text": prompt}]
    for asset in assets[:4]:
        content.append({
            "type": "image_url",
            "image_url": {"url": _image_data_url(asset), "detail": "low"},
        })
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def _parse_copy(
    content: object,
    fallback_tags: list[str] | None = None,
    platform: str | None = None,
) -> dict:
    if isinstance(content, list):
        content = "".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        )
    if not isinstance(content, str):
        raise HTTPException(status_code=502, detail="豆包返回了无法识别的内容")
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="豆包没有返回有效的 JSON 文案") from exc
    title = str(payload.get("title") or "").strip()
    body = str(payload.get("body") or payload.get("caption") or "").strip()
    if not title and not body:
        raise HTTPException(status_code=502, detail="豆包返回的标题和正文均为空")
    raw_tags = payload.get("tags") or payload.get("hashtags") or []
    if isinstance(raw_tags, str):
        raw_tags = re.split(r"[,，\s]+", raw_tags)
    if not isinstance(raw_tags, list):
        raw_tags = []
    candidates = [*raw_tags, *re.findall(r"#([^#\s]+)", body), *(fallback_tags or [])]
    candidates.extend([
        PLATFORM_NAMES.get(platform or "", ""), "原创内容", "摄影分享", "内容创作", "生活记录", "灵感分享",
    ])
    tags: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        tag = re.sub(r"\s+", "", str(value).strip().lstrip("#"))[:30]
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            tags.append(tag)
        if len(tags) == 5:
            break
    tag_line = " ".join(f"#{tag}" for tag in tags)
    body_without_trailing_tags = re.sub(r"(?:\s*#[^#\s]+){1,}\s*$", "", body).rstrip()
    available = max(0, 100_000 - len(tag_line) - (2 if body_without_trailing_tags else 0))
    body_without_trailing_tags = body_without_trailing_tags[:available].rstrip()
    body_with_tags = f"{body_without_trailing_tags}\n\n{tag_line}" if body_without_trailing_tags else tag_line
    return {"title": title[:300], "body": body_with_tags, "tags": tags}


async def generate_copy(
    post: Post,
    platform: str,
    assets: list[MediaAsset],
    custom_prompt: str | None = None,
) -> dict:
    llm = get_private_settings()
    if not llm["api_key"]:
        raise HTTPException(status_code=422, detail="请先在 AI 配置中填写豆包 API Key")
    prompt = build_generation_prompt(post, platform, len(assets[:4]), custom_prompt)
    request_body = {
        "model": llm["model"],
        "messages": build_messages(post, platform, assets, prompt),
        "temperature": 0.9,
        "max_tokens": 1000,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
            response = await client.post(
                f"{llm['base_url'].rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {llm['api_key']}"},
                json=request_body,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="豆包响应超时，请稍后再次生成") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="无法连接豆包 API") from exc
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="豆包 API Key 无效或已失效")
    request_id = response.headers.get("x-request-id")
    try:
        error_payload = response.json().get("error", {}) if response.status_code >= 400 else {}
    except (ValueError, AttributeError):
        error_payload = {}
    error_code = str(error_payload.get("code") or "")
    error_message = str(error_payload.get("message") or "")[:300]
    request_hint = f"（Request ID: {request_id}）" if request_id else ""
    if response.status_code == 404:
        raise HTTPException(
            status_code=422,
            detail=f"豆包模型或推理接入点不存在：{llm['model']}。请在 AI 配置中填写 /models 返回的模型 ID 或 ep-... 接入点 ID。{request_hint}",
        )
    if response.status_code == 429:
        raise HTTPException(status_code=429, detail="豆包请求过于频繁或额度不足")
    if response.status_code >= 400:
        description = " · ".join(value for value in (error_code, error_message) if value)
        raise HTTPException(
            status_code=502,
            detail=f"豆包 API 返回错误 {response.status_code}{f'：{description}' if description else ''}{request_hint}",
        )
    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="豆包 API 返回结构异常") from exc
    return {
        **_parse_copy(
            content,
            fallback_tags=[tag.name for tag in post.tags],
            platform=platform,
        ),
        "prompt": prompt,
        "model": llm["model"],
    }
