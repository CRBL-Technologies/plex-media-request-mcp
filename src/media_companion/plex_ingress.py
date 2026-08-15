"""Bounded, side-effect free Plex webhook ingress helpers.

Plex webhooks are useful observations, not authenticated commands.  This
module intentionally stops at the trust boundary: it verifies the loopback
capability and request shape, parses only the small subset of metadata needed
by the notification ledger, and returns a normalized record.  It does not
open a socket, write an upload, persist the request, or fetch anything from
Plex.

The HTTP adapter can use :func:`parse_plex_webhook` and persist the returned
``NormalizedPlexEvent`` in one transaction before acknowledging the request.
Poster bytes are counted and hashed only while parsing; they are never part of
the returned record.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from typing import Any, BinaryIO, Literal
from urllib.parse import urlsplit

# These values are part of the receiver contract.  Keep them as module
# constants so an HTTP server and its tests cannot accidentally drift apart.
MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_JSON_PART_BYTES = 512 * 1024
MAX_IMAGE_PART_BYTES = 8 * 1024 * 1024
MAX_PARTS = 4
BODY_PARSE_DEADLINE_SECONDS = 15.0
RATE_LIMIT_PER_MINUTE = 120
RATE_LIMIT_BURST = 240
CAPABILITY_BYTES = 32
CAPABILITY_OVERLAP_SECONDS = 10 * 60

# More explicit names are convenient at call sites and preserve the wording
# used by the design contract.
PLEX_WEBHOOK_MAX_BODY_BYTES = MAX_BODY_BYTES
PLEX_WEBHOOK_MAX_JSON_BYTES = MAX_JSON_PART_BYTES
PLEX_WEBHOOK_MAX_IMAGE_BYTES = MAX_IMAGE_PART_BYTES
PLEX_WEBHOOK_MAX_PARTS = MAX_PARTS
PLEX_WEBHOOK_BODY_DEADLINE_SECONDS = BODY_PARSE_DEADLINE_SECONDS
PLEX_WEBHOOK_RATE_LIMIT_PER_MINUTE = RATE_LIMIT_PER_MINUTE
PLEX_WEBHOOK_RATE_LIMIT_BURST = RATE_LIMIT_BURST

_RATING_KEY_RE = re.compile(r"[1-9][0-9]*\Z")
_SERVER_UUID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_EVENT_TYPE = "library.new"
_UNIT_TYPES = frozenset({"movie", "episode"})
_HINT_TYPES = frozenset({"show", "season"})
_ALLOWED_TYPES = _UNIT_TYPES | _HINT_TYPES
_UNSET = object()


def structured_plex_event_key(
    server_uuid: str,
    library_uuid: str,
    rating_key: str,
    tombstone_generation: int | None = None,
) -> str:
    """Encode Plex identity fields without delimiter-based collisions.

    Plex identifiers are independently bounded but may contain ``:``.  A
    canonical JSON object keeps the durable string key inspectable while
    making ``(server, library, rating, generation)`` unambiguous.
    """

    if tombstone_generation is not None and (
        isinstance(tombstone_generation, bool)
        or not isinstance(tombstone_generation, int)
        or tombstone_generation < 0
    ):
        raise WebhookValidationError("invalid_tombstone_generation")
    fields: dict[str, object] = {
        "library_uuid": library_uuid,
        "rating_key": rating_key,
        "server_uuid": server_uuid,
        "version": 1,
    }
    if tombstone_generation is not None:
        fields["tombstone_generation"] = tombstone_generation
    return json.dumps(fields, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


class PlexIngressError(ValueError):
    """Base class for a malformed or policy-invalid webhook request.

    Error text deliberately contains only a stable reason code.  The
    capability, private URLs, filenames, payload, and poster data must never
    be interpolated into an HTTP error or log message.
    """

    status_code = 400

    def __init__(self, reason: str = "invalid_webhook") -> None:
        self.reason = reason
        super().__init__(reason)


class WebhookValidationError(PlexIngressError):
    """The request failed capability, shape, or metadata validation."""


class WebhookLimitError(WebhookValidationError):
    """A body, part, field, or parser deadline limit was exceeded."""


class WebhookCapabilityError(WebhookValidationError):
    """The request did not present the configured path capability."""

    status_code = 404


class WebhookContentTypeError(WebhookValidationError):
    """The request did not use the supported multipart content type."""


class WebhookPersistenceError(PlexIngressError):
    """Marker used by a future HTTP adapter when inbox persistence fails."""

    status_code = 503


# Compatibility aliases make the boundary pleasant to consume without making
# callers depend on the implementation's preferred exception name.
InvalidWebhookError = WebhookValidationError
PlexWebhookError = PlexIngressError
PlexWebhookValidationError = WebhookValidationError


def _deadline_check(deadline: float | None) -> None:
    if deadline is None:
        return
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise WebhookLimitError("invalid_parse_deadline")
    try:
        finite_deadline = float(deadline)
    except (OverflowError, ValueError) as exc:
        raise WebhookLimitError("invalid_parse_deadline") from exc
    if not math.isfinite(finite_deadline):
        raise WebhookLimitError("invalid_parse_deadline")
    if time.monotonic() > finite_deadline:
        raise WebhookLimitError("parse_deadline_exceeded")


def _safe_text(
    value: object,
    field_name: str,
    *,
    maximum: int = 512,
    required: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise WebhookValidationError(f"missing_{field_name}")
        return None
    if not isinstance(value, str):
        raise WebhookValidationError(f"invalid_{field_name}")
    # The webhook is untrusted input.  Strip control characters before this
    # value can reach a log, Telegram renderer, or model context.  Newlines
    # are not meaningful in a title/identifier and are removed as well.
    cleaned = "".join(
        character for character in value if 0x20 <= ord(character) != 0x7F
    )
    cleaned = cleaned.strip()
    if not cleaned:
        if required:
            raise WebhookValidationError(f"missing_{field_name}")
        return None
    if len(cleaned) > maximum:
        raise WebhookLimitError(f"{field_name}_too_long")
    return cleaned


def _safe_identifier(
    value: object,
    field_name: str,
    *,
    maximum: int = 256,
    required: bool = False,
) -> str | None:
    text = _safe_text(value, field_name, maximum=maximum, required=required)
    if text is None:
        return None
    if not _IDENTIFIER_RE.fullmatch(text):
        raise WebhookValidationError(f"invalid_{field_name}")
    return text


def canonical_rating_key(value: object, *, maximum_length: int = 32) -> str:
    """Return a bounded canonical Plex rating key.

    Plex rating keys are accepted only as ordinary decimal strings.  This
    rejects zero, leading-zero/alternate numeric forms, separators, query or
    fragment syntax, traversal, and control characters before URL joining.
    """

    if not isinstance(value, str) or len(value) > maximum_length:
        raise WebhookValidationError("invalid_rating_key")
    if not _RATING_KEY_RE.fullmatch(value):
        raise WebhookValidationError("invalid_rating_key")
    return value


def metadata_path(rating_key: object) -> str:
    """Construct the only metadata path accepted by the resolver."""

    return f"/library/metadata/{canonical_rating_key(rating_key)}"


build_metadata_path = metadata_path
canonical_metadata_path = metadata_path


def _canonical_capability_bytes(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("ascii", "strict")
    else:
        raise WebhookCapabilityError("invalid_capability")
    # The generated token is 32 random bytes encoded using URL-safe base64.
    # Keeping a fixed upper bound also protects callers that load a malformed
    # mounted secret before compare_digest is reached.
    if not raw or len(raw) > 512 or any(byte < 0x21 or byte > 0x7E for byte in raw):
        raise WebhookCapabilityError("invalid_capability")
    return raw


def capability_matches(provided: str | bytes, expected: str | bytes) -> bool:
    """Compare capabilities without a value-dependent early exit.

    ``hmac.compare_digest`` is used over fixed-length SHA-256 digests rather
    than directly over variable-length strings.  This keeps the comparison
    work constant even when an attacker supplies a short or long path.
    """

    try:
        supplied = _canonical_capability_bytes(provided)
        configured = _canonical_capability_bytes(expected)
    except (UnicodeError, TypeError, WebhookCapabilityError):
        # Hash a fixed invalid marker so malformed values follow the same
        # constant-size comparison path as well.
        supplied = b"<invalid-capability>"
        configured = b"<invalid-capability>"
        return (
            hmac.compare_digest(
                hashlib.sha256(supplied).digest(), hashlib.sha256(configured).digest()
            )
            and False
        )
    return hmac.compare_digest(
        hashlib.sha256(supplied).digest(), hashlib.sha256(configured).digest()
    )


constant_time_capability_match = capability_matches
constant_time_compare = capability_matches


def generate_capability() -> str:
    """Generate a URL-safe 256-bit receiver path capability."""

    return (
        base64.urlsafe_b64encode(secrets.token_bytes(CAPABILITY_BYTES))
        .rstrip(b"=")
        .decode("ascii")
    )


generate_webhook_capability = generate_capability
new_webhook_capability = generate_capability


def _path_capability(path: str) -> str:
    if (
        not isinstance(path, str)
        or not path
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in path)
    ):
        raise WebhookCapabilityError("invalid_capability_path")
    parsed = urlsplit(path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise WebhookCapabilityError("invalid_capability_path")
    # The receiver may be mounted at /plex/webhook/<token>, but it must never
    # accept an encoded separator or a token in a query parameter.
    segments = parsed.path.split("/")
    if (
        not segments
        or not segments[-1]
        or any(
            segment in {".", ".."}
            or "%2f" in segment.lower()
            or "%5c" in segment.lower()
            for segment in segments
        )
    ):
        raise WebhookCapabilityError("invalid_capability_path")
    return segments[-1]


def validate_capability(path: str, expected: str | bytes) -> None:
    """Validate a request path capability without exposing either value."""

    supplied = _path_capability(path)
    if isinstance(expected, str) and expected.startswith("/"):
        expected = _path_capability(expected)
    if not capability_matches(supplied, expected):
        raise WebhookCapabilityError("invalid_capability")


class WebhookCapability:
    """A redacted current/previous capability matcher.

    The raw token is retained only in process memory by design.  ``repr`` and
    ``str`` expose an opaque hash/version marker, which is safe for diagnostics.
    """

    __slots__ = ("_value", "version")

    def __init__(self, value: str | bytes, version: int = 1) -> None:
        raw = _canonical_capability_bytes(value)
        if len(raw) < 32:
            raise WebhookCapabilityError("capability_too_short")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise WebhookCapabilityError("invalid_capability_version")
        self._value = raw.decode("ascii")
        self.version = version

    @property
    def value(self) -> str:
        """Return the token for the narrowly scoped router configuration."""

        return self._value

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self._value.encode("ascii")).hexdigest()

    def matches(self, path: str) -> bool:
        try:
            return capability_matches(_path_capability(path), self._value)
        except WebhookCapabilityError:
            return False

    def __repr__(self) -> str:
        return f"WebhookCapability(version={self.version}, fingerprint={self.fingerprint[:12]})"

    def __str__(self) -> str:
        return f"<webhook-capability-v{self.version}:{self.fingerprint[:12]}>"


@dataclass(frozen=True, slots=True)
class CapabilityMatch:
    matched: bool
    version: int | None = None


class WebhookCapabilitySet:
    """Rotation helper with a maximum ten-minute old/new overlap."""

    def __init__(
        self,
        current: WebhookCapability | str,
        *,
        version: int = 1,
        now: float | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.current = (
            current
            if isinstance(current, WebhookCapability)
            else WebhookCapability(current, version)
        )
        self.previous: WebhookCapability | None = None
        self.previous_expires_at: float | None = None
        self._rotated_at = now

    @property
    def version(self) -> int:
        return self.current.version

    def match(self, path: str, *, now: float | None = None) -> CapabilityMatch:
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            current_match = self.current.matches(path)
            previous = self.previous
            previous_match = bool(
                previous
                and (
                    self.previous_expires_at is None
                    or timestamp <= self.previous_expires_at
                )
                and previous.matches(path)
            )
            if current_match:
                return CapabilityMatch(True, self.current.version)
            if previous_match and previous is not None:
                return CapabilityMatch(True, previous.version)
            return CapabilityMatch(False, None)

    def matches(self, path: str, *, now: float | None = None) -> bool:
        return self.match(path, now=now).matched

    def rotate(
        self,
        replacement: WebhookCapability | str | None = None,
        *,
        now: float | None = None,
        overlap_seconds: float = CAPABILITY_OVERLAP_SECONDS,
    ) -> WebhookCapability:
        if isinstance(overlap_seconds, bool) or not isinstance(
            overlap_seconds, (int, float)
        ):
            raise WebhookValidationError("invalid_capability_overlap")
        if not 0 <= float(overlap_seconds) <= CAPABILITY_OVERLAP_SECONDS:
            raise WebhookValidationError("invalid_capability_overlap")
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            self.previous = self.current
            self.previous_expires_at = timestamp + float(overlap_seconds)
            next_version = self.current.version + 1
            self.current = (
                replacement
                if isinstance(replacement, WebhookCapability)
                else WebhookCapability(
                    generate_capability() if replacement is None else replacement,
                    next_version,
                )
            )
            self._rotated_at = timestamp
            return self.current

    def revoke_previous(self) -> None:
        with self._lock:
            self.previous = None
            self.previous_expires_at = None


@dataclass(frozen=True, slots=True)
class NormalizedPlexEvent:
    """The bounded fields safe to place in an event-inbox row.

    ``media_type`` can be ``show``/``season`` for a hint.  Hints are retained
    by :func:`normalize_plex_payload` for a resolver, while
    :func:`parse_plex_webhook` returns only movie/episode notification units.
    No raw payload, filename, poster bytes, token, or private URL is stored.
    """

    event_type: Literal["library.new"]
    server_uuid: str
    machine_identifier: str | None
    library_uuid: str
    library_name: str | None
    rating_key: str
    media_type: str
    title: str
    year: int | None = None
    season_number: int | None = None
    episode_number: int | None = None
    parent_rating_key: str | None = None
    grandparent_rating_key: str | None = None
    guid: str | None = None
    parent_guid: str | None = None
    grandparent_guid: str | None = None
    added_at: datetime | None = None
    poster_size: int = 0
    poster_sha256: str | None = None
    source: str = "plex_webhook"
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.event_type != _EVENT_TYPE:
            raise WebhookValidationError("invalid_event_type")
        server = _safe_identifier(
            self.server_uuid, "server_uuid", maximum=128, required=True
        )
        if server is None or not _SERVER_UUID_RE.fullmatch(server):
            raise WebhookValidationError("invalid_server_uuid")
        object.__setattr__(self, "server_uuid", server)
        object.__setattr__(
            self,
            "machine_identifier",
            _safe_identifier(
                self.machine_identifier, "machine_identifier", maximum=256
            ),
        )
        library = _safe_identifier(self.library_uuid, "library_uuid", required=True)
        if library is None:
            raise WebhookValidationError("missing_library_uuid")
        object.__setattr__(self, "library_uuid", library)
        object.__setattr__(
            self,
            "library_name",
            _safe_text(self.library_name, "library_name", maximum=256),
        )
        object.__setattr__(self, "rating_key", canonical_rating_key(self.rating_key))
        if self.media_type not in _ALLOWED_TYPES:
            raise WebhookValidationError("invalid_media_type")
        object.__setattr__(
            self, "title", _safe_text(self.title, "title", maximum=1024, required=True)
        )
        if isinstance(self.year, bool) or (
            self.year is not None and not isinstance(self.year, int)
        ):
            raise WebhookValidationError("invalid_year")
        if self.year is not None and not 1800 <= self.year <= 3000:
            raise WebhookValidationError("invalid_year")
        for field_name in (
            "season_number",
            "episode_number",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise WebhookValidationError(f"invalid_{field_name}")
        if self.media_type == "episode" and (
            self.season_number is None or self.episode_number is None
        ):
            raise WebhookValidationError("episode_scope_missing")
        for field_name in (
            "parent_rating_key",
            "grandparent_rating_key",
        ):
            value = getattr(self, field_name)
            object.__setattr__(
                self,
                field_name,
                None if value is None else canonical_rating_key(value),
            )
        for field_name in ("guid", "parent_guid", "grandparent_guid"):
            object.__setattr__(
                self,
                field_name,
                _safe_text(getattr(self, field_name), field_name, maximum=1024),
            )
        if (
            isinstance(self.poster_size, bool)
            or not isinstance(self.poster_size, int)
            or not 0 <= self.poster_size <= MAX_IMAGE_PART_BYTES
        ):
            raise WebhookValidationError("invalid_poster_size")
        if self.poster_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.poster_sha256
        ):
            raise WebhookValidationError("invalid_poster_hash")
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
        ):
            raise WebhookValidationError("observed_at_must_be_timezone_aware")

    @property
    def event(self) -> str:
        return self.event_type

    @property
    def library_key(self) -> str:
        return self.library_uuid

    @property
    def is_unit(self) -> bool:
        return self.media_type in _UNIT_TYPES

    @property
    def is_hint(self) -> bool:
        return self.media_type in _HINT_TYPES

    @property
    def key(self) -> str:
        """Stable pre-tombstone dedupe key for the inbox."""

        return structured_plex_event_key(
            self.server_uuid,
            self.library_uuid,
            self.rating_key,
        )

    def event_key(self, tombstone_generation: int = 0) -> str:
        return structured_plex_event_key(
            self.server_uuid,
            self.library_uuid,
            self.rating_key,
            tombstone_generation,
        )

    @property
    def payload_hash(self) -> str:
        return hashlib.sha256(self.sanitized_json().encode("utf-8")).hexdigest()

    def sanitized_dict(self) -> dict[str, Any]:
        """Return only fields approved for durable inbox persistence."""

        result: dict[str, Any] = {
            "event": self.event_type,
            "server_uuid": self.server_uuid,
            "machine_identifier": self.machine_identifier,
            "library_uuid": self.library_uuid,
            "library_name": self.library_name,
            "rating_key": self.rating_key,
            "media_type": self.media_type,
            "title": self.title,
            "year": self.year,
            "season_number": self.season_number,
            "episode_number": self.episode_number,
            "parent_rating_key": self.parent_rating_key,
            "grandparent_rating_key": self.grandparent_rating_key,
            "guid": self.guid,
            "parent_guid": self.parent_guid,
            "grandparent_guid": self.grandparent_guid,
            "added_at": self.added_at.isoformat().replace("+00:00", "Z")
            if self.added_at is not None
            else None,
            "poster_size": self.poster_size,
            "poster_sha256": self.poster_sha256,
            "source": self.source,
        }
        return result

    def sanitized_json(self) -> str:
        return json.dumps(
            self.sanitized_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    # The migration calls this field ``sanitized_payload_json``.  This alias
    # keeps adapters explicit and avoids ever exposing a raw webhook mapping.
    def to_record(self, tombstone_generation: int = 0) -> dict[str, Any]:
        return {
            "event_key": self.event_key(tombstone_generation),
            "source": self.source,
            "event_type": self.event_type,
            "server_uuid": self.server_uuid,
            "library_uuid": self.library_uuid,
            "rating_key": self.rating_key,
            "payload_hash": self.payload_hash,
            "sanitized_payload_json": self.sanitized_json(),
        }

    def __repr__(self) -> str:
        return (
            "NormalizedPlexEvent("
            f"event_type={self.event_type!r}, server_uuid={self.server_uuid!r}, "
            f"library_uuid={self.library_uuid!r}, rating_key={self.rating_key!r}, "
            f"media_type={self.media_type!r})"
        )


PlexWebhookEvent = NormalizedPlexEvent
SafePlexEvent = NormalizedPlexEvent


@dataclass(frozen=True, slots=True)
class ParsedMultipart:
    """Bounded multipart result with no raw upload bytes."""

    payload: Mapping[str, Any]
    poster_size: int = 0
    poster_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class WebhookParseResult:
    event: NormalizedPlexEvent | None
    disposition: Literal["accepted", "ignored"]
    reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.disposition == "accepted"

    @property
    def http_status(self) -> int:
        """HTTP status an adapter should use after its inbox transaction."""

        return 202 if self.accepted else 204


def _reject_json_constant(value: str) -> Any:
    del value
    raise WebhookValidationError("invalid_json_number")


def _reject_duplicate_json_key(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WebhookValidationError("duplicate_json_key")
        result[key] = value
    return result


def parse_json_part(
    value: bytes | str, *, maximum_bytes: int = MAX_JSON_PART_BYTES
) -> Mapping[str, Any]:
    """Decode one bounded JSON part with strict duplicate/number handling."""

    if isinstance(value, str):
        try:
            raw = value.encode("utf-8", "strict")
        except UnicodeError as exc:
            raise WebhookValidationError("invalid_json_encoding") from exc
    elif isinstance(value, bytes):
        raw = value
    else:
        raise WebhookValidationError("invalid_json_part")
    if len(raw) > maximum_bytes:
        raise WebhookLimitError("json_part_too_large")
    try:
        decoded = raw.decode("utf-8", "strict")
        parsed = json.loads(
            decoded,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_json_key,
        )
    except WebhookValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookValidationError("invalid_json") from exc
    if not isinstance(parsed, Mapping):
        raise WebhookValidationError("json_root_must_be_object")
    return parsed


def _content_disposition_name(part: Any) -> str | None:
    header = part.get("Content-Disposition")
    if not isinstance(header, str):
        return None
    # ``Message.get_param`` handles quoted values and RFC parameter syntax.
    name = part.get_param("name", header="Content-Disposition")
    return name if isinstance(name, str) else None


def parse_multipart(
    body: bytes,
    content_type: str,
    *,
    content_encoding: str | None = None,
    deadline: float | None = None,
) -> ParsedMultipart:
    """Parse a bounded Plex multipart body without retaining upload bytes."""

    _deadline_check(deadline)
    if not isinstance(body, bytes):
        raise WebhookValidationError("body_must_be_bytes")
    if len(body) > MAX_BODY_BYTES:
        raise WebhookLimitError("body_too_large")
    if content_encoding is not None and (
        not isinstance(content_encoding, str)
        or content_encoding.strip().lower() not in {"", "identity"}
    ):
        raise WebhookContentTypeError("compressed_body_not_supported")
    if not isinstance(content_type, str):
        raise WebhookContentTypeError("missing_content_type")
    if any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in content_type
    ):
        raise WebhookContentTypeError("invalid_content_type")
    # Let the MIME parser validate the boundary while requiring the outer type
    # to be multipart/form-data.  No caller-supplied filename is ever used.
    outer = content_type.split(";", 1)[0].strip().lower()
    if outer != "multipart/form-data":
        raise WebhookContentTypeError("multipart_required")
    try:
        header = (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode(
            "ascii", "strict"
        )
    except UnicodeEncodeError as exc:
        raise WebhookContentTypeError("invalid_content_type") from exc
    _deadline_check(deadline)
    try:
        message = BytesParser(policy=policy.compat32).parsebytes(header + body)
    except Exception as exc:  # email parser has implementation-specific errors
        raise WebhookValidationError("invalid_multipart") from exc
    _deadline_check(deadline)
    if not message.is_multipart():
        raise WebhookContentTypeError("multipart_required")
    if message.defects or any(part.defects for part in message.walk()):
        raise WebhookValidationError("invalid_multipart")
    # ``policy.compat32`` returns the portable ``Message`` type on Python
    # versions supported by the companion; ``walk`` works for both that type
    # and the newer ``EmailMessage`` implementation.
    walked_parts = [part for part in message.walk() if part is not message]
    if len(walked_parts) > MAX_PARTS:
        raise WebhookLimitError("too_many_parts")
    parts = [part for part in walked_parts if not part.is_multipart()]
    if not parts:
        raise WebhookValidationError("missing_payload_part")

    payload_bytes: bytes | None = None
    poster_size = 0
    poster_seen = False
    poster_hash: str | None = None
    for part in parts:
        _deadline_check(deadline)
        name = _content_disposition_name(part)
        if name not in {"payload", "thumb"}:
            raise WebhookValidationError("unexpected_part")
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes):
            raise WebhookValidationError("invalid_part_encoding")
        if name == "payload":
            if payload_bytes is not None:
                raise WebhookValidationError("duplicate_payload_part")
            if len(data) > MAX_JSON_PART_BYTES:
                raise WebhookLimitError("json_part_too_large")
            payload_bytes = data
        else:
            if poster_seen:
                raise WebhookValidationError("duplicate_poster_part")
            if len(data) > MAX_IMAGE_PART_BYTES:
                raise WebhookLimitError("image_part_too_large")
            content = part.get_content_type().lower()
            if content != "image/jpeg":
                raise WebhookValidationError("invalid_poster_type")
            poster_size = len(data)
            poster_hash = hashlib.sha256(data).hexdigest()
            poster_seen = True
        # Release the local reference as soon as the bounded digest is made;
        # the returned object never contains poster bytes.
        del data
    if payload_bytes is None:
        raise WebhookValidationError("missing_payload_part")
    payload = parse_json_part(payload_bytes)
    return ParsedMultipart(payload, poster_size, poster_hash)


parse_multipart_webhook = parse_multipart


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WebhookValidationError(f"invalid_{field_name}")
    return value


def _first(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _non_negative_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WebhookValidationError(f"invalid_{field_name}")
    return value


def _plex_timestamp(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise WebhookValidationError(f"invalid_{field_name}")
        return value.astimezone(timezone.utc)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WebhookValidationError(f"invalid_{field_name}")
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise WebhookValidationError(f"invalid_{field_name}")
    try:
        return datetime.fromtimestamp(float(value), timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise WebhookValidationError(f"invalid_{field_name}") from exc


def _allowed_library(
    metadata: Mapping[str, Any],
    *,
    allowed_library_ids: Iterable[str] | None,
    allowed_library_names: Iterable[str] | None,
) -> str:
    raw_id = _first(
        metadata, "librarySectionUUID", "librarySectionId", "librarySectionID"
    )
    if isinstance(raw_id, bool) or raw_id is None:
        library_id = None
    elif isinstance(raw_id, int):
        if raw_id <= 0:
            raise WebhookValidationError("invalid_library_id")
        library_id = str(raw_id)
    else:
        library_id = _safe_identifier(raw_id, "library_uuid")
    library_name = _safe_text(
        _first(metadata, "librarySectionTitle", "librarySectionName"),
        "library_name",
        maximum=256,
    )
    ids = {
        str(value).strip()
        for value in (allowed_library_ids or ())
        if str(value).strip()
    }
    names = {
        str(value).strip()
        for value in (allowed_library_names or ())
        if str(value).strip()
    }
    if not ids and not names:
        raise WebhookValidationError("library_allowlist_missing")
    if library_id is not None and library_id in ids:
        return library_id
    if library_name is not None and library_name in names:
        return library_id or library_name
    raise WebhookValidationError("library_not_allowed")


def normalize_plex_payload(
    payload: Mapping[str, Any],
    *,
    expected_server_uuid: str | None = None,
    allowed_server_uuids: Iterable[str] | None = None,
    allowed_library_ids: Iterable[str] | None = None,
    allowed_library_names: Iterable[str] | None = None,
    poster_size: int = 0,
    poster_sha256: str | None = None,
    observed_at: datetime | None = None,
    include_hints: bool = True,
) -> NormalizedPlexEvent | None:
    """Validate and normalize a decoded Plex webhook payload.

    ``None`` means a syntactically valid but irrelevant event (for example a
    music import, another configured server, or a disallowed library).  Shape
    violations raise a generic ``WebhookValidationError`` so the HTTP adapter
    can return a non-acknowledging 4xx.
    """

    root = _mapping(payload, "payload")
    event_type = _safe_text(_first(root, "event"), "event", maximum=64, required=True)
    if event_type != _EVENT_TYPE:
        return None
    server = _mapping(root.get("server"), "server")
    server_uuid = _safe_identifier(
        _first(server, "uuid", "UUID"), "server_uuid", maximum=128, required=True
    )
    if server_uuid is None:
        raise WebhookValidationError("missing_server_uuid")
    allowed_servers = {
        str(value).strip()
        for value in (allowed_server_uuids or ())
        if str(value).strip()
    }
    if expected_server_uuid is not None:
        allowed_servers.add(str(expected_server_uuid).strip())
    if not allowed_servers:
        raise WebhookValidationError("server_allowlist_missing")
    if server_uuid not in allowed_servers:
        return None
    machine_identifier = _safe_identifier(
        _first(server, "machineIdentifier", "machine_identifier"),
        "machine_identifier",
        maximum=256,
    )
    metadata = _mapping(root.get("metadata"), "metadata")
    media_type = _safe_text(
        _first(metadata, "type", "mediaType"), "media_type", maximum=32, required=True
    )
    if media_type not in _ALLOWED_TYPES:
        return None
    if media_type in _HINT_TYPES and not include_hints:
        return None
    library_uuid = _allowed_library(
        metadata,
        allowed_library_ids=allowed_library_ids,
        allowed_library_names=allowed_library_names,
    )
    rating_key = canonical_rating_key(_first(metadata, "ratingKey", "rating_key"))
    title = _safe_text(_first(metadata, "title"), "title", maximum=1024, required=True)
    if title is None:
        raise WebhookValidationError("missing_title")
    year_value = _first(metadata, "year")
    if year_value is not None and (
        isinstance(year_value, bool)
        or not isinstance(year_value, int)
        or not 1800 <= year_value <= 3000
    ):
        raise WebhookValidationError("invalid_year")
    season_number = _non_negative_int(
        _first(metadata, "parentIndex", "seasonNumber", "season_number"),
        "season_number",
    )
    episode_number = _non_negative_int(
        _first(metadata, "index", "episodeNumber", "episode_number"), "episode_number"
    )
    if media_type == "episode" and (season_number is None or episode_number is None):
        raise WebhookValidationError("episode_scope_missing")
    parent_rating_key = _first(metadata, "parentRatingKey", "parent_rating_key")
    grandparent_rating_key = _first(
        metadata, "grandparentRatingKey", "grandparent_rating_key"
    )
    if parent_rating_key is not None:
        parent_rating_key = canonical_rating_key(parent_rating_key)
    if grandparent_rating_key is not None:
        grandparent_rating_key = canonical_rating_key(grandparent_rating_key)
    observed = observed_at or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise WebhookValidationError("observed_at_must_be_timezone_aware")
    added_at = _plex_timestamp(_first(metadata, "addedAt", "added_at"), "added_at")
    return NormalizedPlexEvent(
        event_type="library.new",
        server_uuid=server_uuid,
        machine_identifier=machine_identifier,
        library_uuid=library_uuid,
        library_name=_safe_text(
            _first(metadata, "librarySectionTitle", "librarySectionName"),
            "library_name",
            maximum=256,
        ),
        rating_key=rating_key,
        media_type=media_type,
        title=title,
        year=year_value,
        season_number=season_number,
        episode_number=episode_number,
        parent_rating_key=parent_rating_key,
        grandparent_rating_key=grandparent_rating_key,
        guid=_safe_text(_first(metadata, "guid"), "guid", maximum=1024),
        parent_guid=_safe_text(
            _first(metadata, "parentGuid"), "parent_guid", maximum=1024
        ),
        grandparent_guid=_safe_text(
            _first(metadata, "grandparentGuid"), "grandparent_guid", maximum=1024
        ),
        added_at=added_at,
        poster_size=poster_size,
        poster_sha256=poster_sha256,
        observed_at=observed,
    )


normalize_payload = normalize_plex_payload
normalize_event = normalize_plex_payload


def parse_plex_webhook(
    body: bytes,
    content_type: str,
    *,
    request_path: str | None = None,
    capability: str | bytes | WebhookCapabilitySet | WebhookCapability | None = None,
    expected_capability: str | bytes | WebhookCapability | None = None,
    content_encoding: str | None = None,
    expected_server_uuid: str | None = None,
    allowed_server_uuids: Iterable[str] | None = None,
    allowed_library_ids: Iterable[str] | None = None,
    allowed_library_names: Iterable[str] | None = None,
    observed_at: datetime | None = None,
    deadline_seconds: float = BODY_PARSE_DEADLINE_SECONDS,
) -> NormalizedPlexEvent | None:
    """Validate capability and parse a Plex webhook into one safe unit.

    Capability verification happens before MIME parsing.  Callers may pass a
    ``WebhookCapabilitySet`` for rotation or ``capability``/``expected_capability``
    as the current secret pair.  Hints and irrelevant events return ``None``;
    use :func:`parse_plex_webhook_result` when the HTTP layer needs a 204 reason.
    """

    deadline_value = _validated_deadline_seconds(deadline_seconds)
    started = time.monotonic()
    deadline = started + deadline_value
    if request_path is None:
        raise WebhookCapabilityError("missing_capability_path")
    if isinstance(capability, (WebhookCapabilitySet, WebhookCapability)):
        if not capability.matches(request_path):
            raise WebhookCapabilityError("invalid_capability")
    else:
        expected = (
            expected_capability if expected_capability is not None else capability
        )
        if expected is None:
            raise WebhookCapabilityError("missing_capability")
        if isinstance(expected, WebhookCapability):
            if not expected.matches(request_path):
                raise WebhookCapabilityError("invalid_capability")
        else:
            validate_capability(request_path, expected)
    parsed = parse_multipart(
        body,
        content_type,
        content_encoding=content_encoding,
        deadline=deadline,
    )
    _deadline_check(deadline)
    return normalize_plex_payload(
        parsed.payload,
        expected_server_uuid=expected_server_uuid,
        allowed_server_uuids=allowed_server_uuids,
        allowed_library_ids=allowed_library_ids,
        allowed_library_names=allowed_library_names,
        poster_size=parsed.poster_size,
        poster_sha256=parsed.poster_sha256,
        observed_at=observed_at,
        include_hints=False,
    )


def parse_plex_webhook_result(
    body: bytes,
    content_type: str,
    **kwargs: Any,
) -> WebhookParseResult:
    """Return an explicit accepted/irrelevant result for an HTTP adapter."""

    event = parse_plex_webhook(body, content_type, **kwargs)
    if event is None:
        return WebhookParseResult(None, "ignored", "irrelevant_event")
    return WebhookParseResult(event, "accepted", None)


def _validated_deadline_seconds(value: object) -> float:
    """Validate one finite body/parse deadline before using it arithmetically."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WebhookLimitError("invalid_parse_deadline")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as exc:
        raise WebhookLimitError("invalid_parse_deadline") from exc
    if not math.isfinite(parsed) or not 0 < parsed <= BODY_PARSE_DEADLINE_SECONDS:
        raise WebhookLimitError("invalid_parse_deadline")
    return parsed


