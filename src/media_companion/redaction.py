"""Bounded, provider-safe redaction helpers.

The upstream MCP is an operator-facing dependency, not a safe data boundary.
Provider responses and exception text can contain API keys, bearer tokens,
filesystem paths, and URLs that are useful to the provider but not to a
caller of the companion.  This module is intentionally small and transport
agnostic so every adapter can apply the same last-resort scrubbing before a
value is put in a typed result or an error message.

Redaction is not an output schema.  Callers must still select the fields that
belong in their public result.  The functions here only ensure that a selected
field cannot carry an obvious credential, path, or provider URL through.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final, TypeAlias

REDACTED: Final[str] = "<redacted>"
REDACTED_URL: Final[str] = "<redacted-url>"
REDACTED_PATH: Final[str] = "<redacted-path>"
REDACTED_BYTES: Final[str] = "<redacted-bytes>"
MAX_REDACTED_TEXT_BYTES: Final[int] = 16 * 1024
MAX_REDACTION_DEPTH: Final[int] = 32
MAX_REDACTION_ITEMS: Final[int] = 10_000


class RedactionError(ValueError):
    """Raised when a value cannot be safely bounded and redacted."""


JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


# Keys are deliberately matched by meaning rather than by one provider's
# exact spelling.  A response key named ``apiKey`` or ``X-Plex-Token`` must
# receive the same treatment as ``api_key``.
_SECRET_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[_\-.])(?:api[_\-.]?(?:key|token)|access[_\-.]?token|auth(?:orization)?|"
    r"bearer|bot[_\-.]?token|client[_\-.]?secret|credential|cookie|"
    r"password|plex[_\-.]?token|refresh[_\-.]?token|secret|sign(?:ed)?[_\-.]?"
    r"assertion|session[_\-.]?token|telegram[_\-.]?token|webhook[_\-.]?"
    r"(?:capability|secret)|token|x[_\-.]?api[_\-.]?key|x[_\-.]?plex[_\-.]?token)"
    r"(?:$|[_\-.])",
    re.IGNORECASE,
)
_PATH_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[_\-.])(?:file|filename|filepath|file_path|path|root|rootfolder|"
    r"root_folder|directory|folder|download[_\-.]?path|library[_\-.]?path)"
    r"(?:$|[_\-.])",
    re.IGNORECASE,
)
_URL_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[_\-.])(?:url|uri|href|link|location|endpoint|poster|image[_\-.]?url|"
    r"download[_\-.]?url|stream[_\-.]?url)(?:$|[_\-.])",
    re.IGNORECASE,
)

# Values in free-form messages are scrubbed too.  These expressions are
# intentionally conservative: they target credential syntax and absolute
# provider paths, not ordinary title text containing punctuation.
_BEARER_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\b(?:bearer|basic)\s+[^\s,;]+")
_BOT_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_KEY_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?P<prefix>^|[^A-Za-z0-9])"
    r"(?P<key>[A-Za-z][A-Za-z0-9_.-]*)"
    r"(?P<separator>\s*[:=]\s*)(?P<value>[^\s,;]+)"
)
_QUOTED_KEY_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?P<key_quote>[\"'])"
    r"(?P<key>[A-Za-z][A-Za-z0-9_.-]*)(?P=key_quote)"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<value_quote>[\"'])(?:\\.|[^\\])*?(?P=value_quote)"
)
_URL_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>\"']+")
_FILE_URI_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\bfile://[^\s<>\"']+")
_MAGNET_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\bmagnet:\?[^\s<>\"']+")
_WINDOWS_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\s<>\"']+"
)
_UNIX_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9])/(?:opt|etc|run|var|tmp|home|mnt|media|srv|volume[^/\s]*|"
    r"downloads?|data|config|secrets?)(?:/[^\s<>\"']*)*"
)
_ABSOLUTE_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9])/(?:[^/\s<>\"']+/)+[^\s<>\"']*"
)
# A compact JWT-like value is almost always an assertion or token in an
# exception/log field.  Requiring three segments avoids replacing ordinary
# dotted identifiers.
_JWT_RE: Final[re.Pattern[str]] = re.compile(
    r"\bey[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)


def _key_text(key: object) -> str:
    return key if isinstance(key, str) else str(key)


def _normalized_key(key: object) -> str:
    """Normalize snake, kebab, and camel-case response keys for matching."""

    text = _key_text(key).strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return text.lower()


def is_secret_key(key: object) -> bool:
    """Return whether a mapping key denotes credential/assertion material."""

    text = _normalized_key(key)
    return bool(text and _SECRET_KEY_RE.search(text))


def _is_text_secret_assignment_key(key: object) -> bool:
    """Match credential assignments without treating every ``key`` field as secret.

    ``key`` is a useful, non-secret identifier in structured provider objects,
    so it is intentionally not part of :func:`is_secret_key`.  In free-form
    assignment syntax, however, ``key=...`` is overwhelmingly credential
    material and must be scrubbed alongside ``token=...``.
    """

    normalized = _normalized_key(key)
    return normalized in {"key", "token"} or is_secret_key(key)


def is_path_key(key: object) -> bool:
    """Return whether a mapping key denotes a local/provider filesystem path."""

    text = _normalized_key(key)
    return bool(text and _PATH_KEY_RE.search(text))


def is_url_key(key: object) -> bool:
    """Return whether a mapping key denotes a provider URL or URI."""

    text = _normalized_key(key)
    return bool(text and _URL_KEY_RE.search(text))


def _bounded_text(value: str, *, max_bytes: int) -> str:
    """Bound UTF-8 text without ever splitting a code point."""

    encoded = value.encode("utf-8", "strict")
    if len(encoded) <= max_bytes:
        return value
    # Keep the prefix useful for diagnostics but make truncation explicit.
    marker = "…" if max_bytes >= len("…".encode()) else "."
    marker_bytes = len(marker.encode("utf-8"))
    prefix = encoded[: max(0, max_bytes - marker_bytes)].decode("utf-8", "ignore")
    return prefix + marker


def redact_text(value: str, *, max_bytes: int = MAX_REDACTED_TEXT_BYTES) -> str:
    """Scrub credentials, URLs, and provider paths from free-form text.

    The returned string is bounded.  It is safe for an exception summary or a
    typed ``text`` content item, but it is not a substitute for a field
    allowlist.
    """

    if not isinstance(value, str):
        raise TypeError("redact_text expects a string")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    # Replace the most specific forms first.  A URL can contain ``token=``;
    # replacing the whole URL prevents a later pass from leaving its host or
    # query string behind.
    text = _FILE_URI_RE.sub(REDACTED_URL, value)
    text = _MAGNET_RE.sub(REDACTED_URL, text)
    text = _URL_RE.sub(REDACTED_URL, text)
    text = _BEARER_RE.sub("<redacted-auth>", text)
    text = _BOT_TOKEN_RE.sub(REDACTED, text)
    text = _JWT_RE.sub(REDACTED, text)

    def redact_quoted_assignment(match: re.Match[str]) -> str:
        if not _is_text_secret_assignment_key(match.group("key")):
            return match.group(0)
        return (
            match.group("key_quote")
            + match.group("key")
            + match.group("key_quote")
            + match.group("separator")
            + match.group("value_quote")
            + REDACTED
            + match.group("value_quote")
        )

    def redact_assignment(match: re.Match[str]) -> str:
        if not _is_text_secret_assignment_key(match.group("key")):
            return match.group(0)
        return (
            match.group("prefix")
            + match.group("key")
            + match.group("separator")
            + REDACTED
        )

    text = _QUOTED_KEY_ASSIGNMENT_RE.sub(redact_quoted_assignment, text)
    text = _KEY_ASSIGNMENT_RE.sub(redact_assignment, text)
    text = _WINDOWS_PATH_RE.sub(REDACTED_PATH, text)
    text = _UNIX_PATH_RE.sub(REDACTED_PATH, text)
    text = _ABSOLUTE_PATH_RE.sub(REDACTED_PATH, text)

    # Control characters can create log lines or terminal escapes.  Preserve
    # ordinary whitespace while replacing the rest with a visible space.
    text = "".join(
        character if character in "\t\n\r" or ord(character) >= 0x20 else " "
        for character in text
    )
    return _bounded_text(text, max_bytes=max_bytes)


def _json_safe_scalar(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float)):
        return value  # type: ignore[return-value]
    if isinstance(value, str):
        return redact_text(value)
    return REDACTED


def redact_value(
    value: object,
    *,
    depth: int = 0,
    item_count: list[int] | None = None,
) -> JsonValue:
    """Recursively scrub a JSON-like value while keeping it JSON serializable.

    Sensitive/path/URL-keyed fields are retained with a sentinel rather than
    deleted.  Retaining the key makes it possible for an operator to see that
    a response was intentionally sanitized without leaking the value.
    Unknown object types, bytes, and over-bounded containers become a sentinel.
    """

    count = [0] if item_count is None else item_count
    if depth > MAX_REDACTION_DEPTH:
        return REDACTED
    count[0] += 1
    if count[0] > MAX_REDACTION_ITEMS:
        return REDACTED

    if isinstance(value, Mapping):
        if len(value) > MAX_REDACTION_ITEMS:
            return REDACTED
        result: dict[str, JsonValue] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                continue
            key = _bounded_text(raw_key, max_bytes=512)
            if is_secret_key(key):
                result[key] = REDACTED
            elif is_path_key(key):
                result[key] = REDACTED_PATH
            elif is_url_key(key):
                result[key] = REDACTED_URL
            else:
                result[key] = redact_value(raw_value, depth=depth + 1, item_count=count)
        return result

    # ``str`` is a Sequence, so keep this branch after scalar handling.
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_REDACTION_ITEMS:
            return REDACTED
        return [redact_value(item, depth=depth + 1, item_count=count) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _json_safe_scalar(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return REDACTED_BYTES
    return REDACTED


def redact_json(value: object, *, max_bytes: int | None = None) -> JsonValue:
    """Return a redacted JSON value and optionally enforce its wire size."""

    result = redact_value(value)
    if max_bytes is not None:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("max_bytes must be a positive integer")
        try:
            encoded = json.dumps(
                result,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            # Provider object details can include implementation-specific
            # exception text; never retain that as a chained cause.
            raise RedactionError("redacted value is not JSON serializable") from None
        if len(encoded) > max_bytes:
            raise RedactionError("redacted value exceeds the response bound")
    return result


# Friendly aliases used by adapters and tests.
redact = redact_value
sanitize = redact_json
redact_mapping = redact_value


__all__ = [
    "MAX_REDACTED_TEXT_BYTES",
    "MAX_REDACTION_DEPTH",
    "MAX_REDACTION_ITEMS",
    "REDACTED",
    "REDACTED_BYTES",
    "REDACTED_PATH",
    "REDACTED_URL",
    "RedactionError",
    "is_path_key",
    "is_secret_key",
    "is_url_key",
    "redact",
    "redact_json",
    "redact_mapping",
    "redact_text",
    "redact_value",
    "sanitize",
]
