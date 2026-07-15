"""Generate an Ed25519 key pair for the LiveWatch licensing server.

Run this only on the machine where the private key will be protected. Never
copy the private key into the desktop app, repository, or installation package.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def generate_keypair() -> tuple[str, str]:
    """Return (private_key, public_key) as base64url Ed25519 raw bytes."""
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return _b64url(private_raw), _b64url(public_raw)


def main() -> int:
    private_key, public_key = generate_keypair()
    print("LICENSE_SIGNING_PRIVATE_KEY=" + private_key)
    print("LIVEWATCH_LICENSE_PUBLIC_KEY=" + public_key)
    print("\n私钥只写入授权服务器环境变量；公钥用于商业安装包构建。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
