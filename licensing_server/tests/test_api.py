from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from licensing_server.app import UpdateSettings, create_app
from licensing_server.rate_limit import IpRateLimiter, RateLimitPolicy
from licensing_server.service import LicenseError, LicenseService, LicenseSettings


def _service(tmp_path: Path) -> LicenseService:
    private_key = Ed25519PrivateKey.generate()
    private_b64 = base64.urlsafe_b64encode(private_key.private_bytes_raw()).decode("ascii").rstrip("=")
    return LicenseService(
        LicenseSettings(
            db_path=tmp_path / "licenses.db",
            signing_private_key=private_b64,
            token_hash_secret="test-only-token-secret",
        )
    )


def test_activation_api_and_freeze_protect_refresh(tmp_path: Path) -> None:
    service = _service(tmp_path)
    card_key = service.create_card_key(features={"basic", "export"})
    client = TestClient(create_app(service, admin_token="admin-test-token"))

    root = client.get("/", follow_redirects=False)
    assert root.status_code == 302
    assert root.headers["location"] == "/admin"

    console = client.get("/admin")
    assert console.status_code == 200
    assert "授权管理台" in console.text
    assert 'value="wanshan_media"' in console.text
    assert 'value="wanshan"' not in console.text
    assert 'id="expiresChoice"' in console.text
    assert 'value="minute"' in console.text
    assert "expires_at:selectedExpiresAt()" in console.text

    cards = client.get("/admin/cards", headers={"Authorization": "Bearer admin-test-token"})
    assert cards.status_code == 200
    assert cards.json()["cards"][0]["key_prefix"] == card_key[:8]

    activation = client.post(
        "/v1/activate",
        json={"card_key": card_key, "device_hash": "device-a", "app_version": "1.0.0"},
    )
    assert activation.status_code == 200
    body = activation.json()

    activations = client.get(
        "/admin/activations",
        headers={"Authorization": "Bearer admin-test-token"},
    )
    assert activations.status_code == 200
    assert activations.json()["activations"][0]["id"] == body["activation_id"]
    assert activations.json()["activations"][0]["status"] == "active"

    unauthorized = client.post(f"/admin/activations/{body['activation_id']}/freeze", json={"reason": "refund"})
    assert unauthorized.status_code == 401

    frozen = client.post(
        f"/admin/activations/{body['activation_id']}/freeze",
        headers={"Authorization": "Bearer admin-test-token"},
        json={"reason": "refund"},
    )
    assert frozen.status_code == 200

    refreshed = client.post(
        "/v1/refresh",
        json={
            "activation_id": body["activation_id"],
            "refresh_token": body["refresh_token"],
            "device_hash": "device-a",
            "app_version": "1.0.1",
        },
    )
    assert refreshed.status_code == 403
    assert refreshed.json()["detail"] == "当前设备授权已冻结"


def test_short_lived_card_expires_after_selected_time(tmp_path: Path) -> None:
    service = _service(tmp_path)
    now = 1_800_000_000
    card_key = service.create_card_key(features={"basic"}, expires_at=now + 60, now=now)

    activation = service.activate(card_key=card_key, device_hash="device-a", now=now + 30)
    assert activation["activation_id"]
    import json

    payload_b64 = activation["license"]["payload"]
    raw_payload = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    signed = json.loads(raw_payload.decode("utf-8"))
    assert signed["expires_at"] == now + 60
    assert signed["grace_until"] == now + 60

    with pytest.raises(LicenseError, match="卡密已到期"):
        service.activate(card_key=card_key, device_hash="device-b", now=now + 61)


def test_admin_can_issue_card_with_cloud_policy(tmp_path: Path) -> None:
    service = _service(tmp_path)
    client = TestClient(create_app(service, admin_token="admin-test-token"))

    issued = client.post(
        "/admin/card-keys",
        headers={"Authorization": "Bearer admin-test-token"},
        json={
            "features": ["basic", "live_monitor"],
            "max_devices": 1,
            "max_active_rooms": 4,
            "expires_at": 1_800_000_060,
            "export_watermark": False,
            "force_upgrade_below": "1.2.0",
        },
    )
    assert issued.status_code == 200

    cards = client.get("/admin/cards", headers={"Authorization": "Bearer admin-test-token"}).json()["cards"]
    assert cards[0]["card_key"] == issued.json()["card_key"]
    assert cards[0]["expires_at"] == 1_800_000_060
    assert cards[0]["policy"]["max_active_rooms"] == 4
    assert cards[0]["policy"]["export_watermark"] is False
    assert cards[0]["policy"]["force_upgrade_below"] == "1.2.0"


def test_admin_can_delete_card_and_freeze_existing_devices(tmp_path: Path) -> None:
    service = _service(tmp_path)
    card_key = service.create_card_key(features={"basic", "export"})
    client = TestClient(create_app(service, admin_token="admin-test-token"))
    activation = client.post(
        "/v1/activate",
        json={"card_key": card_key, "device_hash": "device-delete", "app_version": "1.0.0"},
    )
    assert activation.status_code == 200

    cards = client.get("/admin/cards", headers={"Authorization": "Bearer admin-test-token"}).json()["cards"]
    assert len(cards) == 1
    deleted = client.request(
        "DELETE",
        f"/admin/cards/{cards[0]['id']}",
        headers={"Authorization": "Bearer admin-test-token"},
        json={"reason": "test cleanup"},
    )
    assert deleted.status_code == 200
    assert client.get("/admin/cards", headers={"Authorization": "Bearer admin-test-token"}).json()["cards"] == []
    assert client.get("/admin/activations", headers={"Authorization": "Bearer admin-test-token"}).json()["activations"] == []

    refreshed = client.post(
        "/v1/refresh",
        json={
            "activation_id": activation.json()["activation_id"],
            "refresh_token": activation.json()["refresh_token"],
            "device_hash": "device-delete",
            "app_version": "1.0.1",
        },
    )
    assert refreshed.status_code == 403
    assert refreshed.json()["detail"] == "当前设备授权已冻结"


