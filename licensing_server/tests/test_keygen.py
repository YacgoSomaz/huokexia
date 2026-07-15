import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from licensing_server.keygen import generate_keypair


def test_generated_license_keypair_is_base64url_and_matches():
    private_b64, public_b64 = generate_keypair()

    assert "=" not in private_b64
    assert "=" not in public_b64
    assert len(private_b64) >= 40
    assert len(public_b64) >= 40
    private = Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(private_b64 + "="))
    assert base64.urlsafe_b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode().rstrip("=") == public_b64
