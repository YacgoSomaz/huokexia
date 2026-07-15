"""Online activation and refresh client for the local desktop application."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from . import config, license_manager


class LicenseClientError(RuntimeError):
    """A user-safe error for activation and refresh actions."""


class LicenseServerDenial(LicenseClientError):
    """Raised when the server explicitly rejects an existing entitlement."""


Post = Callable[..., Any]


def _server_url(value: str | None = None) -> str:
    url = (value if value is not None else config.LICENSE_SERVER_URL).strip().rstrip("/")
    if not url.startswith("https://"):
        raise LicenseClientError("未配置 HTTPS 授权服务器地址")
    return url


def _request_signature_headers(url: str, payload: dict[str, str], *, signing_secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = str(uuid.uuid4())
    device_hash = str(payload.get("device_hash") or license_manager.current_device_hash())
    app_version = str(payload.get("app_version") or config.LICENSE_APP_VERSION)
    canonical_body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    parsed = urlsplit(url)
    signing_base = "\n".join(
        [
            "POST",
            parsed.path or "/",
            timestamp,
            nonce,
            device_hash,
            app_version,
            hashlib.sha256(canonical_body.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        str(signing_secret or "").encode("utf-8"),
        signing_base.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-LiveWatch-Timestamp": timestamp,
        "X-LiveWatch-Nonce": nonce,
        "X-LiveWatch-Device": device_hash,
        "X-LiveWatch-App-Version": app_version,
        "X-LiveWatch-Signature": signature,
    }


def _request_json(post: Post, url: str, payload: dict[str, str], *, signing_secret: str) -> dict[str, Any]:
    headers = _request_signature_headers(url, payload, signing_secret=signing_secret)
    try:
        response = post(url, json=payload, headers=headers, timeout=config.LICENSE_REQUEST_TIMEOUT_SEC)
    except requests.RequestException as exc:
        raise LicenseClientError("无法连接授权服务器，请检查网络后重试") from exc
    try:
        data = response.json()
    except (ValueError, TypeError) as exc:
        raise LicenseClientError("授权服务器返回格式异常") from exc
    if not isinstance(data, dict):
        raise LicenseClientError("授权服务器返回格式异常")
    status_code = int(getattr(response, "status_code", 200))
    if 400 <= status_code < 500:
        detail = str(data.get("detail") or "授权操作失败")
        raise LicenseServerDenial(detail[:200])
    if status_code >= 500:
        detail = str(data.get("detail") or "授权服务器暂时不可用")
        raise LicenseClientError(detail[:200])
    return data


def _persist_server_reply(reply: dict[str, Any], *, server_url: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    license_doc = reply.get("license")
    activation_id = str(reply.get("activation_id") or (previous or {}).get("activation_id") or "")
    refresh_token = str(reply.get("refresh_token") or (previous or {}).get("refresh_token") or "")
    if not isinstance(license_doc, dict) or not activation_id or not refresh_token:
        raise LicenseClientError("授权服务器返回内容不完整")
    package = dict(license_doc)
    package.update(
        {
            "activation_id": activation_id,
            "refresh_token": refresh_token,
            "server_url": server_url,
        }
    )
    status = license_manager.install_license_doc(package)
    if not status.ok:
        raise LicenseClientError(status.reason)
    return license_manager.public_status()


def _load_package(path: Path | None = None) -> dict[str, Any]:
    package = license_manager._load_license(path or config.LICENSE_PATH)
    if not isinstance(package, dict):
        raise LicenseClientError("本机授权文件无效")
    return package


def activate_card_key(card_key: str, *, server_url: str | None = None, post: Post = requests.post) -> dict[str, Any]:
    value = str(card_key or "").strip().upper()
    if len(value) < 4 or len(value) > 128:
        raise LicenseClientError("请输入有效卡密")
    base_url = _server_url(server_url)
    reply = _request_json(
        post,
        f"{base_url}/v1/activate",
        {
            "card_key": value,
            "device_hash": license_manager.current_device_hash(),
            "app_version": config.LICENSE_APP_VERSION,
            # The server rejects a card created for another desktop product.
            "product_code": config.LICENSE_PRODUCT_CODE,
        },
        signing_secret=value,
    )
    return _persist_server_reply(reply, server_url=base_url)


def refresh_license(*, post: Post = requests.post) -> dict[str, Any]:
    package = _load_package()
    base_url = _server_url(str(package.get("server_url") or ""))
    try:
        reply = _request_json(
            post,
            f"{base_url}/v1/refresh",
            {
                "activation_id": str(package.get("activation_id") or ""),
                "refresh_token": str(package.get("refresh_token") or ""),
                "device_hash": license_manager.current_device_hash(),
                "app_version": config.LICENSE_APP_VERSION,
                "product_code": config.LICENSE_PRODUCT_CODE,
            },
            signing_secret=str(package.get("refresh_token") or ""),
        )
    except LicenseServerDenial:
        license_manager.clear_license_doc()
        raise
    return _persist_server_reply(reply, server_url=base_url, previous=package)
