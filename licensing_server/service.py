"""Card-key storage, device binding, and Ed25519 license signing.

This module is deliberately independent of the desktop client.  It runs only
on the licensing server, where the signing private key is supplied through an
environment variable and never written into the distributable application.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


PRODUCT_FEATURES = {
    "live_replay_xia": {"basic", "export", "batch", "live_monitor", "ai_replay", "short_video_ai", "lead_radar"},
    # 独立获客产品：它只能使用评论线索与导出，不能解锁复盘虾功能。
    "lead_shrimp": {"basic", "lead_radar", "export"},
    "wanshan_media": {"basic", "topic_radar", "copywriting", "prompt_templates", "video_workshop", "distribution", "analytics", "updates"},
    "wanshan_zimeiti": {"basic", "topic_radar", "copywriting", "prompt_templates", "video_workshop", "distribution", "analytics", "updates"},
}
VALID_PRODUCT_CODES = set(PRODUCT_FEATURES)
VALID_FEATURES = set().union(*PRODUCT_FEATURES.values())
DEFAULT_POLICIES = {
    "live_replay_xia": {"max_active_rooms": 10, "export_watermark": True, "force_upgrade_below": ""},
    "lead_shrimp": {},
    "wanshan_media": {},
    "wanshan_zimeiti": {},
}

def default_policy_for_product(product_code: str | None) -> dict[str, Any]:
    return dict(DEFAULT_POLICIES.get(str(product_code or "").strip(), {}))


class LicenseError(ValueError):
    """User-safe licensing error."""


@dataclass(frozen=True)
class LicenseSettings:
    db_path: Path
    signing_private_key: str
    token_hash_secret: str
    product_code: str = "live_replay_xia"
    license_days: int = 3
    grace_days: int = 1


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode((value.strip() + "=" * (-len(value.strip()) % 4)).encode("ascii"))


class LicenseService:
    def __init__(self, settings: LicenseSettings) -> None:
        if not settings.signing_private_key.strip():
            raise ValueError("LICENSE_SIGNING_PRIVATE_KEY 未配置")
        if not settings.token_hash_secret.strip():
            raise ValueError("LICENSE_TOKEN_HASH_SECRET 未配置")
        if settings.license_days < 1 or settings.grace_days < 0:
            raise ValueError("授权有效期配置无效")
        self.settings = settings
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._signing_key = Ed25519PrivateKey.from_private_bytes(
                _b64url_decode(settings.signing_private_key)
            )
        except ValueError as exc:
            raise ValueError("LICENSE_SIGNING_PRIVATE_KEY 不是 Ed25519 原始私钥") from exc
        self._init_db()

    def public_key_b64url(self) -> str:
        public_raw = self._signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return _b64url_encode(public_raw)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.settings.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS card_keys (
                    id TEXT PRIMARY KEY,
                    key_hash TEXT NOT NULL UNIQUE,
                    key_prefix TEXT NOT NULL,
                    key_ciphertext TEXT NOT NULL DEFAULT '',
                    product_code TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    max_devices INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    expires_at INTEGER NOT NULL DEFAULT 0,
                    policy_json TEXT NOT NULL DEFAULT '{}',
                    note TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS activations (
                    id TEXT PRIMARY KEY,
                    card_id TEXT NOT NULL REFERENCES card_keys(id),
                    device_hash TEXT NOT NULL,
                    refresh_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    app_version TEXT NOT NULL DEFAULT '',
                    UNIQUE(card_id, device_hash)
                );
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    card_id TEXT,
                    activation_id TEXT,
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_activations_card_status
                    ON activations(card_id, status);
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(card_keys)").fetchall()}
            if "policy_json" not in columns:
                conn.execute("ALTER TABLE card_keys ADD COLUMN policy_json TEXT NOT NULL DEFAULT '{}'")
            if "key_ciphertext" not in columns:
                conn.execute("ALTER TABLE card_keys ADD COLUMN key_ciphertext TEXT NOT NULL DEFAULT ''")

    def _hash_secret(self, value: str) -> str:
        return hmac.new(
            self.settings.token_hash_secret.encode("utf-8"),
            value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _card_storage_key(self) -> bytes:
        return hashlib.sha256(
            ("card-key-storage-v1:" + self.settings.token_hash_secret).encode("utf-8")
        ).digest()

    def _encrypt_card_key(self, card_key: str) -> str:
        nonce = os.urandom(12)
        encrypted = AESGCM(self._card_storage_key()).encrypt(
            nonce,
            card_key.encode("utf-8"),
            b"livewatch-card-key",
        )
        return _b64url_encode(nonce + encrypted)

    def _decrypt_card_key(self, value: str | None) -> str:
        if not value:
            return ""
        try:
            raw = _b64url_decode(value)
            nonce, encrypted = raw[:12], raw[12:]
            return AESGCM(self._card_storage_key()).decrypt(
                nonce,
                encrypted,
                b"livewatch-card-key",
            ).decode("utf-8")
        except Exception:
            return ""

    @staticmethod
    def _clean_device_hash(device_hash: str) -> str:
        value = str(device_hash or "").strip().lower()
        if not value or len(value) > 128:
            raise LicenseError("设备标识无效")
        return value

    @staticmethod
    def _clean_app_version(app_version: str) -> str:
        value = str(app_version or "").strip()
        if len(value) > 128:
            raise LicenseError("客户端版本字段无效")
        return value

    def _audit(
        self,
        conn: sqlite3.Connection,
        *,
        action: str,
        card_id: str | None = None,
        activation_id: str | None = None,
        detail: str = "",
        now: int,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_logs(id, created_at, action, card_id, activation_id, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, now, action, card_id, activation_id, detail[:500]),
        )

    @staticmethod
    def _new_card_key(prefix: str = "LRX") -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        safe_prefix = "".join(ch for ch in str(prefix or "LRX").upper() if ch.isalnum())[:6] or "LRX"
        groups = ["".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4)]
        return safe_prefix + "-" + "-".join(groups)

    @staticmethod
    def normalize_policy(policy: dict[str, Any] | None, product_code: str | None = None) -> dict[str, Any]:
        selected_product = str(product_code or "").strip()
        if selected_product in {"wanshan_media", "wanshan_zimeiti"}:
            return {}
        raw = policy if isinstance(policy, dict) else {}
        result = default_policy_for_product(selected_product)
        if selected_product == "live_replay_xia":
            try:
                result["max_active_rooms"] = max(1, min(int(raw.get("max_active_rooms", result["max_active_rooms"])), 50))
            except (TypeError, ValueError):
                result["max_active_rooms"] = DEFAULT_POLICIES["live_replay_xia"]["max_active_rooms"]
            result["export_watermark"] = bool(raw.get("export_watermark", result["export_watermark"]))
            result["force_upgrade_below"] = str(raw.get("force_upgrade_below", "") or "").strip()[:64]
        return result

    @classmethod
    def _policy_from_json(cls, value: str | None, product_code: str | None = None) -> dict[str, Any]:
        try:
            loaded = json.loads(value or "{}")
        except json.JSONDecodeError:
            loaded = {}
        return cls.normalize_policy(loaded, product_code=product_code)

    def create_card_key(
        self,
        *,
        features: set[str],
        product_code: str | None = None,
        max_devices: int = 1,
        expires_at: int = 0,
        policy: dict[str, Any] | None = None,
        note: str = "",
        now: int | None = None,
    ) -> str:
        if max_devices < 1 or max_devices > 20:
            raise LicenseError("设备数必须在 1 到 20 之间")
        selected_product = str(product_code or self.settings.product_code).strip()
        if selected_product not in VALID_PRODUCT_CODES:
            raise LicenseError("产品代码无效")
        allowed_features = sorted(set(features) & PRODUCT_FEATURES[selected_product])
        if not allowed_features:
            raise LicenseError("至少选择一个有效功能")
        safe_policy = self.normalize_policy(policy, selected_product)
        created_at = int(now if now is not None else time.time())
        for _ in range(8):
            card_key = self._new_card_key(
                {
                    "lead_shrimp": "LEAD",
                    "wanshan_zimeiti": "WSZ",
                    "wanshan_media": "WSM",
                }.get(selected_product, "LRX")
            )
            card_id = uuid.uuid4().hex
            try:
                with self._connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO card_keys(id, key_hash, key_prefix, key_ciphertext, product_code, features_json, max_devices, status, expires_at, policy_json, note, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                        """,
                        (
                            card_id,
                            self._hash_secret(card_key),
                            card_key[:8],
                            self._encrypt_card_key(card_key),
                            selected_product,
                            json.dumps(allowed_features, ensure_ascii=False),
                            max_devices,
                            max(0, int(expires_at)),
                            json.dumps(safe_policy, ensure_ascii=False, sort_keys=True),
                            str(note or "")[:500],
                            created_at,
                        ),
                    )
                    self._audit(conn, action="card_created", card_id=card_id, now=created_at)
                return card_key
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("生成卡密失败，请重试")

    def _find_card(self, conn: sqlite3.Connection, card_key: str) -> sqlite3.Row:
        value = str(card_key or "").strip().upper()
        if not value or len(value) > 128:
            raise LicenseError("卡密格式无效")
        card = conn.execute(
            "SELECT * FROM card_keys WHERE key_hash = ?", (self._hash_secret(value),)
        ).fetchone()
        if card is None:
            raise LicenseError("卡密无效")
        if card["status"] != "active":
            raise LicenseError("卡密已停用")
        return card

    def _validate_card_time(self, card: sqlite3.Row, now: int) -> None:
        expires_at = int(card["expires_at"] or 0)
        if expires_at and now > expires_at:
            raise LicenseError("卡密已到期")

    def _license_doc(self, activation: sqlite3.Row, card: sqlite3.Row, now: int) -> dict[str, str]:
        card_expiry = int(card["expires_at"] or 0)
        regular_expiry = now + self.settings.license_days * 86400
        expires_at = min(regular_expiry, card_expiry) if card_expiry else regular_expiry
        # A card with an explicit expiry (for example "1 minute" test cards)
        # must not be silently extended by the offline grace window. Grace is
        # reserved for regular fixed-duration licenses during transient network
        # failures, not for cards that the admin deliberately made short-lived.
        grace_until = expires_at if card_expiry else expires_at + self.settings.grace_days * 86400
        payload = {
            "license_id": activation["id"],
            "activation_id": activation["id"],
            "product_code": card["product_code"],
            "device_hash": activation["device_hash"],
            "features": json.loads(card["features_json"]),
            "policy": self._policy_from_json(card["policy_json"], card["product_code"]),
            "issued_at": now,
            "expires_at": expires_at,
            "grace_until": grace_until,
        }
        payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {
            "alg": "Ed25519",
            "payload": _b64url_encode(payload_bytes),
            "signature": _b64url_encode(self._signing_key.sign(payload_bytes)),
        }

    def activate(
        self,
        *,
        card_key: str,
        device_hash: str,
        product_code: str = "",
        app_version: str = "",
        now: int | None = None,
    ) -> dict[str, Any]:
        now_ts = int(now if now is not None else time.time())
        device = self._clean_device_hash(device_hash)
        version = self._clean_app_version(app_version)
        with self._connect() as conn:
            card = self._find_card(conn, card_key)
            if product_code and product_code.strip() != card["product_code"]:
                raise LicenseError("授权产品不匹配")
            self._validate_card_time(card, now_ts)
            activation = conn.execute(
                "SELECT * FROM activations WHERE card_id = ? AND device_hash = ?", (card["id"], device)
            ).fetchone()
            if activation is not None and activation["status"] == "frozen":
                raise LicenseError("当前设备授权已冻结")
            if activation is None:
                active_count = conn.execute(
                    "SELECT COUNT(*) FROM activations WHERE card_id = ? AND status = 'active'", (card["id"],)
                ).fetchone()[0]
                if active_count >= int(card["max_devices"]):
                    raise LicenseError("卡密绑定设备数已达到上限")
                activation_id = uuid.uuid4().hex
                refresh_token = secrets.token_urlsafe(32)
                conn.execute(
                    """
                    INSERT INTO activations(id, card_id, device_hash, refresh_hash, status, first_seen_at, last_seen_at, app_version)
                    VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (activation_id, card["id"], device, self._hash_secret(refresh_token), now_ts, now_ts, version),
                )
                activation = conn.execute("SELECT * FROM activations WHERE id = ?", (activation_id,)).fetchone()
                self._audit(conn, action="activated", card_id=card["id"], activation_id=activation_id, now=now_ts)
            else:
                refresh_token = secrets.token_urlsafe(32)
                conn.execute(
                    "UPDATE activations SET refresh_hash = ?, last_seen_at = ?, app_version = ? WHERE id = ?",
                    (self._hash_secret(refresh_token), now_ts, version, activation["id"]),
                )
                activation = conn.execute("SELECT * FROM activations WHERE id = ?", (activation["id"],)).fetchone()
                self._audit(conn, action="reactivated", card_id=card["id"], activation_id=activation["id"], now=now_ts)
            return {
                "license": self._license_doc(activation, card, now_ts),
                "activation_id": activation["id"],
                "refresh_token": refresh_token,
            }

    def refresh(
        self,
        *,
        activation_id: str,
        refresh_token: str,
        device_hash: str,
        product_code: str = "",
        app_version: str = "",
        now: int | None = None,
    ) -> dict[str, Any]:
        now_ts = int(now if now is not None else time.time())
        device = self._clean_device_hash(device_hash)
        version = self._clean_app_version(app_version)
        if not activation_id or len(activation_id) > 128 or not refresh_token or len(refresh_token) > 256:
            raise LicenseError("授权刷新凭据无效")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT activations.*, card_keys.product_code, card_keys.features_json, card_keys.max_devices,
                       card_keys.status AS card_status, card_keys.expires_at AS card_expires_at,
                       card_keys.policy_json AS policy_json
                FROM activations JOIN card_keys ON card_keys.id = activations.card_id
                WHERE activations.id = ?
                """,
                (activation_id,),
            ).fetchone()
            if row is None or not hmac.compare_digest(row["refresh_hash"], self._hash_secret(refresh_token)):
                raise LicenseError("授权刷新凭据无效")
            if row["device_hash"] != device:
                raise LicenseError("授权不属于当前设备")
            if product_code and product_code.strip() != row["product_code"]:
                raise LicenseError("授权产品不匹配")
            if row["status"] == "frozen":
                raise LicenseError("当前设备授权已冻结")
            if row["status"] != "active" or row["card_status"] != "active":
                raise LicenseError("授权已停用")
            card = {
                "product_code": row["product_code"],
                "features_json": row["features_json"],
                "expires_at": row["card_expires_at"],
                "policy_json": row["policy_json"],
            }
            self._validate_card_time(card, now_ts)
            conn.execute("UPDATE activations SET last_seen_at = ?, app_version = ? WHERE id = ?", (now_ts, version, activation_id))
            activation = conn.execute("SELECT * FROM activations WHERE id = ?", (activation_id,)).fetchone()
            self._audit(conn, action="refreshed", card_id=activation["card_id"], activation_id=activation_id, now=now_ts)
            return {"license": self._license_doc(activation, card, now_ts), "activation_id": activation_id}

    def freeze_activation(self, activation_id: str, *, reason: str = "", now: int | None = None) -> None:
        now_ts = int(now if now is not None else time.time())
        with self._connect() as conn:
            activation = conn.execute("SELECT * FROM activations WHERE id = ?", (activation_id,)).fetchone()
            if activation is None:
                raise LicenseError("授权记录不存在")
            conn.execute("UPDATE activations SET status = 'frozen' WHERE id = ?", (activation_id,))
            self._audit(conn, action="frozen", card_id=activation["card_id"], activation_id=activation_id, detail=reason, now=now_ts)

    def unbind_activation(self, activation_id: str, *, reason: str = "", now: int | None = None) -> None:
        now_ts = int(now if now is not None else time.time())
        with self._connect() as conn:
            activation = conn.execute("SELECT * FROM activations WHERE id = ?", (activation_id,)).fetchone()
            if activation is None:
                raise LicenseError("授权记录不存在")
            conn.execute("UPDATE activations SET status = 'unbound' WHERE id = ?", (activation_id,))
            self._audit(conn, action="unbound", card_id=activation["card_id"], activation_id=activation_id, detail=reason, now=now_ts)

    def delete_card_key(self, card_id: str, *, reason: str = "", now: int | None = None) -> None:
        now_ts = int(now if now is not None else time.time())
        with self._connect() as conn:
            card = conn.execute("SELECT * FROM card_keys WHERE id = ?", (card_id,)).fetchone()
            if card is None or card["status"] == "deleted":
                raise LicenseError("卡密不存在")
            conn.execute("UPDATE card_keys SET status = 'deleted' WHERE id = ?", (card_id,))
            conn.execute("UPDATE activations SET status = 'frozen' WHERE card_id = ? AND status = 'active'", (card_id,))
            self._audit(conn, action="card_deleted", card_id=card_id, detail=reason, now=now_ts)

    def list_activations(self, *, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT activations.id, activations.card_id, activations.device_hash, activations.status,
                       activations.first_seen_at, activations.last_seen_at, activations.app_version,
                       card_keys.key_prefix, card_keys.max_devices, card_keys.note, card_keys.expires_at
                FROM activations JOIN card_keys ON card_keys.id = activations.card_id
                WHERE card_keys.status != 'deleted'
                ORDER BY activations.last_seen_at DESC LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_cards(self, *, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT card_keys.id, card_keys.key_prefix, card_keys.key_ciphertext,
                       card_keys.product_code, card_keys.features_json,
                       card_keys.max_devices, card_keys.status, card_keys.expires_at, card_keys.note,
                       card_keys.policy_json,
                       card_keys.created_at,
                       SUM(CASE WHEN activations.status = 'active' THEN 1 ELSE 0 END) AS active_devices
                FROM card_keys LEFT JOIN activations ON activations.card_id = card_keys.id
                WHERE card_keys.status != 'deleted'
                GROUP BY card_keys.id
                ORDER BY card_keys.created_at DESC LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["features"] = json.loads(item.pop("features_json"))
            item["policy"] = self._policy_from_json(item.pop("policy_json"), item.get("product_code"))
            item["card_key"] = self._decrypt_card_key(item.pop("key_ciphertext", ""))
            result.append(item)
        return result
