"""Persistent anchor profile cache.

The listening list is user-editable and may be cleared, but historical exports
and performance analysis still need stable names and avatars.  This cache keeps
resolved anchor metadata independently from rooms.json.
"""

from __future__ import annotations

import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any

import requests

from . import config


_SAFE_ID_RE = re.compile(r"[^0-9A-Za-z_.-]+")
_AVATAR_EXTS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def load_profiles() -> dict[str, dict[str, str]]:
    if not config.ANCHOR_PROFILE_CACHE.exists():
        return {}
    try:
        raw = json.loads(config.ANCHOR_PROFILE_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for rid, profile in raw.items():
        if not isinstance(profile, dict):
            continue
        room_id = str(rid or "").strip()
        if not room_id:
            continue
        out[room_id] = _public_profile(room_id, profile)
    return out


def save_profile(room_id: str, metadata: dict[str, Any] | None) -> dict[str, str]:
    room_id = str(room_id or "").strip()
    if not room_id:
        return {}
    metadata = metadata or {}
    config.ensure_dirs()
    raw = _load_raw()
    existing = raw.get(room_id, {}) if isinstance(raw.get(room_id), dict) else {}
    profile = {
        "anchor_name": _clean(metadata.get("anchor_name")) or _clean(existing.get("anchor_name")),
        "avatar_url": _clean(metadata.get("avatar_url")) or _clean(existing.get("avatar_url")),
        "source_url": _clean(metadata.get("source_url")) or _clean(existing.get("source_url")),
        "sec_user_id": _clean(metadata.get("sec_user_id")) or _clean(existing.get("sec_user_id")),
        "updated_ts": int(time.time()),
    }
    local_path = _clean(existing.get("local_avatar_path"))
    source_avatar = _clean(metadata.get("avatar_url")) or (_clean(existing.get("avatar_url")) if not local_path else "")
    if source_avatar:
        downloaded = _download_avatar(room_id, source_avatar)
        if downloaded:
            local_path = str(downloaded)
    if local_path:
        profile["local_avatar_path"] = local_path
    raw[room_id] = profile
    try:
        config.ANCHOR_PROFILE_CACHE.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return _public_profile(room_id, profile)


def avatar_file(room_id: str) -> Path | None:
    profile = _load_raw().get(str(room_id or "").strip(), {})
    if not isinstance(profile, dict):
        return None
    path = Path(_clean(profile.get("local_avatar_path")))
    if path.exists() and path.is_file():
        return path
    return None


def _load_raw() -> dict[str, dict[str, Any]]:
    if not config.ANCHOR_PROFILE_CACHE.exists():
        return {}
    try:
        raw = json.loads(config.ANCHOR_PROFILE_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _public_profile(room_id: str, profile: dict[str, Any]) -> dict[str, str]:
    local_path = Path(_clean(profile.get("local_avatar_path")))
    avatar_url = _clean(profile.get("avatar_url"))
    if local_path.exists() and local_path.is_file():
        try:
            version = int(local_path.stat().st_mtime)
        except OSError:
            version = int(time.time())
        avatar_url = f"/api/avatars/{room_id}?v={version}"
    return {
        "anchor_name": _clean(profile.get("anchor_name")),
        "avatar_url": avatar_url,
        "source_url": _clean(profile.get("source_url")),
        "sec_user_id": _clean(profile.get("sec_user_id")),
    }


def _download_avatar(room_id: str, avatar_url: str) -> Path | None:
    if not avatar_url.startswith(("http://", "https://")):
        return None
    try:
        resp = requests.get(avatar_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except requests.RequestException:
        return None
    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if not content_type.startswith("image/") or len(resp.content or b"") < 64:
        return None
    ext = _AVATAR_EXTS.get(content_type)
    if not ext:
        guessed = mimetypes.guess_extension(content_type)
        ext = guessed if guessed in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"
    safe_id = _SAFE_ID_RE.sub("_", room_id).strip("._") or "avatar"
    path = config.AVATAR_CACHE_DIR / f"{safe_id}{ext}"
    try:
        path.write_bytes(resp.content)
    except OSError:
        return None
    return path


def _clean(value: Any) -> str:
    return str(value or "").strip()