def test_admin_can_issue_wanshan_card_and_activation_preserves_product_code(tmp_path: Path) -> None:
    service = _service(tmp_path)
    client = TestClient(create_app(service, admin_token="admin-test-token"))

    issued = client.post(
        "/admin/card-keys",
        headers={"Authorization": "Bearer admin-test-token"},
        json={"product_code": "wanshan_media", "features": ["basic"]},
    )
    assert issued.status_code == 200
    card_key = issued.json()["card_key"]

    cards = client.get("/admin/cards", headers={"Authorization": "Bearer admin-test-token"}).json()["cards"]
    assert cards[0]["product_code"] == "wanshan_media"
    assert cards[0]["card_key"] == card_key

    activation = client.post(
        "/v1/activate",
        json={"card_key": card_key, "device_hash": "wanshan-device", "product_code": "wanshan_media"},
    )
    assert activation.status_code == 200


def test_activation_rejects_a_card_for_the_wrong_product(tmp_path: Path) -> None:
    service = _service(tmp_path)
    client = TestClient(create_app(service, admin_token="admin-test-token"))
    card_key = service.create_card_key(features={"basic"}, product_code="wanshan_media")

    response = client.post(
        "/v1/activate",
        json={"card_key": card_key, "device_hash": "wrong-product-device", "product_code": "live_replay_xia"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "授权产品不匹配"


def test_admin_public_key_requires_admin_and_returns_build_key(tmp_path: Path) -> None:
    service = _service(tmp_path)
    client = TestClient(create_app(service, admin_token="admin-test-token"))

    unauthorized = client.get("/admin/public-key")
    assert unauthorized.status_code == 401

    response = client.get("/admin/public-key", headers={"Authorization": "Bearer admin-test-token"})
    assert response.status_code == 200
    assert response.json() == {"public_key": service.public_key_b64url()}


def test_update_manifest_returns_public_installer_metadata(tmp_path: Path) -> None:
    service = _service(tmp_path)
    client = TestClient(
        create_app(
            service,
            admin_token="admin-test-token",
            update_settings=UpdateSettings(
                product_code="live_replay_xia",
                latest_version="1.0.9",
                min_version="1.0.4",
                installer_url="https://license.example.com/downloads/LiveWatchSetup_1.0.9.exe",
                sha256="a" * 64,
                notes="修复授权验签与自动更新。",
                mandatory=True,
            ),
        )
    )

    response = client.get("/v1/update?product_code=live_replay_xia&current_version=1.0.0")
    assert response.status_code == 200
    body = response.json()
    assert body["has_update"] is True
    assert body["latest_version"] == "1.0.9"
    assert body["installer_url"].startswith("https://")
    assert body["sha256"] == "a" * 64
    assert body["mandatory"] is True

    same_version = client.get("/v1/update?product_code=live_replay_xia&current_version=1.0.9")
    assert same_version.status_code == 200
    assert same_version.json()["has_update"] is False


def test_update_manifest_can_be_empty_or_product_scoped(tmp_path: Path) -> None:
    service = _service(tmp_path)
    client = TestClient(create_app(service, admin_token="admin-test-token"))

    empty = client.get("/v1/update?product_code=live_replay_xia&current_version=1.0.0")
    assert empty.status_code == 200
    assert empty.json()["has_update"] is False

    wrong_product = client.get("/v1/update?product_code=wanshan_media&current_version=1.0.0")
    assert wrong_product.status_code == 404
def test_admin_trust_cookie_is_bound_to_device_and_allows_reuse(tmp_path: Path) -> None:
    service = _service(tmp_path)
    client = TestClient(create_app(service, admin_token="admin-test-token"))
    device_headers = {"X-Admin-Device": "browser-device-a"}

    unauthorized = client.get("/admin/session", headers=device_headers)
    assert unauthorized.status_code == 401

    login = client.post(
        "/admin/session",
        headers=device_headers,
        json={"token": "admin-test-token", "device_hash": "browser-device-a"},
    )
    assert login.status_code == 200
    assert "livewatch_admin_trust" in client.cookies
    assert login.json()["expires_at"] > 0

    reused = client.get("/admin/cards", headers=device_headers)
    assert reused.status_code == 200

    wrong_device = client.get("/admin/cards", headers={"X-Admin-Device": "browser-device-b"})
    assert wrong_device.status_code == 401


def test_public_activation_endpoint_is_rate_limited(tmp_path: Path) -> None:
    service = _service(tmp_path)
    client = TestClient(
        create_app(
            service,
            admin_token="admin-test-token",
            rate_limiter=IpRateLimiter(RateLimitPolicy(window_seconds=60, activate_attempts=1, refresh_attempts=5)),
        )
    )

    first = client.post("/v1/activate", json={"card_key": "LRX-INVALID", "device_hash": "device-a"})
    second = client.post("/v1/activate", json={"card_key": "LRX-INVALID", "device_hash": "device-a"})

    assert first.status_code == 400
    assert second.status_code == 429
    assert second.headers["retry-after"].isdigit()
