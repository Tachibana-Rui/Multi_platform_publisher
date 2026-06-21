from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import threading

from fastapi import HTTPException

from .config import settings


DEFAULT_MODEL = "doubao-seed-2-0-lite-260428"
DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
MODEL_ALIASES = {
    "Doubao-Seed-2.0-lite": DEFAULT_MODEL,
    "doubao-seed-2-0-lite": DEFAULT_MODEL,
}
SETTINGS_PATH = settings.data_dir / "llm_settings.json"
_lock = threading.RLock()


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _protect(secret: str) -> str:
    raw = secret.encode("utf-8")
    if os.name != "nt":
        return "plain:" + base64.b64encode(raw).decode("ascii")
    buffer = ctypes.create_string_buffer(raw)
    input_blob = DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(input_blob), "Content Hub Doubao API Key", None, None, None,
        0x01, ctypes.byref(output_blob),
    ):
        raise HTTPException(status_code=500, detail="无法使用 Windows 加密 API Key")
    try:
        protected = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return "dpapi:" + base64.b64encode(protected).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def _unprotect(value: str) -> str:
    if value.startswith("plain:"):
        return base64.b64decode(value[6:]).decode("utf-8")
    if not value.startswith("dpapi:") or os.name != "nt":
        return ""
    protected = base64.b64decode(value[6:])
    buffer = ctypes.create_string_buffer(protected)
    input_blob = DATA_BLOB(len(protected), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(input_blob), None, None, None, None, 0x01, ctypes.byref(output_blob)
    ):
        return ""
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def _read() -> dict:
    if not SETTINGS_PATH.exists():
        return {"model": DEFAULT_MODEL, "base_url": DEFAULT_BASE_URL}
    try:
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    configured_model = payload.get("model") or DEFAULT_MODEL
    return {
        "model": MODEL_ALIASES.get(configured_model, configured_model),
        "base_url": DEFAULT_BASE_URL,
        "api_key_protected": payload.get("api_key_protected"),
    }


def get_public_settings() -> dict:
    with _lock:
        payload = _read()
        key = _unprotect(payload.get("api_key_protected") or "")
        return {
            "provider": "doubao",
            "model": payload["model"],
            "base_url": DEFAULT_BASE_URL,
            "has_api_key": bool(key),
            "api_key_hint": f"••••{key[-4:]}" if key else None,
        }


def get_private_settings() -> dict:
    with _lock:
        payload = _read()
        return {
            "model": payload["model"],
            "base_url": DEFAULT_BASE_URL,
            "api_key": _unprotect(payload.get("api_key_protected") or ""),
        }


def update_settings(api_key=None, model=None, clear_api_key: bool = False) -> dict:
    with _lock:
        current = _read()
        if clear_api_key:
            current.pop("api_key_protected", None)
        elif api_key is not None and api_key.strip():
            current["api_key_protected"] = _protect(api_key.strip())
        if model is not None and model.strip():
            current["model"] = model.strip()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = SETTINGS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(SETTINGS_PATH)
    return get_public_settings()
