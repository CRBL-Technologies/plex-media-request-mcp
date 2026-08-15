import base64
import hashlib

from media_gateway.password import hash_password, verify_password


def test_password_round_trip() -> None:
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong password", encoded)
    assert not verify_password("anything", "not-a-hash")


def test_existing_scrypt_cost_remains_verifiable() -> None:
    password = "existing-dashboard-password"
    salt = b"0123456789abcdef"
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )

    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    encoded = f"scrypt$16384$8$1${encode(salt)}${encode(digest)}"
    assert verify_password(password, encoded)
