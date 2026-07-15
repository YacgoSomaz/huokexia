"""HTTP API for the card-key licensing service.

Run in production with:
    uvicorn --factory licensing_server.app:create_app_from_env --host 127.0.0.1 --port 9077
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from .service import LicenseError, LicenseService, LicenseSettings
from .admin_console import ADMIN_HTML
from .rate_limit import IpRateLimiter, RateLimitPolicy, client_ip_from_request


class ActivationRequest(BaseModel):
    card_key: str = Field(min_length=4, max_length=128)
    device_hash: str = Field(min_length=1, max_length=128)
    app_version: str = Field(default="", max_length=128)
    product_code: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9_.-]*$")


class RefreshRequest(BaseModel):
    activation_id: str = Field(min_length=1, max_length=128)
    refresh_token: str = Field(min_length=16, max_length=256)
    device_hash: str = Field(min_length=1, max_length=128)
    app_version: str = Field(default="", max_length=128)
    product_code: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9_.-]*$")


class CreateCardRequest(BaseModel):
    features: list[str] = Field(min_length=1, max_length=20)
    product_code: str = Field(default="live_replay_xia", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    max_devices: int = Field(default=1, ge=1, le=20)
    expires_at: int = Field(default=0, ge=0)
    policy: dict[str, Any] = Field(default_factory=dict)
    max_active_rooms: int | None = Field(default=None, ge=1, le=50)
    export_watermark: bool | None = None
    force_upgrade_below: str = Field(default="", max_length=64)
    note: str = Field(default="", max_length=500)


class ReasonRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class AdminLoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=500)
    device_hash: str = Field(min_length=8, max_length=256)


class UpdateSettings(BaseModel):
    product_code: str = Field(default="live_replay_xia", max_length=64)
    latest_version: str = Field(default="", max_length=64)
    min_version: str = Field(default="", max_length=64)
    installer_url: str = Field(default="", max_length=500)
    sha256: str = Field(default="", max_length=128)
    notes: str = Field(default="", max_length=2000)
    mandatory: bool = False


def _version_parts(value: str) -> list[int]:
    parts = [int(x) for x in str(value or "").split(".") if x.isdigit()]
    return parts or [0]


def _version_lt(left: str, right: str) -> bool:
    if not right:
        return False
    a = _version_parts(left)
    b = _version_parts(right)
    width = max(len(a), len(b))
    a.extend([0] * (width - len(a)))
    b.extend([0] * (width - len(b)))
    return a < b


def _public_error(exc: LicenseError) -> HTTPException:
    text = str(exc)
    status = 403 if any(word in text for word in ("冻结", "停用", "到期", "不属于", "不匹配")) else 400
    return HTTPException(status_code=status, detail=text)


def create_app(
    service: LicenseService,
    *,
    admin_token: str,
    update_settings: UpdateSettings | None = None,
    rate_limiter: IpRateLimiter | None = None,
    trusted_proxies: set[str] | None = None,
) -> FastAPI:
    if len(admin_token.strip()) < 16:
        raise ValueError("LICENSE_ADMIN_TOKEN 必须至少 16 个字符")
    app = FastAPI(title="直播复盘侠授权服务", version="1.0.0", docs_url=None, redoc_url=None)
    limiter = rate_limiter or IpRateLimiter()
    proxy_ips = trusted_proxies or {"127.0.0.1", "::1"}
    updates = update_settings or UpdateSettings()
    admin_cookie_name = "livewatch_admin_trust"
    admin_session_ttl = 86400

    @app.middleware("http")
    async def rate_limit_public_license_requests(request: Request, call_next):
        remote_ip = request.client.host if request.client else ""
        client_ip = client_ip_from_request(
            remote_ip,
            request.headers.get("x-forwarded-for", ""),
            proxy_ips,
        )
        allowed, retry_after = limiter.allow(path=request.url.path, client_ip=client_ip)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后再试"},
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)

    def _admin_client_ip(request: Request) -> str:
        remote_ip = request.client.host if request.client else ""
        return client_ip_from_request(
            remote_ip,
            request.headers.get("x-forwarded-for", ""),
            proxy_ips,
        )

    def _admin_session_secret() -> bytes:
        return hashlib.sha256(("admin-session:" + admin_token).encode("utf-8")).digest()

    def _sign_admin_session(payload: dict[str, object]) -> str:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = hmac.new(_admin_session_secret(), body, hashlib.sha256).digest()
        return (
            base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
            + "."
            + base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
        )

    def _verify_admin_session(token: str, *, client_ip: str, device_hash: str) -> bool:
        try:
            body_b64, sig_b64 = token.split(".", 1)
            body = base64.urlsafe_b64decode((body_b64 + "=" * (-len(body_b64) % 4)).encode("ascii"))
            sig = base64.urlsafe_b64decode((sig_b64 + "=" * (-len(sig_b64) % 4)).encode("ascii"))
            expected = hmac.new(_admin_session_secret(), body, hashlib.sha256).digest()
            if not hmac.compare_digest(sig, expected):
                return False
            payload = json.loads(body.decode("utf-8"))
            if int(payload.get("exp", 0)) < int(time.time()):
                return False
            if payload.get("ip") != client_ip:
                return False
            if payload.get("device") != device_hash:
                return False
            return True
        except Exception:
            return False

    def require_admin(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_admin_device: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = f"Bearer {admin_token}"
        if authorization is not None and hmac.compare_digest(authorization, expected):
            return
        session = request.cookies.get(admin_cookie_name, "")
        device = str(x_admin_device or "").strip()
        if session and device and _verify_admin_session(session, client_ip=_admin_client_ip(request), device_hash=device):
            return
        raise HTTPException(status_code=401, detail="管理员授权无效")

    @app.get("/v1/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/v1/update")
    def update_manifest(
        product_code: str = Query(default="live_replay_xia", max_length=64, pattern=r"^[A-Za-z0-9_.-]+$"),
        current_version: str = Query(default="", max_length=64),
    ) -> dict[str, object]:
        configured_product = (updates.product_code or service.settings.product_code).strip()
        if product_code != configured_product:
            raise HTTPException(status_code=404, detail="暂无该产品更新")
        if not updates.latest_version or not updates.installer_url or not updates.sha256:
            return {
                "ok": True,
                "has_update": False,
                "latest_version": "",
                "current_version": current_version,
            }
        has_update = _version_lt(current_version, updates.latest_version)
        mandatory = updates.mandatory or _version_lt(current_version, updates.min_version)
        return {
            "ok": True,
            "has_update": has_update,
            "product_code": configured_product,
            "current_version": current_version,
            "latest_version": updates.latest_version,
            "min_version": updates.min_version,
            "mandatory": mandatory,
            "installer_url": updates.installer_url,
            "sha256": updates.sha256.lower(),
            "notes": updates.notes,
        }

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/admin", status_code=302)

    @app.post("/v1/activate")
    def activate(payload: ActivationRequest) -> dict[str, object]:
        try:
            return service.activate(
                card_key=payload.card_key,
                device_hash=payload.device_hash,
                product_code=payload.product_code,
                app_version=payload.app_version,
            )
        except LicenseError as exc:
            raise _public_error(exc) from exc

    @app.post("/v1/refresh")
    def refresh(payload: RefreshRequest) -> dict[str, object]:
        try:
            return service.refresh(
                activation_id=payload.activation_id,
                refresh_token=payload.refresh_token,
                device_hash=payload.device_hash,
                product_code=payload.product_code,
                app_version=payload.app_version,
            )
        except LicenseError as exc:
            raise _public_error(exc) from exc

    @app.get("/wanshan-media/updates/{file_name}", include_in_schema=False)
    def wanshan_update_file(file_name: str) -> FileResponse:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", file_name):
            raise HTTPException(status_code=404, detail="Not Found")
        root = Path(os.environ.get("WANSHAN_UPDATE_FILE_ROOT", "/var/www/wanshan-media/updates")).resolve()
        target = (root / file_name).resolve()
        if target.parent != root or not target.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(target)

    @app.get("/admin", response_class=HTMLResponse)
    def admin_console() -> HTMLResponse:
        return HTMLResponse(
            ADMIN_HTML,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
            },
        )

    @app.post("/admin/session")
    def admin_login(payload: AdminLoginRequest, request: Request, response: Response) -> dict[str, object]:
        if not hmac.compare_digest(payload.token, admin_token):
            raise HTTPException(status_code=401, detail="管理员授权无效")
        now = int(time.time())
        cookie = _sign_admin_session(
            {
                "ip": _admin_client_ip(request),
                "device": payload.device_hash.strip(),
                "iat": now,
                "exp": now + admin_session_ttl,
            }
        )
        secure_cookie = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"
        response.set_cookie(
            admin_cookie_name,
            cookie,
            max_age=admin_session_ttl,
            httponly=True,
            secure=secure_cookie,
            samesite="strict",
            path="/admin",
        )
        return {"ok": True, "expires_in": admin_session_ttl, "expires_at": now + admin_session_ttl}

    @app.get("/admin/session", dependencies=[Depends(require_admin)])
    def admin_session() -> dict[str, object]:
        return {"ok": True, "expires_in": admin_session_ttl, "expires_at": int(time.time()) + admin_session_ttl}

    @app.post("/admin/card-keys", dependencies=[Depends(require_admin)])
    def create_card(payload: CreateCardRequest) -> dict[str, str]:
        policy = dict(payload.policy or {})
        if payload.product_code == "live_replay_xia":
            if payload.max_active_rooms is not None:
                policy["max_active_rooms"] = payload.max_active_rooms
            if payload.export_watermark is not None:
                policy["export_watermark"] = payload.export_watermark
            if payload.force_upgrade_below:
                policy["force_upgrade_below"] = payload.force_upgrade_below
        try:
            card_key = service.create_card_key(
                features=set(payload.features),
                product_code=payload.product_code,
                max_devices=payload.max_devices,
                expires_at=payload.expires_at,
                policy=policy,
                note=payload.note,
            )
        except LicenseError as exc:
            raise _public_error(exc) from exc
        return {"card_key": card_key}

    @app.get("/admin/cards", dependencies=[Depends(require_admin)])
    def list_cards(limit: int = 200) -> dict[str, object]:
        return {"cards": service.list_cards(limit=limit)}

    @app.delete("/admin/cards/{card_id}", dependencies=[Depends(require_admin)])
    def delete_card(card_id: str, payload: ReasonRequest | None = None) -> dict[str, bool]:
        try:
            service.delete_card_key(card_id, reason=(payload.reason if payload else ""))
        except LicenseError as exc:
            raise _public_error(exc) from exc
        return {"ok": True}

    @app.get("/admin/public-key", dependencies=[Depends(require_admin)])
    def public_key() -> dict[str, str]:
        return {"public_key": service.public_key_b64url()}

    @app.get("/admin/activations", dependencies=[Depends(require_admin)])
    def list_activations(limit: int = 200) -> dict[str, object]:
        return {"activations": service.list_activations(limit=limit)}

    @app.post("/admin/activations/{activation_id}/freeze", dependencies=[Depends(require_admin)])
    def freeze(activation_id: str, payload: ReasonRequest) -> dict[str, bool]:
        try:
            service.freeze_activation(activation_id, reason=payload.reason)
        except LicenseError as exc:
            raise _public_error(exc) from exc
        return {"ok": True}

    @app.post("/admin/activations/{activation_id}/unbind", dependencies=[Depends(require_admin)])
    def unbind(activation_id: str, payload: ReasonRequest) -> dict[str, bool]:
        try:
            service.unbind_activation(activation_id, reason=payload.reason)
        except LicenseError as exc:
            raise _public_error(exc) from exc
        return {"ok": True}

    return app


def create_app_from_env() -> FastAPI:
    signing_private_key = os.environ.get("LICENSE_SIGNING_PRIVATE_KEY", "").strip()
    token_hash_secret = os.environ.get("LICENSE_TOKEN_HASH_SECRET", "").strip()
    admin_token = os.environ.get("LICENSE_ADMIN_TOKEN", "").strip()
    settings = LicenseSettings(
        db_path=Path(os.environ.get("LICENSE_DB_PATH", "./license_data/licenses.db")).expanduser(),
        signing_private_key=signing_private_key,
        token_hash_secret=token_hash_secret,
        product_code=os.environ.get("LICENSE_PRODUCT_CODE", "live_replay_xia").strip() or "live_replay_xia",
        license_days=int(os.environ.get("LICENSE_DOCUMENT_DAYS", "3")),
        grace_days=int(os.environ.get("LICENSE_GRACE_DAYS", "1")),
    )
    policy = RateLimitPolicy(
        window_seconds=int(os.environ.get("LICENSE_RATE_LIMIT_WINDOW_SEC", "60")),
        activate_attempts=int(os.environ.get("LICENSE_RATE_LIMIT_ACTIVATE", "8")),
        refresh_attempts=int(os.environ.get("LICENSE_RATE_LIMIT_REFRESH", "60")),
    )
    trusted_proxies = {
        value.strip()
        for value in os.environ.get("LICENSE_TRUSTED_PROXY_IPS", "127.0.0.1,::1").split(",")
        if value.strip()
    }
    return create_app(
        LicenseService(settings),
        admin_token=admin_token,
        update_settings=UpdateSettings(
            product_code=os.environ.get("LICENSE_UPDATE_PRODUCT_CODE", settings.product_code).strip() or settings.product_code,
            latest_version=os.environ.get("LICENSE_UPDATE_LATEST_VERSION", "").strip(),
            min_version=os.environ.get("LICENSE_UPDATE_MIN_VERSION", "").strip(),
            installer_url=os.environ.get("LICENSE_UPDATE_INSTALLER_URL", "").strip(),
            sha256=os.environ.get("LICENSE_UPDATE_INSTALLER_SHA256", "").strip(),
            notes=os.environ.get("LICENSE_UPDATE_NOTES", "").strip(),
            mandatory=os.environ.get("LICENSE_UPDATE_MANDATORY", "").strip().lower() in {"1", "true", "yes", "on"},
        ),
        rate_limiter=IpRateLimiter(policy),
        trusted_proxies=trusted_proxies,
    )
