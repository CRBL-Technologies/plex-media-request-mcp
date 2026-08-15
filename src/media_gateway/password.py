"""Generate and verify the dashboard's scrypt password format."""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import os


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must contain at least 12 characters")
    if len(password) > 1024:
        raise ValueError("password is too long")
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024
    )
    return f"scrypt$32768$8$1${_encode(salt)}${_encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    if len(password) > 1024 or len(encoded) > 256:
        return False
    try:
        algorithm, n, r, p, salt, expected = encoded.strip().split("$")
        if (algorithm, n, r, p) != ("scrypt", "32768", "8", "1"):
            return False
        decoded_salt = _decode(salt)
        decoded_expected = _decode(expected)
        if len(decoded_salt) != 16 or len(decoded_expected) != 32:
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=decoded_salt,
            n=32768,
            r=8,
            p=1,
            dklen=32,
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(actual, decoded_expected)
    except (ValueError, TypeError):
        return False


def main() -> None:
    first = getpass.getpass("New dashboard password: ")
    second = getpass.getpass("Repeat dashboard password: ")
    if first != second:
        raise SystemExit("passwords do not match")
    print(hash_password(first))
