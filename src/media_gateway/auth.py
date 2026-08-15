"""Dashboard sessions and trusted gateway authentication."""

from __future__ import annotations

import base64
import hmac
import time
from dataclasses import dataclass


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class Sessions:
    key: bytes
    ttl_seconds: int = 12 * 60 * 60

    def issue(self) -> str:
        expires = str(int(time.time()) + self.ttl_seconds)
        signature = _b64(hmac.digest(self.key, expires.encode(), "sha256"))
        return f"{expires}.{signature}"

    def valid(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        expires, signature = token.split(".", 1)
        if not expires.isdigit() or int(expires) < int(time.time()):
            return False
        expected = _b64(hmac.digest(self.key, expires.encode(), "sha256"))
        return hmac.compare_digest(signature, expected)

    def csrf(self, token: str) -> str:
        return _b64(hmac.digest(self.key, f"csrf:{token}".encode(), "sha256"))

    def valid_csrf(self, token: str, value: str | None) -> bool:
        return value is not None and hmac.compare_digest(self.csrf(token), value)
