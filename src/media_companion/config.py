"""Validated, non-secret configuration for the media companion.

Configuration contains references to mounted secret files rather than secret
contents.  This module never opens those files.  Keeping that rule here makes
it difficult for a future startup path to accidentally put a token in a log or
in a model-visible object.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn
from urllib.parse import unquote, urlsplit, urlunsplit

from .errors import (
    ConfigurationError,
    InvalidSecretReferenceError,
    InvalidTimeoutConfigurationError,
    InvalidURLConfigurationError,
    MissingConfigurationError,
)


DEFAULT_CONNECT_TIMEOUT_SECONDS = 3.0
DEFAULT_TOTAL_TIMEOUT_SECONDS = 15.0
DEFAULT_BODY_TIMEOUT_SECONDS = 15.0
MAX_CONNECT_TIMEOUT_SECONDS = 3.0
MAX_TOTAL_TIMEOUT_SECONDS = 15.0
MAX_BODY_TIMEOUT_SECONDS = 15.0
DEFAULT_DATABASE_PATH = "/opt/data/state/media_companion.sqlite3"
DEFAULT_TMDB_URL = "https://api.themoviedb.org/3"

# Deployment uses these names for mounted credentials.  They are deliberately
# paths, not values.  The filenames are documented here for tooling that builds
# a deployment manifest; no path is read or checked for existence at startup.
CANONICAL_SECRET_FILENAMES = {
    "upstream": "upstream.env",
    "companion": "companion.env",
    "actor_signing": "actor-signing.key",
    "dashboard_auth": "dashboard-auth.env",
    "dashboard_api": "dashboard-api.key",
}

_SECRET_SELECTOR = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


def _configuration_error(
    error_type: type[ConfigurationError], field_name: str
) -> NoReturn:
    """Raise a configuration error without including the untrusted value."""

    raise error_type(f"invalid configuration field: {field_name}")


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        _configuration_error(ConfigurationError, field_name)
    assert isinstance(value, str)
    stripped = value.strip()
    if not stripped:
        _configuration_error(ConfigurationError, field_name)
    return stripped


def _canonical_path(raw_path: str, field_name: str) -> Path:
    if (
        not raw_path.startswith("/")
        or any(ord(character) < 0x20 for character in raw_path)
        or "?" in raw_path
        or "#" in raw_path
    ):
        raise InvalidSecretReferenceError(
            f"{field_name} must be an absolute secret-file reference"
        )
    if raw_path.endswith("/"):
        raise InvalidSecretReferenceError(
            f"{field_name} must point to a file, not a directory"
        )

    # Do lexical canonicalization only.  Path.resolve() can inspect the host
    # filesystem and is unnecessary for validating a mounted-file reference.
    normalized = os.path.normpath(raw_path)
    if normalized != raw_path or normalized in {"", "/", ".", ".."}:
        raise InvalidSecretReferenceError(
            f"{field_name} must use a canonical absolute secret-file path"
        )
    return Path(normalized)


@dataclass(frozen=True, slots=True)
class SecretFileRef:
    """A canonical absolute path to a mounted secret file.

    ``SecretFileRef`` intentionally has no ``read`` method.  Consumers that
    need credential material should do so at their narrow I/O boundary.  The
    representation is redacted so accidental exception/log formatting cannot
    reveal a host path or a value loaded from it.
    """

    path: Path
    key: str | None = None

    def __post_init__(self) -> None:
        try:
            raw_path = os.fspath(self.path)
        except TypeError as exc:
            raise InvalidSecretReferenceError(
                "secret-file reference must be a path"
            ) from exc
        if not isinstance(raw_path, str):
            raise InvalidSecretReferenceError("secret-file reference must be a path")
        if raw_path.startswith("file:"):
            parsed = type(self).from_value(
                raw_path, field_name="secret_file", key=self.key
            )
            object.__setattr__(self, "path", parsed.path)
            object.__setattr__(self, "key", parsed.key)
            return
        object.__setattr__(self, "path", _canonical_path(raw_path, "secret_file"))
        if self.key is not None:
            if not isinstance(self.key, str) or not _SECRET_SELECTOR.fullmatch(
                self.key
            ):
                raise InvalidSecretReferenceError(
                    "secret-file selector must be a canonical environment name"
                )

    @classmethod
    def from_value(
        cls,
        value: object,
        *,
        field_name: str = "secret_file",
        key: str | None = None,
    ) -> "SecretFileRef":
        if isinstance(value, cls):
            if key is None or key == value.key:
                return value
            if value.key is not None:
                raise InvalidSecretReferenceError(
                    f"{field_name} has a conflicting secret-file selector"
                )
            return cls(value.path, key=key)
        if not isinstance(value, (str, os.PathLike)):
            raise InvalidSecretReferenceError(
                f"{field_name} must be an absolute secret-file reference"
            )

        text = os.fspath(value)
        if not isinstance(text, str):
            raise InvalidSecretReferenceError(
                f"{field_name} must be an absolute secret-file reference"
            )
        text = text.strip()
        if not text:
            raise InvalidSecretReferenceError(
                f"{field_name} must be an absolute secret-file reference"
            )

        # Accept an ordinary absolute path and the canonical file URI spelling.
        # ``file://host/path`` is rejected: secret references must be local
        # mounted files, never network resources.
        if text.startswith("file://"):
            parsed = urlsplit(text)
            if parsed.scheme.lower() != "file" or parsed.netloc:
                raise InvalidSecretReferenceError(
                    f"{field_name} must use a local file URI"
                )
            raw_path = unquote(parsed.path)
            if parsed.query or parsed.fragment:
                raise InvalidSecretReferenceError(
                    f"{field_name} must not contain URI query or fragment"
                )
            if "%2f" in text.lower() or "%5c" in text.lower():
                raise InvalidSecretReferenceError(
                    f"{field_name} must not encode path separators"
                )
        elif text.startswith("file:"):
            if "%2f" in text.lower() or "%5c" in text.lower():
                raise InvalidSecretReferenceError(
                    f"{field_name} must not encode path separators"
                )
            raw_path = text[5:]
            if raw_path.startswith("//"):
                # A file URI with two slashes but no host is still interpreted
                # consistently as a local absolute path.
                raw_path = raw_path[2:]
            raw_path = unquote(raw_path)
        else:
            raw_path = text

        return cls(_canonical_path(raw_path, field_name), key=key)

    @property
    def basename(self) -> str:
        """Return the non-secret filename for deployment diagnostics."""

        return self.path.name

    def __repr__(self) -> str:
        return "SecretFileRef(<redacted>)"

    def __str__(self) -> str:
        return "<secret-file>"


def parse_secret_file_reference(
    value: object, *, field_name: str = "secret_file"
) -> SecretFileRef:
    """Validate a secret-file reference without touching the referenced file."""

    return SecretFileRef.from_value(value, field_name=field_name)


# Compatibility-friendly names for callers that prefer a verb-style helper.
parse_secret_ref = parse_secret_file_reference
validate_secret_file_reference = parse_secret_file_reference
SecretRef = SecretFileRef
SecretFile = SecretFileRef


def normalize_url(value: object, *, field_name: str = "url") -> str:
    """Return a canonical HTTP(S) URL without credentials or URL arguments.

    Service clients use configured origins only.  Query strings, fragments,
    userinfo, and non-HTTP(S) schemes are rejected before a client is built.
    Trailing slashes are removed so joining a fixed API path cannot accidentally
    create a double slash.
    """

    if not isinstance(value, str):
        raise InvalidURLConfigurationError(f"{field_name} must be an HTTP(S) URL")
    text = value.strip()
    if not text or any(
        character.isspace() or ord(character) < 0x20 for character in text
    ):
        raise InvalidURLConfigurationError(f"{field_name} must be an HTTP(S) URL")

    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        raise InvalidURLConfigurationError(f"{field_name} must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidURLConfigurationError(
            f"{field_name} must not contain URL credentials"
        )
    if parsed.query or parsed.fragment:
        raise InvalidURLConfigurationError(
            f"{field_name} must not contain a query or fragment"
        )
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise InvalidURLConfigurationError(
            f"{field_name} has an invalid host or port"
        ) from exc
    if not hostname:
        raise InvalidURLConfigurationError(f"{field_name} must include a hostname")
    if any(ord(character) < 0x20 for character in hostname):
        raise InvalidURLConfigurationError(f"{field_name} must include a hostname")

    host = hostname.lower().rstrip(".")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


canonical_url = normalize_url
validate_url = normalize_url


@dataclass(frozen=True, slots=True)
class ServiceEndpoint:
    """A named, validated dependency origin."""

    name: str
    url: str

    def __post_init__(self) -> None:
        endpoint_name = _text(self.name, "endpoint_name")
        object.__setattr__(self, "name", endpoint_name)
        object.__setattr__(
            self, "url", normalize_url(self.url, field_name=f"{endpoint_name}_url")
        )

    @property
    def base_url(self) -> str:
        return self.url

    @property
    def origin(self) -> str:
        return self.url


Endpoint = ServiceEndpoint


def _timeout_value(value: object, field_name: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidTimeoutConfigurationError(
            f"{field_name} must be a positive number"
        )
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0 or parsed > maximum:
        raise InvalidTimeoutConfigurationError(
            f"{field_name} must be between 0 and {maximum:g} seconds"
        )
    return parsed


@dataclass(frozen=True, slots=True)
class TimeoutConfig:
    """Bounded HTTP deadlines used by all companion service clients."""

    connect_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    total_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS
    body_seconds: float = DEFAULT_BODY_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        connect = _timeout_value(
            self.connect_seconds,
            "connect_timeout_seconds",
            MAX_CONNECT_TIMEOUT_SECONDS,
        )
        total = _timeout_value(
            self.total_seconds,
            "total_timeout_seconds",
            MAX_TOTAL_TIMEOUT_SECONDS,
        )
        body = _timeout_value(
            self.body_seconds,
            "body_timeout_seconds",
            MAX_BODY_TIMEOUT_SECONDS,
        )
        if connect > total:
            raise InvalidTimeoutConfigurationError(
                "connect_timeout_seconds cannot exceed total_timeout_seconds"
            )
        object.__setattr__(self, "connect_seconds", connect)
        object.__setattr__(self, "total_seconds", total)
        object.__setattr__(self, "body_seconds", body)

    @property
    def connect(self) -> float:
        return self.connect_seconds

    @property
    def connect_timeout_seconds(self) -> float:
        return self.connect_seconds

    @property
    def total(self) -> float:
        return self.total_seconds

    @property
    def total_timeout_seconds(self) -> float:
        return self.total_seconds

    @property
    def body(self) -> float:
        return self.body_seconds

    @property
    def body_timeout_seconds(self) -> float:
        return self.body_seconds

    @property
    def requests_timeout(self) -> tuple[float, float]:
        """The ``requests`` connect/read timeout tuple."""

        return (self.connect_seconds, self.total_seconds)


HttpTimeouts = TimeoutConfig
Timeouts = TimeoutConfig


@dataclass(frozen=True, slots=True)
class CompanionConfig:
    """Validated companion settings with secret contents intentionally absent."""

    upstream_url: str = ""
    upstream_token_file: SecretFileRef | None = None
    database_path: Path = field(default_factory=lambda: Path(DEFAULT_DATABASE_PATH))
    plex_url: str | None = None
    plex_token_file: SecretFileRef | None = None
    radarr_url: str | None = None
    radarr_api_key_file: SecretFileRef | None = None
    sonarr_url: str | None = None
    sonarr_api_key_file: SecretFileRef | None = None
    tmdb_url: str | None = None
    tmdb_api_key_file: SecretFileRef | None = None
    telegram_bot_token_file: SecretFileRef | None = None
    actor_signing_key_file: SecretFileRef | None = None
    dashboard_api_key_file: SecretFileRef | None = None
    plex_webhook_capability_file: SecretFileRef | None = None
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    plex_server_uuid: str | None = None
    plex_machine_identifier: str | None = None
    plex_library_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "upstream_url",
            normalize_url(self.upstream_url, field_name="upstream_url"),
        )
        for field_name in (
            "plex_url",
            "radarr_url",
            "sonarr_url",
            "tmdb_url",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    normalize_url(value, field_name=field_name),
                )

        if self.tmdb_url is None and self.tmdb_api_key_file is not None:
            object.__setattr__(self, "tmdb_url", DEFAULT_TMDB_URL)

        for field_name in (
            "upstream_token_file",
            "plex_token_file",
            "radarr_api_key_file",
            "sonarr_api_key_file",
            "tmdb_api_key_file",
            "telegram_bot_token_file",
            "actor_signing_key_file",
            "dashboard_api_key_file",
            "plex_webhook_capability_file",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    SecretFileRef.from_value(value, field_name=field_name),
                )

        db_path = self.database_path
        if not isinstance(db_path, (str, os.PathLike)):
            raise ConfigurationError("database_path must be an absolute path")
        db_text = os.fspath(db_path)
        if not isinstance(db_text, str) or not db_text.strip().startswith("/"):
            raise ConfigurationError("database_path must be an absolute path")
        if "\x00" in db_text:
            raise ConfigurationError("database_path contains an invalid character")
        object.__setattr__(self, "database_path", Path(os.path.normpath(db_text)))

        if not isinstance(self.timeouts, TimeoutConfig):
            raise InvalidTimeoutConfigurationError("timeouts must be TimeoutConfig")
        libraries = tuple(
            library.strip()
            for library in self.plex_library_names
            if isinstance(library, str) and library.strip()
        )
        object.__setattr__(self, "plex_library_names", libraries)

    @property
    def upstream(self) -> ServiceEndpoint:
        return ServiceEndpoint("upstream", self.upstream_url)

    @property
    def upstream_secret_file(self) -> SecretFileRef | None:
        return self.upstream_token_file

    @property
    def plex_secret_file(self) -> SecretFileRef | None:
        return self.plex_token_file

    @property
    def radarr_secret_file(self) -> SecretFileRef | None:
        return self.radarr_api_key_file

    @property
    def sonarr_secret_file(self) -> SecretFileRef | None:
        return self.sonarr_api_key_file

    @property
    def plex(self) -> ServiceEndpoint | None:
        return None if self.plex_url is None else ServiceEndpoint("plex", self.plex_url)

    @property
    def radarr(self) -> ServiceEndpoint | None:
        return (
            None
            if self.radarr_url is None
            else ServiceEndpoint("radarr", self.radarr_url)
        )

    @property
    def sonarr(self) -> ServiceEndpoint | None:
        return (
            None
            if self.sonarr_url is None
            else ServiceEndpoint("sonarr", self.sonarr_url)
        )

    def secret_files(self) -> Mapping[str, SecretFileRef]:
        """Return configured references, never the contents they point to."""

        return {
            field_name: value
            for field_name in (
                "upstream_token_file",
                "plex_token_file",
                "radarr_api_key_file",
                "sonarr_api_key_file",
                "tmdb_api_key_file",
                "telegram_bot_token_file",
                "actor_signing_key_file",
                "dashboard_api_key_file",
                "plex_webhook_capability_file",
            )
            if (value := getattr(self, field_name)) is not None
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CompanionConfig":
        return load_config(env)


AppConfig = CompanionConfig
Config = CompanionConfig
MediaCompanionConfig = CompanionConfig


def _lookup(values: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        raw = values.get(name)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _required(values: Mapping[str, str], field_name: str, *names: str) -> str:
    value = _lookup(values, *names)
    if value is None:
        raise MissingConfigurationError(
            f"missing required configuration field: {field_name}"
        )
    return value


def _optional_ref(
    values: Mapping[str, str],
    field_name: str,
    *names: str,
    key: str | None = None,
) -> SecretFileRef | None:
    value = _lookup(values, *names)
    if value is None:
        return None
    return SecretFileRef.from_value(value, field_name=field_name, key=key)


def _reject_inline_secret(
    values: Mapping[str, str], field_name: str, *inline_names: str
) -> None:
    if _lookup(values, *inline_names) is not None:
        raise InvalidSecretReferenceError(
            f"{field_name} must use its *_FILE secret reference"
        )


def _optional_float(
    values: Mapping[str, str], field_name: str, *names: str
) -> float | None:
    value = _lookup(values, *names)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidTimeoutConfigurationError(
            f"{field_name} must be a positive number"
        ) from exc


def _optional_path(
    values: Mapping[str, str], field_name: str, *names: str
) -> Path | None:
    value = _lookup(values, *names)
    if value is None:
        return None
    if not value.startswith("/") or "\x00" in value:
        raise ConfigurationError(f"{field_name} must be an absolute path")
    return Path(os.path.normpath(value))


def _optional_url(
    values: Mapping[str, str], field_name: str, *names: str
) -> str | None:
    value = _lookup(values, *names)
    return None if value is None else normalize_url(value, field_name=field_name)


def load_config(env: Mapping[str, str] | None = None) -> CompanionConfig:
    """Load and validate environment configuration without reading secrets.

    Only the companion's URL and mounted secret-file references are required by
    this skeleton.  Service-specific credentials are optional until the
    corresponding workflow adapter is enabled; when supplied, they must use a
    ``*_FILE`` path and are never read here.
    """

    values: Mapping[str, str] = os.environ if env is None else env

    upstream_url = _required(
        values,
        "upstream_url",
        "MEDIA_COMPANION_UPSTREAM_URL",
        "MEDIA_COMPANION_UPSTREAM_BASE_URL",
        "COMPANION_UPSTREAM_URL",
        "COMPANION_UPSTREAM_BASE_URL",
        "UPSTREAM_URL",
    )
    upstream_token_file = SecretFileRef.from_value(
        _required(
            values,
            "upstream_token_file",
            "MEDIA_COMPANION_UPSTREAM_TOKEN_FILE",
            "COMPANION_UPSTREAM_TOKEN_FILE",
            "UPSTREAM_TOKEN_FILE",
            "MEDIA_COMPANION_UPSTREAM_SECRET_FILE",
            "COMPANION_UPSTREAM_SECRET_FILE",
            "UPSTREAM_SECRET_FILE",
        ),
        field_name="upstream_token_file",
        key="MCP_AUTH_TOKEN",
    )

    _reject_inline_secret(
        values,
        "upstream_token_file",
        "MEDIA_COMPANION_UPSTREAM_TOKEN",
        "COMPANION_UPSTREAM_TOKEN",
        "UPSTREAM_TOKEN",
    )
    _reject_inline_secret(
        values,
        "plex_token_file",
        "MEDIA_COMPANION_PLEX_TOKEN",
        "COMPANION_PLEX_TOKEN",
        "PLEX_TOKEN",
    )
    _reject_inline_secret(
        values,
        "radarr_api_key_file",
        "MEDIA_COMPANION_RADARR_API_KEY",
        "COMPANION_RADARR_API_KEY",
        "RADARR_API_KEY",
    )
    _reject_inline_secret(
        values,
        "sonarr_api_key_file",
        "MEDIA_COMPANION_SONARR_API_KEY",
        "COMPANION_SONARR_API_KEY",
        "SONARR_API_KEY",
    )
    _reject_inline_secret(
        values,
        "tmdb_api_key_file",
        "MEDIA_COMPANION_TMDB_API_KEY",
        "COMPANION_TMDB_API_KEY",
        "TMDB_API_KEY",
    )
    _reject_inline_secret(
        values,
        "telegram_bot_token_file",
        "MEDIA_COMPANION_TELEGRAM_BOT_TOKEN",
        "COMPANION_TELEGRAM_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
    )

    connect = _optional_float(
        values,
        "connect_timeout_seconds",
        "MEDIA_COMPANION_CONNECT_TIMEOUT_SECONDS",
        "MEDIA_COMPANION_CONNECT_TIMEOUT",
        "COMPANION_CONNECT_TIMEOUT_SECONDS",
        "COMPANION_CONNECT_TIMEOUT",
        "CONNECT_TIMEOUT_SECONDS",
    )
    total = _optional_float(
        values,
        "total_timeout_seconds",
        "MEDIA_COMPANION_TOTAL_TIMEOUT_SECONDS",
        "MEDIA_COMPANION_TOTAL_TIMEOUT",
        "COMPANION_TOTAL_TIMEOUT_SECONDS",
        "COMPANION_TOTAL_TIMEOUT",
        "TOTAL_TIMEOUT_SECONDS",
        "MEDIA_COMPANION_TIMEOUT_SECONDS",
        "MEDIA_COMPANION_REQUEST_TIMEOUT_SECONDS",
        "COMPANION_TIMEOUT_SECONDS",
        "COMPANION_REQUEST_TIMEOUT_SECONDS",
        "TIMEOUT_SECONDS",
    )
    body = _optional_float(
        values,
        "body_timeout_seconds",
        "MEDIA_COMPANION_BODY_TIMEOUT_SECONDS",
        "MEDIA_COMPANION_BODY_TIMEOUT",
        "COMPANION_BODY_TIMEOUT_SECONDS",
        "COMPANION_BODY_TIMEOUT",
        "BODY_TIMEOUT_SECONDS",
    )
    timeouts = TimeoutConfig(
        connect_seconds=(
            DEFAULT_CONNECT_TIMEOUT_SECONDS if connect is None else connect
        ),
        total_seconds=DEFAULT_TOTAL_TIMEOUT_SECONDS if total is None else total,
        body_seconds=DEFAULT_BODY_TIMEOUT_SECONDS if body is None else body,
    )

    libraries_value = _lookup(
        values,
        "MEDIA_COMPANION_PLEX_LIBRARY_NAMES",
        "COMPANION_PLEX_LIBRARY_NAMES",
        "PLEX_LIBRARY_NAMES",
    )
    libraries = (
        tuple(item.strip() for item in libraries_value.split(",") if item.strip())
        if libraries_value is not None
        else ()
    )

    db_path = _optional_path(
        values,
        "database_path",
        "MEDIA_COMPANION_DB_PATH",
        "COMPANION_DB_PATH",
        "DATABASE_PATH",
    )

    return CompanionConfig(
        upstream_url=upstream_url,
        upstream_token_file=upstream_token_file,
        database_path=Path(DEFAULT_DATABASE_PATH) if db_path is None else db_path,
        plex_url=_optional_url(
            values,
            "plex_url",
            "MEDIA_COMPANION_PLEX_URL",
            "COMPANION_PLEX_URL",
            "PLEX_URL",
        ),
        plex_token_file=_optional_ref(
            values,
            "plex_token_file",
            "MEDIA_COMPANION_PLEX_TOKEN_FILE",
            "COMPANION_PLEX_TOKEN_FILE",
            "PLEX_TOKEN_FILE",
            "MEDIA_COMPANION_PLEX_SECRET_FILE",
            "COMPANION_PLEX_SECRET_FILE",
            "PLEX_SECRET_FILE",
            key="PLEX_API_KEY",
        ),
        radarr_url=_optional_url(
            values,
            "radarr_url",
            "MEDIA_COMPANION_RADARR_URL",
            "COMPANION_RADARR_URL",
            "RADARR_URL",
        ),
        radarr_api_key_file=_optional_ref(
            values,
            "radarr_api_key_file",
            "MEDIA_COMPANION_RADARR_API_KEY_FILE",
            "COMPANION_RADARR_API_KEY_FILE",
            "RADARR_API_KEY_FILE",
            "MEDIA_COMPANION_RADARR_SECRET_FILE",
            "COMPANION_RADARR_SECRET_FILE",
            "RADARR_SECRET_FILE",
            key="RADARR_API_KEY",
        ),
        sonarr_url=_optional_url(
            values,
            "sonarr_url",
            "MEDIA_COMPANION_SONARR_URL",
            "COMPANION_SONARR_URL",
            "SONARR_URL",
        ),
        sonarr_api_key_file=_optional_ref(
            values,
            "sonarr_api_key_file",
            "MEDIA_COMPANION_SONARR_API_KEY_FILE",
            "COMPANION_SONARR_API_KEY_FILE",
            "SONARR_API_KEY_FILE",
            "MEDIA_COMPANION_SONARR_SECRET_FILE",
            "COMPANION_SONARR_SECRET_FILE",
            "SONARR_SECRET_FILE",
            key="SONARR_API_KEY",
        ),
        tmdb_url=_optional_url(
            values,
            "tmdb_url",
            "MEDIA_COMPANION_TMDB_URL",
            "COMPANION_TMDB_URL",
            "TMDB_URL",
        ),
        tmdb_api_key_file=_optional_ref(
            values,
            "tmdb_api_key_file",
            "MEDIA_COMPANION_TMDB_API_KEY_FILE",
            "COMPANION_TMDB_API_KEY_FILE",
            "TMDB_API_KEY_FILE",
            key="TMDB_API_KEY",
        ),
        telegram_bot_token_file=_optional_ref(
            values,
            "telegram_bot_token_file",
            "MEDIA_COMPANION_TELEGRAM_BOT_TOKEN_FILE",
            "COMPANION_TELEGRAM_BOT_TOKEN_FILE",
            "TELEGRAM_BOT_TOKEN_FILE",
            key="TELEGRAM_BOT_TOKEN",
        ),
        actor_signing_key_file=_optional_ref(
            values,
            "actor_signing_key_file",
            "MEDIA_COMPANION_ACTOR_SIGNING_KEY_FILE",
            "COMPANION_ACTOR_SIGNING_KEY_FILE",
            "ACTOR_SIGNING_KEY_FILE",
        ),
        dashboard_api_key_file=_optional_ref(
            values,
            "dashboard_api_key_file",
            "MEDIA_COMPANION_DASHBOARD_API_KEY_FILE",
            "COMPANION_DASHBOARD_API_KEY_FILE",
            "DASHBOARD_API_KEY_FILE",
        ),
        plex_webhook_capability_file=_optional_ref(
            values,
            "plex_webhook_capability_file",
            "MEDIA_COMPANION_PLEX_WEBHOOK_CAPABILITY_FILE",
            "COMPANION_PLEX_WEBHOOK_CAPABILITY_FILE",
            "PLEX_WEBHOOK_CAPABILITY_FILE",
        ),
        timeouts=timeouts,
        plex_server_uuid=_lookup(
            values,
            "MEDIA_COMPANION_PLEX_SERVER_UUID",
            "COMPANION_PLEX_SERVER_UUID",
            "PLEX_SERVER_UUID",
        ),
        plex_machine_identifier=_lookup(
            values,
            "MEDIA_COMPANION_PLEX_MACHINE_IDENTIFIER",
            "COMPANION_PLEX_MACHINE_IDENTIFIER",
            "PLEX_MACHINE_IDENTIFIER",
        ),
        plex_library_names=libraries,
    )


from_environment = load_config


__all__ = [
    "AppConfig",
    "CANONICAL_SECRET_FILENAMES",
    "CompanionConfig",
    "Config",
    "DEFAULT_BODY_TIMEOUT_SECONDS",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_DATABASE_PATH",
    "DEFAULT_TMDB_URL",
    "DEFAULT_TOTAL_TIMEOUT_SECONDS",
    "Endpoint",
    "HttpTimeouts",
    "MAX_BODY_TIMEOUT_SECONDS",
    "MAX_CONNECT_TIMEOUT_SECONDS",
    "MAX_TOTAL_TIMEOUT_SECONDS",
    "SecretFileRef",
    "SecretFile",
    "SecretRef",
    "ServiceEndpoint",
    "TimeoutConfig",
    "Timeouts",
    "canonical_url",
    "from_environment",
    "load_config",
    "normalize_url",
    "parse_secret_file_reference",
    "parse_secret_ref",
    "validate_secret_file_reference",
    "validate_url",
    "MediaCompanionConfig",
]