def read_bounded_body(
    stream: BinaryIO,
    *,
    content_length: int | None = None,
    maximum_bytes: int = MAX_BODY_BYTES,
    deadline_seconds: float = BODY_PARSE_DEADLINE_SECONDS,
    chunk_size: int = 64 * 1024,
) -> bytes:
    """Read a chunked request within the same byte/time budget as multipart."""

    if isinstance(content_length, bool) or (
        content_length is not None
        and (not isinstance(content_length, int) or content_length < 0)
    ):
        raise WebhookValidationError("invalid_content_length")
    if content_length is not None and content_length > maximum_bytes:
        raise WebhookLimitError("body_too_large")
    if (
        isinstance(chunk_size, bool)
        or not isinstance(chunk_size, int)
        or not 0 < chunk_size <= maximum_bytes
    ):
        raise WebhookLimitError("invalid_chunk_size")
    deadline = time.monotonic() + _validated_deadline_seconds(deadline_seconds)
    chunks: list[bytes] = []
    total = 0
    if content_length == 0:
        return b""
    while True:
        _deadline_check(deadline)
        try:
            remaining = maximum_bytes - total + 1
            if content_length is not None:
                remaining = min(remaining, content_length - total + 1)
            chunk = stream.read(min(chunk_size, remaining))
        except Exception as exc:
            raise WebhookValidationError("body_read_failed") from exc
        if not isinstance(chunk, bytes):
            raise WebhookValidationError("body_read_failed")
        if not chunk:
            break
        total += len(chunk)
        if total > maximum_bytes:
            raise WebhookLimitError("body_too_large")
        chunks.append(chunk)
        if content_length is not None and total >= content_length:
            # A body with trailing bytes is still rejected by the hard byte
            # bound; do not read unboundedly merely to find EOF.
            if total != content_length:
                raise WebhookValidationError("content_length_mismatch")
            break
    if content_length is not None and total != content_length:
        raise WebhookValidationError("content_length_mismatch")
    return b"".join(chunks)


