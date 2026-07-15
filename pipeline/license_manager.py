"""Commercial license verification and feature gates.

The client is not trusted: it only verifies a license signed by the license
server. Private signing keys must never be bundled with the installer.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import platform
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows platforms
    winreg = None

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config, license_clock

FREE_FEATURES = {"basic"}
COMMERCIAL_FEATURES = {
    "export",
    "batch",
    "live_monitor",
    "ai_replay",
    "short_video_ai",
    "lead_radar",
}
ALL_FEATURES = FREE_FEATURES | COMMERCIAL_FEATURES
DEFAULT_POLICY = {
    "max_active_rooms": 10,
    "export_watermark": True,
    "force_upgrade_below": "",
    "disabled_features": [],
}
_PROTECTED_LICENSE_STORAGE = "livewatch-license-cache-v2"
_PROTECTED_LICENSE_PURPOSE = b"LiveWatch local license cache v2"


@dataclass(frozen=True)
class LicenseStatus:
    ok: bool
    mode: str
    reason: str
    features: set[str]
    payload: dict[str, Any]
    expires_at: int = 0
    grace_until: int = 0


class LicenseFeatureError(PermissionError):
    """Raised when a commercial capability is used without a valid license."""


def _b64url_decode(value: str) -> bytes:
    value = value.strip()
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _machine_guid_windows() -> str:
    if winreg is None:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value)
    except OSError:
        return ""


def device_fingerprint_parts() -> list[str]:
    """Return local-only fingerprint fields. Raw values are never uploaded."""
    parts = [
        f"os={platform.system()}",
        f"node={platform.node()}",
        f"machine={platform.machine()}",
        f"processor={platform.processor()}",
    ]
    if platform.system().lower() == "windows":
        guid = _machine_guid_windows()
        if guid:
            parts.append(f"machine_guid={guid}")
    else:
        for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                raw = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if raw:
                parts.append(f"machine_id={raw}")
                break
    mac = uuid.getnode()
    if mac:
        parts.append(f"mac={mac:012x}")
    return [p.strip().lower() for p in parts if p and not p.endswith("=")]


def current_device_hash(*, salt: str | None = None, parts: list[str] | None = None) -> str:
    normalized = "\n".join(sorted(parts or device_fingerprint_parts()))
    product_salt = salt if salt is not None else config.LICENSE_PRODUCT_SALT
    return hashlib.sha256(f"{product_salt}\n{normalized}".encode("utf-8")).hexdigest()


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob_from_bytes(data: bytes) -> _DATA_BLOB:
    buffer = ctypes.create_string_buffer(data)
    blob = _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob._buffer = buffer  # type: ignore[attr-defined]
    return blob


def _bytes_from_blob(blob: _DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def _dpapi_crypt(data: bytes, *, protect: bool) -> bytes | None:
    if platform.system().lower() != "windows":
        return None
    try:
        crypt32 = ctypes.windll.crypt32
    except AttributeError:  # pragma: no cover - non-Windows safety
        return None
    in_blob = _blob_from_bytes(data)
    entropy_blob = _blob_from_bytes(_PROTECTED_LICENSE_PURPOSE)
    out_blob = _DATA_BLOB()
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "LiveWatch license cache",
            ctypes.byref(entropy_blob),
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
    if not ok:
        return None
    return _bytes_from_blob(out_blob)


def _local_aes_key() -> bytes:
    return hashlib.sha256(
        (
            "livewatch-license-cache-v2\n"
            f"{config.LICENSE_PRODUCT_CODE}\n"
            f"{config.LICENSE_PRODUCT_SALT}\n"
            f"{current_device_hash()}"
        ).encode("utf-8")
    ).digest()


def _protect_license_bytes(raw: bytes) -> dict[str, str]:
    dpapi = _dpapi_crypt(raw, protect=True)
    if dpapi is not None:
        return {"scheme": "win32-dpapi", "payload": _b64url_encode(dpapi)}
    nonce = os.urandom(12)
    encrypted = AESGCM(_local_aes_key()).encrypt(nonce, raw, _PROTECTED_LICENSE_PURPOSE)
    return {"scheme": "local-aesgcm", "nonce": _b64url_encode(nonce), "payload": _b64url_encode(encrypted)}


def _unprotect_license_bytes(container: dict[str, Any]) -> bytes | None:
    scheme = str(container.get("scheme") or "")
    try:
        payload = _b64url_decode(str(container.get("payload") or ""))
        if scheme == "win32-dpapi":
            return _dpapi_crypt(payload, protect=False)
        if scheme == "local-aesgcm":
            nonce = _b64url_decode(str(container.get("nonce") or ""))
            return AESGCM(_local_aes_key()).decrypt(nonce, payload, _PROTECTED_LICENSE_PURPOSE)
    except Exception:
        return None
    return None


def _is_protected_license_container(data: dict[str, Any]) -> bool:
    return data.get("storage") == _PROTECTED_LICENSE_STORAGE and data.get("protected") is True


def _wrap_license_doc(doc: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    protected = _protect_license_bytes(raw)
    return {
        "storage": _PROTECTED_LICENSE_STORAGE,
        "protected": True,
        "version": 2,
        **protected,
    }


def _unwrap_license_doc(data: dict[str, Any]) -> dict[str, Any] | None:
    if not _is_protected_license_container(data):
        return data
    raw = _unprotect_license_bytes(data)
    if raw is None:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _load_license(path: Path | None = None) -> dict[str, Any] | None:
    target = path or config.LICENSE_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    doc = _unwrap_license_doc(data)
    if doc and not _is_protected_license_container(data):
        try:
            save_license_doc(doc, path=target)
        except OSError:
            pass
    return doc


def _public_key_from_config(public_key: str | None = None) -> Ed25519PublicKey:
    raw = (public_key if public_key is not None else config.LICENSE_PUBLIC_KEY).strip()
    if not raw:
        raise ValueError("未配置授权公钥")
    if "-----BEGIN" in raw:
        raise ValueError("请配置 base64/raw Ed25519 公钥，不要在客户端放私钥或 PEM 私钥")
    return Ed25519PublicKey.from_public_bytes(_b64url_decode(raw))


def _version_parts(value: str) -> list[int]:
    parts: list[int] = []
    for chunk in str(value or "").strip().split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if digits:
            parts.append(int(digits))
    return parts or [0]


def _version_lt(left: str, right: str) -> bool:
    if not str(right or "").strip():
        return False
    a = _version_parts(left)
    b = _version_parts(right)
    length = max(len(a), len(b))
    a.extend([0] * (length - len(a)))
    b.extend([0] * (length - len(b)))
    return a < b


def normalize_policy(policy: Any) -> dict[str, Any]:
    raw = policy if isinstance(policy, dict) else {}
    result = dict(DEFAULT_POLICY)
    try:
        result["max_active_rooms"] = max(
            1,
            min(int(raw.get("max_active_rooms", result["max_active_rooms"])), 50),
        )
    except (TypeError, ValueError):
        result["max_active_rooms"] = DEFAULT_POLICY["max_active_rooms"]
    result["export_watermark"] = bool(raw.get("export_watermark", result["export_watermark"]))
    result["force_upgrade_below"] = str(raw.get("force_upgrade_below", "") or "").strip()[:64]
    disabled_raw = raw.get("disabled_features", [])
    if not isinstance(disabled_raw, list):
        disabled_raw = []
    result["disabled_features"] = sorted(
        {
            str(feature).strip()
            for feature in disabled_raw
            if str(feature).strip() in COMMERCIAL_FEATURES
        }
    )
    return result


def _effective_features_for_policy(features: set[str], policy: dict[str, Any]) -> set[str]:
    disabled = set(policy.get("disabled_features") or [])
    return (set(features) - disabled) | FREE_FEATURES


def verify_license(
    license_doc: dict[str, Any],
    *,
    public_key: str | None = None,
    now: int | None = None,
    expected_device_hash: str | None = None,
    product_code: str | None = None,
) -> LicenseStatus:
    now_ts = int(now if now is not None else time.time())
    product = product_code or config.LICENSE_PRODUCT_CODE
    try:
        payload_b64 = str(license_doc["payload"])
        signature_b64 = str(license_doc["signature"])
        alg = str(license_doc.get("alg") or "")
    except KeyError:
        return LicenseStatus(False, "invalid", "授权文件缺少字段", FREE_FEATURES, {})
    if alg and alg != "Ed25519":
        return LicenseStatus(False, "invalid", "授权签名算法不受支持", FREE_FEATURES, {})

    try:
        payload_bytes = _b64url_decode(payload_b64)
        signature = _b64url_decode(signature_b64)
        _public_key_from_config(public_key).verify(signature, payload_bytes)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (InvalidSignature, ValueError, json.JSONDecodeError, TypeError) as exc:
        return LicenseStatus(False, "invalid", f"授权验签失败：{exc}", FREE_FEATURES, {})

    if str(payload.get("product_code") or "") != product:
        return LicenseStatus(False, "invalid", "授权产品不匹配", FREE_FEATURES, payload)
    expected_hash = expected_device_hash or current_device_hash()
    if str(payload.get("device_hash") or "") != expected_hash:
        return LicenseStatus(False, "invalid", "授权不属于当前设备", FREE_FEATURES, payload)

    expires_at = int(payload.get("expires_at") or 0)
    grace_until = int(payload.get("grace_until") or expires_at)
    features = set(str(x) for x in (payload.get("features") or [])) | FREE_FEATURES
    features &= ALL_FEATURES
    if expires_at and now_ts <= expires_at:
        return LicenseStatus(True, "licensed", "授权有效", features, payload, expires_at, grace_until)
    if grace_until and now_ts <= grace_until:
        return LicenseStatus(True, "grace", "授权已到期，宽限期内可继续使用，请尽快联网刷新", features, payload, expires_at, grace_until)
    return LicenseStatus(False, "expired", "授权已过期", FREE_FEATURES, payload, expires_at, grace_until)


def current_status(*, now: int | None = None) -> LicenseStatus:
    if not config.LICENSE_ENFORCE:
        return LicenseStatus(True, "development", "开发模式未强制授权", ALL_FEATURES, {})
    doc = _load_license()
    if not doc:
        return LicenseStatus(False, "missing", "未激活，请输入卡密激活", FREE_FEATURES, {})
    status = verify_license(doc, now=now)
    if not status.ok:
        return status
    policy = normalize_policy(status.payload.get("policy"))
    if _version_lt(config.LICENSE_APP_VERSION, str(policy.get("force_upgrade_below") or "")):
        return LicenseStatus(
            False,
            "upgrade_required",
            f"当前版本 {config.LICENSE_APP_VERSION} 已停用，请升级到 {policy['force_upgrade_below']} 或更高版本",
            FREE_FEATURES,
            status.payload,
            status.expires_at,
            status.grace_until,
        )
    clock = license_clock.check_and_record(now=now)
    if not clock.ok:
        return LicenseStatus(
            False,
            "clock_error",
            clock.reason,
            FREE_FEATURES,
            status.payload,
            status.expires_at,
            status.grace_until,
        )
    effective_features = _effective_features_for_policy(status.features, policy)
    return LicenseStatus(
        status.ok,
        status.mode,
        status.reason,
        effective_features,
        status.payload,
        status.expires_at,
        status.grace_until,
    )


def has_feature(feature: str) -> bool:
    return feature in current_status().features


def require_feature(feature: str) -> None:
    """Require one feature without coupling license rules to the web framework."""
    status = current_status()
    if feature in status.features:
        return
    if status.ok and feature in set(normalize_policy(status.payload.get("policy")).get("disabled_features") or []):
        raise LicenseFeatureError("当前功能已被管理员停用")
    raise LicenseFeatureError(status.reason)


def current_policy() -> dict[str, Any]:
    status = current_status()
    return normalize_policy(status.payload.get("policy"))


def policy_int(name: str, default: int, *, minimum: int = 1, maximum: int = 10_000) -> int:
    value = current_policy().get(name, default)
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def public_status() -> dict[str, Any]:
    status = current_status()
    return {
        "ok": status.ok,
        "mode": status.mode,
        "reason": status.reason,
        "features": sorted(status.features),
        "policy": normalize_policy(status.payload.get("policy")) if status.ok else dict(DEFAULT_POLICY),
        "enforced": config.LICENSE_ENFORCE,
        "expires_at": status.expires_at,
        "grace_until": status.grace_until,
    }


def save_license_doc(doc: dict[str, Any], path: Path | None = None) -> None:
    target = path or config.LICENSE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(_wrap_license_doc(doc), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def clear_license_doc(path: Path | None = None) -> None:
    """Remove the local cached entitlement after a server-side denial.

    Offline grace is only for network failures. If the licensing server says a
    card is expired, frozen, or disabled, the cached signed package must stop
    granting features immediately on this machine.
    """
    target = path or config.LICENSE_PATH
    try:
        target.unlink()
    except FileNotFoundError:
        return


def install_license_doc(doc: dict[str, Any], path: Path | None = None) -> LicenseStatus:
    """Verify and persist a signed license package.

    Invalid licenses are rejected before writing, so a bad paste cannot replace
    a previously working local license file.
    """
    status = verify_license(doc)
    if not status.ok:
        return status
    save_license_doc(doc, path=path)
    return status
