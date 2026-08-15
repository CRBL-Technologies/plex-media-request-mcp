"""Small authentication primitives for the operations dashboard.

The browser receives opaque session and CSRF values.  Only SHA-256 digests are
retained server-side, so an accidental session-store dump is not itself a set
of bearer credentials.  Sessions deliberately do not survive process restart.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import hmac
import secrets
import threading
import time
from typing import Callable, Iterable
from urllib.parse import urlsplit


PASSWORD_SCHEME = "scrypt"
DEFAULT_SCRYPT_N = 1 << 14
DEFAULT_SCRYPT_R = 8
DEFAULT_SCRYPT_P = 1
MAX_PASSWORD_BYTES = 1024
SESSION_TOKEN_BYTES = 32
CSRF_TOKEN_BYTES = 32
DEFAULT_IDLE_SECONDS = 30 * 60
DEFAULT_ABSOLUTE_SECONDS = 12 * 60 * 60


class DashboardAuthError(ValueError):
    """Base class for bounded authentication failures."""


class InvalidPasswordHash(DashboardAuthError):
    """Raised when the configured password hash is malformed or unsafe."""


class InvalidRequestOrigin(DashboardAuthError):
    """Raised when a browser request does not match configured origins."""


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise InvalidPasswordHash("invalid password hash encoding")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeEncodeError) as exc:
        raise InvalidPasswordHash("invalid password hash encoding") from exc


def _password_bytes(password: str) -> bytes:
    if not isinstance(password, str):
        raise DashboardAuthError("password must be text")
    encoded = password.encode("utf-8")
    if not encoded or len(encoded) > MAX_PASSWORD_BYTES:
        raise DashboardAuthError("password length is invalid")
    return encoded


def hash_password(
    password: str,
    *,
    salt: bytes | None = None,
    n: int = DEFAULT_SCRYPT_N,
    r: int = DEFAULT_SCRYPT_R,
    p: int = DEFAULT_SCRYPT_P,
) -> str:
    """Return a self-describing stdlib scrypt password hash."""

    if n != DEFAULT_SCRYPT_N or r != DEFAULT_SCRYPT_R or p != DEFAULT_SCRYPT_P:
        raise InvalidPasswordHash("unsupported scrypt work factor")
    actual_salt = secrets.token_bytes(16) if salt is None else bytes(salt)
    if len(actual_salt) != 16:
        raise InvalidPasswordHash("password salt must be 16 bytes")
    digest = hashlib.scrypt(
        _password_bytes(password),
        salt=actual_salt,
        n=n,
        r=r,
        p=p,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    return "$".join(
        (
            PASSWORD_SCHEME,
            str(n),
            str(r),
            str(p),
            _b64encode(actual_salt),
            _b64encode(digest),
        )
    )


def verify_password(password: str, encoded_hash: str) -> bool:
    """Verify a configured hash without accepting weaker parameter variants."""

    try:
        n, r, p, salt, expected = parse_password_hash(encoded_hash)
        actual = hashlib.scrypt(
            _password_bytes(password),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=32,
            maxmem=64 * 1024 * 1024,
        )
    except (DashboardAuthError, TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def parse_password_hash(encoded_hash: str) -> tuple[int, int, int, bytes, bytes]:
    """Parse the one supported scrypt encoding without doing the KDF."""

    if not isinstance(encoded_hash, str) or len(encoded_hash.encode("utf-8")) > 4096:
        raise InvalidPasswordHash("invalid password hash")
    parts = encoded_hash.split("$")
    if len(parts) != 6:
        raise InvalidPasswordHash("invalid password hash")
    scheme, n_text, r_text, p_text, salt_text, digest_text = parts
    try:
        n, r, p = int(n_text), int(r_text), int(p_text)
    except (TypeError, ValueError) as exc:
        raise InvalidPasswordHash("invalid password hash parameters") from exc
    if (
        scheme != PASSWORD_SCHEME
        or n_text != str(DEFAULT_SCRYPT_N)
        or r_text != str(DEFAULT_SCRYPT_R)
        or p_text != str(DEFAULT_SCRYPT_P)
    ):
        raise InvalidPasswordHash("unsupported password hash")
    salt = _b64decode(salt_text)
    expected = _b64decode(digest_text)
    if len(salt) != 16 or len(expected) != 32:
        raise InvalidPasswordHash("invalid password hash size")
    if _b64encode(salt) != salt_text or _b64encode(expected) != digest_text:
        raise InvalidPasswordHash("invalid password hash encoding")
    return n, r, p, salt, expected


def validate_password_hash(encoded_hash: str) -> None:
    """Validate a dashboard password hash during startup."""

    parse_password_hash(encoded_hash)


def _canonical_origin(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise InvalidRequestOrigin("malformed origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise InvalidRequestOrigin("malformed origin") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise InvalidRequestOrigin("malformed origin")
    default_port = 443 if parsed.scheme == "https" else 80
    authority = parsed.hostname.lower()
    if "%" in authority or any(
        ord(char) < 0x21 or char.isspace() for char in authority
    ):
        raise InvalidRequestOrigin("malformed origin")
    if ":" in authority and not authority.startswith("["):
        authority = f"[{authority}]"
    if port is not None and port != default_port:
        authority = f"{authority}:{port}"
    return parsed.scheme, authority


def validate_request_origin(
    *,
    host: str | None,
    origin: str | None,
    allowed_origins: Iterable[str],
    require_origin: bool,
) -> None:
    """Validate Host and Origin against exact configured browser origins."""

    configured = {_canonical_origin(value) for value in allowed_origins}
    if not configured:
        raise InvalidRequestOrigin("no dashboard origin is configured")
    if not isinstance(host, str) or not host.strip() or "," in host:
        raise InvalidRequestOrigin("missing or ambiguous host")
    normalized_host = host.strip().lower()
    if normalized_host not in {authority for _, authority in configured}:
        raise InvalidRequestOrigin("host is not allowed")
    if origin is None:
        if require_origin:
            raise InvalidRequestOrigin("origin is required")
        return
    canonical_origin = _canonical_origin(origin)
    if canonical_origin not in configured or canonical_origin[1] != normalized_host:
        raise InvalidRequestOrigin("origin is not allowed")


def _digest_secret(value: str) -> bytes:
    return hashlib.sha256(value.encode("ascii")).digest()


@dataclass(frozen=True, slots=True)
class SessionSecrets:
    token: str
    csrf_token: str


@dataclass(frozen=True, slots=True)
class DashboardSession:
    actor: str
    # Hex SHA-256 of the opaque bearer token.  It is safe to propagate to the
    # companion for audit binding without turning the public session object
    # into another bearer credential.
    session_digest: str
    created_at: float
    last_seen_at: float
    absolute_expires_at: float


@dataclass(slots=True)
class _StoredSession:
    actor: str
    csrf_digest: bytes
    created_at: float
    last_seen_at: float
    absolute_expires_at: float


class SessionStore:
    """Thread-safe in-memory server-side dashboard session store."""

    def __init__(
        self,
        *,
        idle_seconds: int = DEFAULT_IDLE_SECONDS,
        absolute_seconds: int = DEFAULT_ABSOLUTE_SECONDS,
        clock: Callable[[], float] = time.time,
        max_sessions: int = 64,
    ) -> None:
        if (
            isinstance(idle_seconds, bool)
            or not isinstance(idle_seconds, int)
            or isinstance(absolute_seconds, bool)
            or not isinstance(absolute_seconds, int)
            or idle_seconds <= 0
            or absolute_seconds < idle_seconds
        ):
            raise ValueError("invalid dashboard session lifetimes")
        if (
            isinstance(max_sessions, bool)
            or not isinstance(max_sessions, int)
            or max_sessions <= 0
            or max_sessions > 1024
        ):
            raise ValueError("invalid dashboard session capacity")
        self.idle_seconds = idle_seconds
        self.absolute_seconds = absolute_seconds
        self.max_sessions = max_sessions
        self._clock = clock
        self._sessions: dict[bytes, _StoredSession] = {}
        self._lock = threading.RLock()

    def create(self, actor: str = "admin") -> tuple[SessionSecrets, DashboardSession]:
        if not actor or len(actor) > 128:
            raise ValueError("invalid dashboard actor")
        now = self._clock()
        token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        csrf = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
        stored = _StoredSession(
            actor=actor,
            csrf_digest=_digest_secret(csrf),
            created_at=now,
            last_seen_at=now,
            absolute_expires_at=now + self.absolute_seconds,
        )
        with self._lock:
            self._purge_locked(now)
            if len(self._sessions) >= self.max_sessions:
                oldest = min(
                    self._sessions,
                    key=lambda key: self._sessions[key].last_seen_at,
                )
                del self._sessions[oldest]
            self._sessions[_digest_secret(token)] = stored
        token_digest = _digest_secret(token)
        return SessionSecrets(token=token, csrf_token=csrf), self._public(
            stored, token_digest
        )

    def validate(
        self,
        token: str,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = False,
        touch: bool = True,
    ) -> DashboardSession | None:
        try:
            token_digest = _digest_secret(token)
        except (AttributeError, UnicodeEncodeError):
            return None
        now = self._clock()
        with self._lock:
            stored = self._sessions.get(token_digest)
            if stored is None:
                return None
            if self._expired(stored, now):
                del self._sessions[token_digest]
                return None
            if require_csrf:
                if csrf_token is None:
                    return None
                try:
                    csrf_digest = _digest_secret(csrf_token)
                except (AttributeError, UnicodeEncodeError):
                    return None
                if not hmac.compare_digest(csrf_digest, stored.csrf_digest):
                    return None
            if touch:
                stored.last_seen_at = now
            return self._public(stored, token_digest)

    def revoke(self, token: str) -> bool:
        try:
            digest = _digest_secret(token)
        except (AttributeError, UnicodeEncodeError):
            return False
        with self._lock:
            return self._sessions.pop(digest, None) is not None

    def purge(self) -> int:
        with self._lock:
            return self._purge_locked(self._clock())

    def _purge_locked(self, now: float) -> int:
        expired = [
            key for key, value in self._sessions.items() if self._expired(value, now)
        ]
        for key in expired:
            del self._sessions[key]
        return len(expired)

    def _expired(self, value: _StoredSession, now: float) -> bool:
        return (
            now < value.created_at
            or now >= value.absolute_expires_at
            or now - value.last_seen_at >= self.idle_seconds
        )

    @staticmethod
    def _public(value: _StoredSession, token_digest: bytes) -> DashboardSession:
        return DashboardSession(
            actor=value.actor,
            session_digest=token_digest.hex(),
            created_at=value.created_at,
            last_seen_at=value.last_seen_at,
            absolute_expires_at=value.absolute_expires_at,
        )