class WebhookRateLimiter:
    """Thread-safe token bucket for one loopback capability."""

    def __init__(
        self,
        *,
        rate_per_minute: int = RATE_LIMIT_PER_MINUTE,
        burst: int = RATE_LIMIT_BURST,
        clock: Any = time.monotonic,
    ) -> None:
        if (
            isinstance(rate_per_minute, bool)
            or not isinstance(rate_per_minute, int)
            or rate_per_minute <= 0
        ):
            raise ValueError("rate_per_minute must be positive")
        if isinstance(burst, bool) or not isinstance(burst, int) or burst <= 0:
            raise ValueError("burst must be positive")
        self.rate_per_second = rate_per_minute / 60.0
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._last = float(clock())
        self._clock = clock
        self._lock = threading.Lock()

    def allow(self, *, now: float | None = None, tokens: int = 1) -> bool:
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            return False
        timestamp = float(self._clock() if now is None else now)
        with self._lock:
            elapsed = max(0.0, timestamp - self._last)
            self._tokens = min(
                self.capacity, self._tokens + elapsed * self.rate_per_second
            )
            self._last = timestamp
            if self._tokens < tokens:
                return False
            self._tokens -= tokens
            return True

    consume = allow
    check = allow

    @property
    def available_tokens(self) -> int:
        with self._lock:
            return max(0, int(self._tokens))


__all__ = [
    "BODY_PARSE_DEADLINE_SECONDS",
    "CAPABILITY_BYTES",
    "CAPABILITY_OVERLAP_SECONDS",
    "MAX_BODY_BYTES",
    "MAX_IMAGE_PART_BYTES",
    "MAX_JSON_PART_BYTES",
    "MAX_PARTS",
    "PLEX_WEBHOOK_BODY_DEADLINE_SECONDS",
    "PLEX_WEBHOOK_MAX_BODY_BYTES",
    "PLEX_WEBHOOK_MAX_IMAGE_BYTES",
    "PLEX_WEBHOOK_MAX_JSON_BYTES",
    "PLEX_WEBHOOK_MAX_PARTS",
    "PLEX_WEBHOOK_RATE_LIMIT_BURST",
    "PLEX_WEBHOOK_RATE_LIMIT_PER_MINUTE",
    "CapabilityMatch",
    "InvalidWebhookError",
    "NormalizedPlexEvent",
    "ParsedMultipart",
    "PlexIngressError",
    "PlexWebhookError",
    "PlexWebhookEvent",
    "PlexWebhookValidationError",
    "SafePlexEvent",
    "WebhookCapability",
    "WebhookCapabilityError",
    "WebhookCapabilitySet",
    "WebhookContentTypeError",
    "WebhookLimitError",
    "WebhookParseResult",
    "WebhookPersistenceError",
    "WebhookRateLimiter",
    "WebhookValidationError",
    "build_metadata_path",
    "canonical_metadata_path",
    "canonical_rating_key",
    "capability_matches",
    "constant_time_capability_match",
    "constant_time_compare",
    "generate_capability",
    "generate_webhook_capability",
    "metadata_path",
    "new_webhook_capability",
    "normalize_event",
    "normalize_payload",
    "normalize_plex_payload",
    "parse_json_part",
    "parse_multipart",
    "parse_multipart_webhook",
    "parse_plex_webhook",
    "parse_plex_webhook_result",
    "read_bounded_body",
    "structured_plex_event_key",
    "validate_capability",
]
