"""Read-only legacy migration planning and transactional import helpers.

The first version of Media Companion intentionally does not make the legacy
``media_requests`` table part of its schema migration.  This module is the
explicit, reviewable bridge between that table and the companion ledger.  It
has two deliberately separate operations:

``dry_run_legacy_migration``
    Opens a database read-only (or accepts synthetic row mappings), validates
    every row, and returns an aggregate/redactable plan.  It never writes to
    either database.

``import_legacy_rows`` / :class:`LegacyMigrationImporter`
    Applies an already planned set in one target transaction.  A source row
    receives exactly one durable disposition and a deterministic source
    mapping.  Series seasons are expanded into independent canonical
    subscriptions/units; that expansion is derived lineage and is not counted
    as additional source rows.  Source deletion is opt-in and requires a
    verified backup plus exact dry-run approval.

The functions are intentionally dependency-light.  They accept the current
``Database`` wrapper, a sqlite connection/path, or synthetic mappings so the
tests can exercise every known legacy shape without production access.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import quote

from .db import Database, utc_timestamp


LEGACY_TABLE = "media_requests"
DEFAULT_SOURCE_NAME = "legacy_media_requests"
MAX_SEASONS = 50
_MAX_SQLITE_INTEGER = 2**63 - 1

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IMDB = re.compile(r"^tt[0-9]+$", re.IGNORECASE)
_SAFE_KEY = re.compile(r"[^A-Za-z0-9_.-]+")


class LegacyMigrationError(RuntimeError):
    """Base error for an unsafe or incomplete legacy import."""


class LegacySchemaError(LegacyMigrationError):
    """Raised when a source or target does not have the expected shape."""


class LegacyDisposition(str, Enum):
    """The mutually exclusive source-row outcomes.

    ``delete_candidate`` is a dry-run/accounting disposition.  It is never
    executed unless the caller supplies an explicit approval flag.  The
    canonical post-approval disposition is ``deleted_after_approval``.
    """

    MIGRATED = "migrated"
    EQUIVALENTLY_MERGED = "equivalently_merged"
    TERMINALLY_ARCHIVED = "terminally_archived"
    DELETE_CANDIDATE = "delete_candidate"
    DELETED_AFTER_APPROVAL = "deleted_after_approval"
    QUARANTINED = "quarantined"

    # Friendly compatibility spellings used by callers that name the action
    # rather than the accounting label.
    MERGED = "equivalently_merged"
    ARCHIVED = "terminally_archived"
    DELETED = "deleted_after_approval"


class LegacyReason(str, Enum):
    """Stable reason codes used in dry-run aggregates and quarantine rows."""

    VALID_PENDING_MOVIE = "valid_pending_movie"
    VALID_PENDING_SERIES = "valid_pending_series"
    TERMINAL_STATUS = "terminal_status"
    NOTIFIED = "notified"
    MISSING_DESTINATION = "missing_destination"
    MISSING_SERIES_SEASONS = "missing_series_seasons"
    MISSING_IDENTITY = "missing_identity"
    INVALID_SOURCE_ID = "invalid_source_id"
    UNKNOWN_MEDIA_TYPE = "unknown_media_type"
    INVALID_STATUS = "invalid_status"
    INVALID_SEASONS = "invalid_series_seasons"
    TOO_MANY_SEASONS = "too_many_series_seasons"
    MOVIE_SEASONS_NOT_NULL = "movie_seasons_not_null"
    IDENTITY_MALFORMED = "identity_malformed"
    IDENTITY_TYPE_MISMATCH = "identity_type_mismatch"
    IDENTITY_CONFLICT = "identity_conflict"
    SOURCE_ROW_CHANGED = "source_row_changed_after_mapping"
    MISSING_SOURCE_ROW_ID = "missing_source_row_id"
    APPROVAL_WITHOUT_SOURCE = "approval_without_deletable_source"
    MISSING_REQUESTER_ID = "missing_requester_user_id"
    SOURCE_SNAPSHOT_CHANGED = "source_snapshot_changed"
    PROVIDER_STATE_WITHOUT_IDENTITY = "provider_state_without_canonical_identity"
    INVALID_YEAR = "invalid_year"


@dataclass(frozen=True, slots=True)
class LegacyIdentity:
    """Validated stable identifiers copied from one legacy row."""

    media_type: str
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    imdb_id: str | None = None
    provider: str | None = None
    provider_id: str | None = None

    @property
    def aliases(self) -> tuple[tuple[str, str], ...]:
        values: list[tuple[str, str]] = []
        if self.tmdb_id is not None:
            values.append(("tmdb", str(self.tmdb_id)))
        if self.tvdb_id is not None:
            values.append(("tvdb", str(self.tvdb_id)))
        if self.imdb_id is not None:
            values.append(("imdb", self.imdb_id))
        if self.provider is not None and self.provider_id is not None:
            if (self.provider, self.provider_id) not in values:
                values.append((self.provider, self.provider_id))
        return tuple(values)

    @property
    def stable(self) -> bool:
        return bool(self.aliases)

    @property
    def key(self) -> str:
        return ":".join(
            [self.media_type, *(f"{name}={value}" for name, value in self.aliases)]
        )


@dataclass(frozen=True, slots=True)
class LegacyRowClassification:
    """Deterministic, non-sensitive classification of a source row."""

    source_row_id: str | None
    disposition: LegacyDisposition
    reason: str
    media_type: str | None
    destination: int | None
    identity: LegacyIdentity | None
    seasons: tuple[int, ...]
    source_fingerprint: str
    terminal: bool = False
    delete_candidate: bool = False
    source_table: str = LEGACY_TABLE
    source_name: str = DEFAULT_SOURCE_NAME

    @property
    def expansion_count(self) -> int:
        """Number of derived target units for this row."""

        if self.disposition in {
            LegacyDisposition.MIGRATED,
            LegacyDisposition.EQUIVALENTLY_MERGED,
        }:
            return len(self.seasons) if self.media_type == "series" else 1
        return 0

    @property
    def source_id(self) -> str | None:
        """Alias retained for callers that use ``source_id`` terminology."""

        return self.source_row_id


@dataclass(frozen=True, slots=True)
class LegacyDryRunReport:
    """Aggregate-only dry-run result with per-row decisions for audit/tests."""

    rows: tuple[LegacyRowClassification, ...]
    source_name: str = DEFAULT_SOURCE_NAME
    source_table: str = LEGACY_TABLE
    source_identity: str | None = None
    source_schema_fingerprint: str | None = None
    source_data_fingerprint: str | None = None

    @property
    def source_rows(self) -> int:
        return len(self.rows)

    @property
    def disposition_counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in self.rows:
            value = row.disposition.value
            result[value] = result.get(value, 0) + 1
        return dict(sorted(result.items()))

    @property
    def counts(self) -> dict[str, int]:
        """Alias for ``disposition_counts``."""

        return self.disposition_counts

    @property
    def reason_counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in self.rows:
            result[row.reason] = result.get(row.reason, 0) + 1
        return dict(sorted(result.items()))

    @property
    def derived_expansion_count(self) -> int:
        return sum(row.expansion_count for row in self.rows)

    @property
    def series_expansion_count(self) -> int:
        return sum(
            row.expansion_count for row in self.rows if row.media_type == "series"
        )

    @property
    def delete_candidate_count(self) -> int:
        return sum(row.delete_candidate for row in self.rows)

    @property
    def quarantined_count(self) -> int:
        return self.disposition_counts.get(LegacyDisposition.QUARANTINED.value, 0)

    @property
    def residual(self) -> int:
        """Rows without exactly one disposition (always zero for valid reports)."""

        return self.source_rows - sum(self.disposition_counts.values())

    def to_redacted_artifact(self) -> dict[str, Any]:
        """Return an aggregate artifact containing no title/requester values."""

        return {
            "schema_version": 1,
            "source_name": self.source_name,
            "source_table": self.source_table,
            "source_rows": self.source_rows,
            "source_identity": self.source_identity,
            "source_schema_fingerprint": self.source_schema_fingerprint,
            "source_data_fingerprint": self.source_data_fingerprint,
            "disposition_counts": self.disposition_counts,
            "reason_counts": self.reason_counts,
            "derived_expansion_count": self.derived_expansion_count,
            "series_expansion_count": self.series_expansion_count,
            "delete_candidate_count": self.delete_candidate_count,
            "quarantined_count": self.quarantined_count,
            "residual": self.residual,
        }

    @property
    def redacted_artifact(self) -> dict[str, Any]:
        return self.to_redacted_artifact()

    def to_json(self) -> str:
        return json.dumps(
            self.to_redacted_artifact(), sort_keys=True, separators=(",", ":")
        )

    @property
    def dry_run_hash(self) -> str:
        """Hash of the complete redacted plan used for approval binding."""

        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @property
    def plan_hash(self) -> str:
        """Compatibility alias for :attr:`dry_run_hash`."""

        return self.dry_run_hash

    def approve(
        self,
        backup: "LegacyBackupArtifact",
        *,
        approved_delete_reasons: Iterable[str] = (),
    ) -> "LegacyMigrationApproval":
        return LegacyMigrationApproval.from_dry_run(
            self,
            backup,
            approved_delete_reasons=approved_delete_reasons,
        )


@dataclass(frozen=True, slots=True)
class LegacyBackupArtifact:
    """Immutable, verified snapshot metadata for a legacy SQLite source.

    The artifact deliberately stores hashes and source identity rather than
    source values.  The backup file is created with SQLite's native backup
    API while an ``IMMEDIATE`` transaction fences source writers, then is
    atomically published with read-only permissions.
    """

    source_database: str
    source_name: str
    source_table: str
    source_identity: str
    source_schema_fingerprint: str
    source_data_fingerprint: str
    source_rows: int
    backup_path: str
    backup_sha256: str
    backup_size_bytes: int
    integrity_check: str = "ok"
    quick_check: str = "ok"
    verified: bool = True
    immutable: bool = True

    @property
    def artifact_hash(self) -> str:
        payload = {
            "source_database": self.source_database,
            "source_name": self.source_name,
            "source_table": self.source_table,
            "source_identity": self.source_identity,
            "source_schema_fingerprint": self.source_schema_fingerprint,
            "source_data_fingerprint": self.source_data_fingerprint,
            "source_rows": self.source_rows,
            "backup_sha256": self.backup_sha256,
            "backup_size_bytes": self.backup_size_bytes,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def to_redacted_artifact(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source_name": self.source_name,
            "source_table": self.source_table,
            "source_rows": self.source_rows,
            "source_identity": self.source_identity,
            "source_schema_fingerprint": self.source_schema_fingerprint,
            "source_data_fingerprint": self.source_data_fingerprint,
            "backup_sha256": self.backup_sha256,
            "backup_size_bytes": self.backup_size_bytes,
            "integrity_check": self.integrity_check,
            "quick_check": self.quick_check,
            "verified": self.verified,
            "immutable": self.immutable,
        }


@dataclass(frozen=True, slots=True)
class LegacyMigrationApproval:
    """Approval bound to one exact dry-run, source snapshot, and backup."""

    source_name: str
    source_table: str
    dry_run_hash: str
    source_rows: int
    disposition_counts: Mapping[str, int]
    reason_counts: Mapping[str, int]
    approved_delete_reasons: tuple[str, ...]
    backup: LegacyBackupArtifact

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "disposition_counts", MappingProxyType(dict(self.disposition_counts))
        )
        object.__setattr__(
            self, "reason_counts", MappingProxyType(dict(self.reason_counts))
        )

    @classmethod
    def from_dry_run(
        cls,
        report: LegacyDryRunReport,
        backup: LegacyBackupArtifact,
        *,
        approved_delete_reasons: Iterable[str] = (),
    ) -> "LegacyMigrationApproval":
        if not backup.verified or not backup.immutable:
            raise LegacyMigrationError("approval requires a verified immutable backup")
        if (
            backup.source_name != report.source_name
            or backup.source_table != report.source_table
            or backup.source_rows != report.source_rows
            or backup.source_data_fingerprint != report.source_data_fingerprint
            or (
                report.source_identity is not None
                and backup.source_identity != report.source_identity
            )
        ):
            raise LegacyMigrationError(
                "backup does not match the dry-run source snapshot"
            )
        reasons = tuple(
            sorted(
                {
                    reason.value if isinstance(reason, Enum) else str(reason)
                    for reason in approved_delete_reasons
                }
            )
        )
        candidate_reasons = {row.reason for row in report.rows if row.delete_candidate}
        if not set(reasons) <= candidate_reasons:
            raise LegacyMigrationError(
                "approval includes a reason absent from the exact dry-run"
            )
        return cls(
            report.source_name,
            report.source_table,
            report.dry_run_hash,
            report.source_rows,
            dict(report.disposition_counts),
            dict(report.reason_counts),
            reasons,
            backup,
        )

    @property
    def approval_hash(self) -> str:
        payload = {
            "source_name": self.source_name,
            "source_table": self.source_table,
            "dry_run_hash": self.dry_run_hash,
            "source_rows": self.source_rows,
            "disposition_counts": dict(self.disposition_counts),
            "reason_counts": dict(self.reason_counts),
            "approved_delete_reasons": self.approved_delete_reasons,
            "backup_hash": self.backup.artifact_hash,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class LegacyImportOutcome:
    """One source row's final importer outcome."""

    classification: LegacyRowClassification
    disposition: LegacyDisposition
    reason: str
    target_request_id: int | None = None
    target_item_ids: tuple[int, ...] = ()
    source_deleted: bool = False

    @property
    def source_row_id(self) -> str | None:
        return self.classification.source_row_id

    @property
    def derived_item_count(self) -> int:
        return len(self.target_item_ids)


@dataclass(frozen=True, slots=True)
class LegacyImportReport:
    """Transactional import report."""

    outcomes: tuple[LegacyImportOutcome, ...]
    source_name: str = DEFAULT_SOURCE_NAME
    source_table: str = LEGACY_TABLE

    @property
    def rows(self) -> tuple[LegacyImportOutcome, ...]:
        return self.outcomes

    @property
    def source_rows(self) -> int:
        return len(self.outcomes)

    @property
    def disposition_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            value = outcome.disposition.value
            counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def counts(self) -> dict[str, int]:
        return self.disposition_counts

    @property
    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.reason] = counts.get(outcome.reason, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def derived_expansion_count(self) -> int:
        return sum(outcome.derived_item_count for outcome in self.outcomes)

    @property
    def expansion_count(self) -> int:
        return self.derived_expansion_count

    @property
    def deleted_source_rows(self) -> int:
        return sum(outcome.source_deleted for outcome in self.outcomes)

    @property
    def residual(self) -> int:
        return self.source_rows - sum(self.disposition_counts.values())

    def to_redacted_artifact(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source_name": self.source_name,
            "source_table": self.source_table,
            "source_rows": self.source_rows,
            "disposition_counts": self.disposition_counts,
            "reason_counts": self.reason_counts,
            "derived_expansion_count": self.derived_expansion_count,
            "deleted_source_rows": self.deleted_source_rows,
            "residual": self.residual,
        }

    @property
    def redacted_artifact(self) -> dict[str, Any]:
        return self.to_redacted_artifact()


@dataclass(frozen=True, slots=True)
class _SourceRows:
    rows: tuple[Mapping[str, Any], ...]
    source_name: str
    source_table: str
    source_path: Path | None = None
    source_connection: sqlite3.Connection | None = None
    delete_supported: bool = False
    source_identity: str | None = None
    source_schema_fingerprint: str | None = None
    source_data_fingerprint: str | None = None


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _source_name(value: str) -> str:
    if not isinstance(value, str):
        raise LegacySchemaError("source name must be text")
    normalized = value.strip()
    if not normalized or len(normalized.encode("utf-8", "replace")) > 256:
        raise LegacySchemaError("source name must be bounded and non-empty")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in normalized):
        raise LegacySchemaError("source name contains control characters")
    return normalized


def _quote_identifier(value: str, label: str = "identifier") -> str:
    """Validate and quote an identifier before interpolating it into SQL."""

    return '"' + _identifier(value, label).replace('"', '""') + '"'


def _lookup(row: Mapping[str, Any], *names: str) -> Any:
    """Case-insensitive legacy-column lookup with explicit aliases."""

    for name in names:
        if name in row:
            return row[name]
    lower = {str(key).lower(): key for key in row}
    for name in names:
        key = lower.get(name.lower())
        if key is not None:
            return row[key]
    return None


def _lookup_values(row: Mapping[str, Any], *names: str) -> tuple[Any, ...]:
    """Return all populated aliases, preserving their source order."""

    lower = {str(key).lower(): key for key in row}
    values: list[Any] = []
    seen: set[str] = set()
    for name in names:
        key: Any | None = name if name in row else lower.get(name.lower())
        if key is None or str(key).lower() in seen:
            continue
        seen.add(str(key).lower())
        value = row[key]
        if value is not None:
            values.append(value)
    return tuple(values)


def _bounded_text(value: Any, *, max_bytes: int = 512) -> str | None:
    """Return bounded, printable text suitable for durable ledger fields."""

    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    # Telegram/control characters and NULs must not enter audit/request text.
    normalized = "".join(
        char if (ord(char) >= 0x20 and char != "\x7f") else " " for char in value
    ).strip()
    if not normalized:
        return None
    encoded = normalized.encode("utf-8", "replace")
    if len(encoded) <= max_bytes:
        return normalized
    clipped = encoded[:max_bytes].decode("utf-8", "ignore").rstrip()
    return clipped or None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _fingerprint(row: Mapping[str, Any]) -> str:
    payload = json.dumps(_json_safe(row), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rows_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        [_json_safe(row) for row in rows], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_identity(
    path: Path,
    *,
    source_table: str,
    schema_fingerprint: str,
    data_fingerprint: str,
) -> str:
    """Hash path identity plus content so replacement/race is fail-closed."""

    resolved = path.resolve()
    stat_result = resolved.stat()
    payload = {
        "path": str(resolved),
        "device": int(stat_result.st_dev),
        "inode": int(stat_result.st_ino),
        "size": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
        "table": source_table,
        "schema": schema_fingerprint,
        "data": data_fingerprint,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _source_row_id(row: Mapping[str, Any]) -> str | None:
    values = _lookup_values(row, "id", "request_id", "source_row_id", "source_id")
    if len(values) > 1:
        normalized: list[str] = []
        for value in values:
            if isinstance(value, bool):
                return None
            if isinstance(value, int) and value > 0:
                normalized.append(str(value))
            elif isinstance(value, str) and value.strip():
                normalized.append(value.strip())
            else:
                return None
        if len(set(normalized)) != 1:
            return None
    value = values[0] if values else None
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if 0 < value <= _MAX_SQLITE_INTEGER else None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if len(text.encode("utf-8", "replace")) > 256 or any(
            ord(char) < 0x20 or ord(char) == 0x7F for char in text
        ):
            return None
        return text
    return None


def _media_type(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    values = _lookup_values(row, "media_type", "media_kind", "type")
    if len(values) > 1:
        normalized_values = {str(value).strip().lower() for value in values}
        if len(normalized_values) != 1:
            return None, LegacyReason.UNKNOWN_MEDIA_TYPE.value
    value = values[0] if values else None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"movie", "film"}:
            return "movie", None
        if normalized in {"series", "tv", "show"}:
            return "series", None
        return None, LegacyReason.UNKNOWN_MEDIA_TYPE.value
    return None, LegacyReason.UNKNOWN_MEDIA_TYPE.value


def _destination(row: Mapping[str, Any]) -> tuple[int | None, bool]:
    destination_keys = (
        "requested_by_chat_id",
        "destination",
        "destination_id",
        "chat_id",
    )
    values = _lookup_values(row, *destination_keys)
    if not values:
        return None, True
    normalized: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            return None, False
        if (
            value == 0
            or value < -(_MAX_SQLITE_INTEGER + 1)
            or value > _MAX_SQLITE_INTEGER
        ):
            return None, False
        normalized.append(value)
    if len(set(normalized)) != 1:
        return None, False
    return normalized[0], True


def _positive_id(value: Any) -> tuple[int | None, bool]:
    if value is None:
        return None, True
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > _MAX_SQLITE_INTEGER
    ):
        return None, False
    return value, True


def _imdb_id(value: Any) -> tuple[str | None, bool]:
    if value is None:
        return None, True
    if not isinstance(value, str):
        return None, False
    value = value.strip()
    if not value or _IMDB.fullmatch(value) is None:
        return None, False
    return value.lower(), True


def _legacy_year(row: Mapping[str, Any]) -> tuple[int | None, bool]:
    value = _lookup(row, "year", "release_year")
    if value is None:
        return None, True
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 9999:
        return None, False
    return value, True


def _identity(
    row: Mapping[str, Any], media_type: str
) -> tuple[LegacyIdentity | None, str | None]:
    def consistent(*names: str) -> tuple[Any, bool]:
        values = _lookup_values(row, *names)
        if not values:
            return None, True
        # Alias columns are allowed to repeat the same value, never to
        # silently choose one of two competing identities.
        normalized = {str(value).strip().lower() for value in values}
        return (values[0], len(normalized) == 1)

    tmdb_value, tmdb_consistent = consistent("tmdb_id", "tmdbId", "tmdb")
    tvdb_value, tvdb_consistent = consistent("tvdb_id", "tvdbId", "tvdb")
    imdb_value, imdb_consistent = consistent("imdb_id", "imdbId", "imdb")
    if not (tmdb_consistent and tvdb_consistent and imdb_consistent):
        return None, LegacyReason.IDENTITY_CONFLICT.value
    tmdb, tmdb_ok = _positive_id(tmdb_value)
    tvdb, tvdb_ok = _positive_id(tvdb_value)
    imdb, imdb_ok = _imdb_id(imdb_value)
    if not (tmdb_ok and tvdb_ok and imdb_ok):
        return None, LegacyReason.IDENTITY_MALFORMED.value

    # The old request paths use TMDB for movies and TVDB for series.  IMDb is
    # a stable cross-type fallback.  A value in the wrong type-specific slot
    # is recoverable only with operator review, so quarantine rather than
    # treating it as a safe delete.
    if media_type == "movie" and tmdb is None and imdb is None and tvdb is not None:
        return None, LegacyReason.IDENTITY_TYPE_MISMATCH.value
    if media_type == "series" and tvdb is None and imdb is None and tmdb is not None:
        return None, LegacyReason.IDENTITY_TYPE_MISMATCH.value

    provider_values = _lookup_values(row, "external_provider", "provider")
    provider_id_values = _lookup_values(row, "external_id", "provider_id")
    if (
        len(provider_values) > 1
        and {str(value).strip().lower() for value in provider_values}.__len__() != 1
    ):
        return None, LegacyReason.IDENTITY_CONFLICT.value
    if (
        len(provider_id_values) > 1
        and {str(value).strip().lower() for value in provider_id_values}.__len__() != 1
    ):
        return None, LegacyReason.IDENTITY_CONFLICT.value
    provider = provider_values[0] if provider_values else None
    provider_id = provider_id_values[0] if provider_id_values else None
    if provider is not None or provider_id is not None:
        if not isinstance(provider, str) or not isinstance(provider_id, (str, int)):
            return None, LegacyReason.IDENTITY_MALFORMED.value
        provider = provider.strip().lower()
        provider_id = str(provider_id).strip()
        if not provider or not provider_id:
            return None, LegacyReason.IDENTITY_MALFORMED.value
        if (media_type == "movie" and provider == "tvdb") or (
            media_type == "series" and provider == "tmdb"
        ):
            return None, LegacyReason.IDENTITY_TYPE_MISMATCH.value
        if provider in {"tmdb", "tvdb"}:
            numeric_provider_id: Any = provider_id
            if isinstance(provider_id, str) and provider_id.isdecimal():
                numeric_provider_id = int(provider_id)
            parsed, parsed_ok = _positive_id(numeric_provider_id)
            if not parsed_ok or parsed is None:
                return None, LegacyReason.IDENTITY_MALFORMED.value
            if provider == "tmdb" and tmdb is not None and tmdb != parsed:
                return None, LegacyReason.IDENTITY_CONFLICT.value
            if provider == "tvdb" and tvdb is not None and tvdb != parsed:
                return None, LegacyReason.IDENTITY_CONFLICT.value
            provider_id = str(parsed)
        elif provider == "imdb":
            parsed_imdb, parsed_ok = _imdb_id(provider_id)
            if not parsed_ok or parsed_imdb is None:
                return None, LegacyReason.IDENTITY_MALFORMED.value
            if imdb is not None and imdb != parsed_imdb:
                return None, LegacyReason.IDENTITY_CONFLICT.value
            provider_id = parsed_imdb
        else:
            # Unknown provider labels cannot be safely compared across rows.
            return None, LegacyReason.IDENTITY_CONFLICT.value
    else:
        provider = None
        provider_id = None

    identity = LegacyIdentity(
        media_type=media_type,
        tmdb_id=tmdb,
        tvdb_id=tvdb,
        imdb_id=imdb,
        provider=provider,
        provider_id=provider_id,
    )
    if not identity.stable:
        return None, LegacyReason.MISSING_IDENTITY.value
    return identity, None


def _decode_seasons(value: Any) -> tuple[tuple[int, ...] | None, str | None]:
    """Decode the old JSON season field without coercing unsafe values."""

    if value is None:
        return None, None
    decoded = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, LegacyReason.INVALID_SEASONS.value
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, LegacyReason.INVALID_SEASONS.value
    if decoded is None:
        return None, None
    if isinstance(decoded, (str, bytes, dict)) or not isinstance(
        decoded, (list, tuple)
    ):
        return None, LegacyReason.INVALID_SEASONS.value
    seasons: set[int] = set()
    for value_item in decoded:
        if (
            isinstance(value_item, bool)
            or not isinstance(value_item, int)
            or value_item < 0
            or value_item > _MAX_SQLITE_INTEGER
        ):
            return None, LegacyReason.INVALID_SEASONS.value
        seasons.add(value_item)
    if len(seasons) > MAX_SEASONS:
        return None, LegacyReason.TOO_MANY_SEASONS.value
    return tuple(sorted(seasons)), None


_PENDING_STATUSES = {
    "requested",
    "accepted",
    "downloading",
    "imported_to_arr",
    "notifying",
    "pending",
    "processing",
}
_TERMINAL_STATUSES = {
    "available",
    "fulfilled",
    "delivered",
    "canceled",
    "cancelled",
}


def _status(row: Mapping[str, Any]) -> tuple[str | None, bool]:
    value = _lookup(row, "status", "state")
    if not isinstance(value, str):
        return None, False
    normalized = value.strip().lower()
    if (
        normalized in _PENDING_STATUSES
        or normalized in _TERMINAL_STATUSES
        or normalized
        in {
            "visible_in_plex",
        }
    ):
        return normalized, True
    return normalized, False


def classify_legacy_row(
    row: Mapping[str, Any],
    *,
    source_name: str = DEFAULT_SOURCE_NAME,
    source_table: str = LEGACY_TABLE,
) -> LegacyRowClassification:
    """Classify one legacy row without writing or exposing its contents.

    The delete-candidate set is intentionally narrow: a known movie/series
    pending row with a structurally valid (or absent) season field and a
    *missing* destination, missing stable identity, or missing series scope.
    Malformed fields, wrong-type IDs, unknown statuses/types, and conflicts are
    quarantined instead.
    """

    source_name = _source_name(source_name)
    source_table = _identifier(source_table, "source table")
    fingerprint = _fingerprint(row)
    source_id = _source_row_id(row)
    if source_id is None:
        return LegacyRowClassification(
            source_id,
            LegacyDisposition.QUARANTINED,
            LegacyReason.INVALID_SOURCE_ID.value,
            None,
            None,
            None,
            (),
            fingerprint,
            source_table=source_table,
            source_name=source_name,
        )

    media_type, media_reason = _media_type(row)
    if media_type is None:
        return LegacyRowClassification(
            source_id,
            LegacyDisposition.QUARANTINED,
            media_reason or LegacyReason.UNKNOWN_MEDIA_TYPE.value,
            None,
            None,
            None,
            (),
            fingerprint,
            source_table=source_table,
            source_name=source_name,
        )

    status, status_known = _status(row)
    notified = _lookup(row, "notified_available_at", "notified_at", "fulfilled_at")
    has_notification = notified is not None and str(notified).strip() != ""
    terminal = (
        has_notification or status in _TERMINAL_STATUSES or status == "visible_in_plex"
    )
    if not status_known and not has_notification:
        return LegacyRowClassification(
            source_id,
            LegacyDisposition.QUARANTINED,
            LegacyReason.INVALID_STATUS.value,
            media_type,
            None,
            None,
            (),
            fingerprint,
            source_table=source_table,
            source_name=source_name,
        )

    destination, destination_valid = _destination(row)
    identity, identity_reason = _identity(row, media_type)
    seasons, season_reason = _decode_seasons(
        _lookup(row, "season_numbers", "seasons", "seasons_json")
    )

    # Terminal rows are retained as history, not re-enqueued.  The only
    # fields required for this decision are a known type/status and source ID;
    # malformed season/identity data must not turn an already terminal row
    # into a new request.
    if terminal:
        return LegacyRowClassification(
            source_id,
            LegacyDisposition.TERMINALLY_ARCHIVED,
            LegacyReason.NOTIFIED.value
            if has_notification
            else LegacyReason.TERMINAL_STATUS.value,
            media_type,
            destination if destination_valid else None,
            identity,
            seasons or (),
            fingerprint,
            terminal=True,
            source_table=source_table,
            source_name=source_name,
        )

    _, year_valid = _legacy_year(row)
    if not year_valid:
        return LegacyRowClassification(
            source_id,
            LegacyDisposition.QUARANTINED,
            LegacyReason.INVALID_YEAR.value,
            media_type,
            None,
            None,
            (),
            fingerprint,
            source_table=source_table,
            source_name=source_name,
        )

    # A malformed status was handled above.  Non-pending statuses (for
    # example failed/blocked) are not safe deletion classes even if a schema
    # from a future legacy build introduced them.
    if status not in _PENDING_STATUSES:
        return LegacyRowClassification(
            source_id,
            LegacyDisposition.QUARANTINED,
            LegacyReason.INVALID_STATUS.value,
            media_type,
            destination if destination_valid else None,
            identity,
            seasons or (),
            fingerprint,
            source_table=source_table,
            source_name=source_name,
        )

    _, arr_present, arr_valid = _legacy_arr_state(row, media_type)
    if not arr_valid:
        return LegacyRowClassification(
            source_id,
            LegacyDisposition.QUARANTINED,
            LegacyReason.IDENTITY_MALFORMED.value,
            media_type,
            None,
            identity,
            seasons or (),
            fingerprint,
            source_table=source_table,
            source_name=source_name,
        )
    if arr_present and identity is None:
        # An Arr/provider record is an in-flight external obligation.  It is
        # never safe to treat the missing canonical TMDB/TVDB identity as a
        # deletion candidate.
        return LegacyRowClassification(
            source_id,
            LegacyDisposition.QUARANTINED,
            LegacyReason.PROVIDER_STATE_WITHOUT_IDENTITY.value,
            media_type,
            destination if destination_valid else None,
            None,
            seasons or (),
            fingerprint,
            source_table=source_table,
            source_name=source_name,
        )

    # Field-shape problems are ambiguous, not approved deletion classes.
    if not destination_valid:
        destination_values = [
            _lookup(row, key)
            for key in (
                "requested_by_chat_id",
                "destination",
                "destination_id",
                "chat_id",
            )
        ]
        return LegacyRowClassification(
            source_id,
            LegacyDisposition.QUARANTINED,
            LegacyReason.IDENTITY_MALFORMED.value
            if any(value is not None for value in destination_values)
            else LegacyReason.MISSING_DESTINATION.value,
            media_type,
            None,
            identity,
            seasons or (),
            fingerprint,
            source_table=source_table,
            source_name=source_name,
        )

    if season_reason is not None:
        return LegacyRowClassification(
            source_id,
            LegacyDisposition.QUARANTINED,
            season_reason,
            media_type,
            destination,
            identity,
            (),
            fingerprint,
            source_table=source_table,
            source_name=source_name,
        )

    # A movie's NULL season scope is normal.  A non-NULL list is not silently
    # discarded because it could be a corrupted series/movie type boundary.
    if media_type == "movie":
        if seasons is not None:
            return LegacyRowClassification(
                source_id,
                LegacyDisposition.QUARANTINED,
                LegacyReason.MOVIE_SEASONS_NOT_NULL.value,
                media_type,
                destination,
                identity,
                seasons,
                fingerprint,
                source_table=source_table,
                source_name=source_name,
            )
        if identity_reason in {
            LegacyReason.IDENTITY_MALFORMED.value,
            LegacyReason.IDENTITY_TYPE_MISMATCH.value,
            LegacyReason.IDENTITY_CONFLICT.value,
        }:
            return LegacyRowClassification(
                source_id,
                LegacyDisposition.QUARANTINED,
                identity_reason,
                media_type,
                destination,
                None,
                (),
                fingerprint,
                source_table=source_table,
                source_name=source_name,
            )
        if destination is None:
            return LegacyRowClassification(
                source_id,
                LegacyDisposition.DELETE_CANDIDATE,
                LegacyReason.MISSING_DESTINATION.value,
                media_type,
                None,
                identity,
                (),
                fingerprint,
                delete_candidate=True,
                source_table=source_table,
                source_name=source_name,
            )
        if identity is None:
            return LegacyRowClassification(
                source_id,
                LegacyDisposition.DELETE_CANDIDATE,
                LegacyReason.MISSING_IDENTITY.value,
                media_type,
                destination,
                None,
                (),
                fingerprint,
                delete_candidate=True,
                source_table=source_table,
                source_name=source_name,
            )
        return LegacyRowClassification(
            source_id,
            LegacyDisposition.MIGRATED,
            LegacyReason.VALID_PENDING_MOVIE.value,
            media_type,
            destination,
            identity,
            (),
            fingerprint,
            source_table=source_table,
            source_name=source_name,
        )

    # Series requires a non-empty explicit list.  ``[]`` and JSON ``null``
    # are absent scope and are eligible for the narrow approved deletion
    # class; malformed/non-negative violations were quarantined above.
    if seasons is None or not seasons:
        if identity_reason in {
            LegacyReason.IDENTITY_MALFORMED.value,
            LegacyReason.IDENTITY_TYPE_MISMATCH.value,
            LegacyReason.IDENTITY_CONFLICT.value,
        }:
            return LegacyRowClassification(
                source_id,
                LegacyDisposition.QUARANTINED,
                identity_reason,
                media_type,
                destination,
                None,
                (),
                fingerprint,
                source_table=source_table,
                source_name=source_name,
            )
        if destination is None:
            return LegacyRowClassification(
                source_id,
                LegacyDisposition.DELETE_CANDIDATE,
                LegacyReason.MISSING_DESTINATION.value,
                media_type,
                None,
                identity,
                (),
                fingerprint,
                delete_candidate=True,
                source_table=source_table,
                source_name=source_name,
            )
        if identity is None:
            return LegacyRowClassification(
                source_id,
                LegacyDisposition.DELETE_CANDIDATE,
                LegacyReason.MISSING_IDENTITY.value,
                media_type,
                destination,
                None,
                (),
                fingerprint,
                delete_candidate=True,
                source_table=source_table,
                source_name=source_name,
            )
        return LegacyRowClassification(
            source_id,
            LegacyDisposition.DELETE_CANDIDATE,
            LegacyReason.MISSING_SERIES_SEASONS.value,
            media_type,
            destination,
            identity,
            (),
            fingerprint,
            delete_candidate=True,
            source_table=source_table,
            source_name=source_name,
        )

    if identity_reason in {
        LegacyReason.IDENTITY_MALFORMED.value,
        LegacyReason.IDENTITY_TYPE_MISMATCH.value,
        LegacyReason.IDENTITY_CONFLICT.value,
    }:
        return LegacyRowClassification(
            source_id,
            LegacyDisposition.QUARANTINED,
            identity_reason,
            media_type,
            destination,
            None,
            seasons,
            fingerprint,
            source_table=source_table,
            source_name=source_name,
        )
    if destination is None:
        return LegacyRowClassification(
            source_id,
            LegacyDisposition.DELETE_CANDIDATE,
            LegacyReason.MISSING_DESTINATION.value,
            media_type,
            None,
            identity,
            seasons,
            fingerprint,
            delete_candidate=True,
            source_table=source_table,
            source_name=source_name,
        )
    if identity is None:
        return LegacyRowClassification(
            source_id,
            LegacyDisposition.DELETE_CANDIDATE,
            LegacyReason.MISSING_IDENTITY.value,
            media_type,
            destination,
            None,
            seasons,
            fingerprint,
            delete_candidate=True,
            source_table=source_table,
            source_name=source_name,
        )
    return LegacyRowClassification(
        source_id,
        LegacyDisposition.MIGRATED,
        LegacyReason.VALID_PENDING_SERIES.value,
        media_type,
        destination,
        identity,
        seasons,
        fingerprint,
        source_table=source_table,
        source_name=source_name,
    )


def _readonly_uri(path: Path) -> str:
    return f"file:{quote(str(path), safe='/\\:')}?mode=ro"


def _rows_from_connection(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[Mapping[str, Any], ...]:
    table = _identifier(table, "source table")
    try:
        connection.row_factory = sqlite3.Row
        columns = connection.execute(
            f"PRAGMA table_info({_quote_identifier(table, 'source table')})"
        ).fetchall()
        if not columns:
            raise LegacySchemaError(f"legacy table {table!r} does not exist")
        column_names = {str(column[1]) for column in columns}
        order = _quote_identifier("id" if "id" in column_names else "rowid")
        result = connection.execute(
            f"SELECT * FROM {_quote_identifier(table, 'source table')} ORDER BY {order}"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise LegacySchemaError(f"cannot read legacy table {table!r}: {exc}") from exc
    return tuple(dict(row) for row in result)


def _schema_fingerprint(connection: sqlite3.Connection, table: str) -> str:
    table = _identifier(table, "source table")
    rows = connection.execute(
        "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger', 'view') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    table_info = connection.execute(
        f"PRAGMA table_info({_quote_identifier(table, 'source table')})"
    ).fetchall()
    payload = [tuple(row) for row in rows]
    payload.append(("table_info", table, [tuple(row) for row in table_info]))
    return hashlib.sha256(
        json.dumps(_json_safe(payload), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _source_metadata(
    connection: sqlite3.Connection,
    table: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    path: Path | None,
) -> tuple[str | None, str, str]:
    schema = _schema_fingerprint(connection, table)
    data = _rows_fingerprint(rows)
    identity = (
        _source_identity(
            path,
            source_table=table,
            schema_fingerprint=schema,
            data_fingerprint=data,
        )
        if path is not None
        else None
    )
    return identity, schema, data


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def create_verified_legacy_backup(
    source: Any,
    destination: str | Path,
    *,
    source_name: str = DEFAULT_SOURCE_NAME,
    source_table: str = LEGACY_TABLE,
) -> LegacyBackupArtifact:
    """Create a verified immutable SQLite backup without touching the source.

    Only a filesystem SQLite source is accepted: a synthetic mapping or a
    caller-owned connection cannot provide a durable rollback artifact with a
    stable source identity.  Existing destination files are never replaced.
    """

    source_path = _source_path_value(source)
    if source_path is None:
        raise LegacyMigrationError(
            "verified backup requires a filesystem SQLite source"
        )
    source_path = source_path.resolve()
    if (
        not source_path.exists()
        or source_path.is_symlink()
        or not source_path.is_file()
    ):
        raise LegacyMigrationError(
            "verified backup source must be an existing SQLite file"
        )
    destination_path = Path(destination).resolve()
    if source_path == destination_path:
        raise LegacyMigrationError("backup destination must differ from source")
    if destination_path.exists() or destination_path.is_symlink():
        raise LegacyMigrationError("backup destination already exists")
    source_name = _source_name(source_name)
    source_table = _identifier(source_table, "source table")
    destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.",
        suffix=".tmp",
        dir=str(destination_path.parent),
    )
    os.close(temporary_fd)
    temporary_path = Path(temporary_name)
    source_connection = sqlite3.connect(
        str(source_path), isolation_level=None, timeout=5.0
    )
    snapshot_connection: sqlite3.Connection | None = None
    backup_connection = sqlite3.connect(str(temporary_path), isolation_level=None)
    committed_source = False
    published = False
    try:
        source_connection.execute("BEGIN IMMEDIATE")
        rows = _rows_from_connection(source_connection, source_table)
        source_identity, schema, data = _source_metadata(
            source_connection, source_table, rows, path=source_path
        )
        quick = str(source_connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(
            source_connection.execute("PRAGMA integrity_check").fetchone()[0]
        )
        source_foreign_keys = source_connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if quick.lower() != "ok" or integrity.lower() != "ok" or source_foreign_keys:
            raise LegacyMigrationError("source failed SQLite integrity verification")
        # Python's sqlite3 backup API can block when called on the same
        # connection that owns an IMMEDIATE transaction.  Keep that writer
        # fence open, but copy from a second read-only connection observing
        # the fenced snapshot.
        snapshot_connection = sqlite3.connect(
            _readonly_uri(source_path), uri=True, isolation_level=None
        )
        snapshot_connection.backup(backup_connection)
        backup_connection.commit()
        backup_quick = str(
            backup_connection.execute("PRAGMA quick_check").fetchone()[0]
        )
        backup_integrity = str(
            backup_connection.execute("PRAGMA integrity_check").fetchone()[0]
        )
        backup_foreign_keys = backup_connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        backup_rows = _rows_from_connection(backup_connection, source_table)
        backup_schema = _schema_fingerprint(backup_connection, source_table)
        backup_data = _rows_fingerprint(backup_rows)
        if (
            backup_quick.lower() != "ok"
            or backup_integrity.lower() != "ok"
            or backup_foreign_keys
            or backup_schema != schema
            or backup_data != data
        ):
            raise LegacyMigrationError("SQLite backup verification failed")
        backup_connection.close()
        backup_connection = None  # type: ignore[assignment]
        os.chmod(temporary_path, 0o400)
        with temporary_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination_path)
        published = True
        directory_fd = os.open(str(destination_path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.chmod(destination_path, 0o400)
        digest, size = _sha256_file(destination_path)
        # The IMMEDIATE source fence must still describe the artifact before
        # it is published; otherwise approval could authorize a stale copy.
        current_rows = _rows_from_connection(source_connection, source_table)
        current_identity, current_schema, current_data = _source_metadata(
            source_connection, source_table, current_rows, path=source_path
        )
        if (current_identity, current_schema, current_data) != (
            source_identity,
            schema,
            data,
        ):
            raise LegacyMigrationError("source changed while backup was verified")
        committed_source = True
        return LegacyBackupArtifact(
            str(source_path),
            source_name,
            source_table,
            source_identity or "",
            schema,
            data,
            len(rows),
            str(destination_path),
            digest,
            size,
            integrity_check=backup_integrity,
            quick_check=backup_quick,
        )
    finally:
        if backup_connection is not None:
            backup_connection.close()
        if snapshot_connection is not None:
            snapshot_connection.close()
        if not committed_source:
            try:
                source_connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        else:
            try:
                source_connection.execute("COMMIT")
            except sqlite3.Error:
                pass
        source_connection.close()
        if temporary_path.exists():
            temporary_path.unlink()
        if published and not committed_source and destination_path.exists():
            destination_path.unlink()


def verify_legacy_backup(
    artifact: LegacyBackupArtifact,
    *,
    source: Any | None = None,
) -> None:
    """Fail closed if a backup artifact or its source identity changed."""

    if not artifact.verified or not artifact.immutable:
        raise LegacyMigrationError("backup artifact is not verified and immutable")
    backup_path = Path(artifact.backup_path)
    if not backup_path.exists() or backup_path.is_symlink():
        raise LegacyMigrationError("backup artifact is missing or is a symlink")
    if backup_path.stat().st_mode & 0o222:
        raise LegacyMigrationError("backup artifact is writable")
    digest, size = _sha256_file(backup_path)
    if digest != artifact.backup_sha256 or size != artifact.backup_size_bytes:
        raise LegacyMigrationError("backup artifact hash or size changed")
    connection = sqlite3.connect(_readonly_uri(backup_path), uri=True)
    try:
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        rows = _rows_from_connection(connection, artifact.source_table)
        schema = _schema_fingerprint(connection, artifact.source_table)
        data = _rows_fingerprint(rows)
    finally:
        connection.close()
    if (
        quick.lower() != "ok"
        or integrity.lower() != "ok"
        or foreign_keys
        or schema != artifact.source_schema_fingerprint
        or data != artifact.source_data_fingerprint
        or len(rows) != artifact.source_rows
    ):
        raise LegacyMigrationError("backup artifact integrity or source hash mismatch")
    if source is not None:
        source_path = _source_path_value(source)
        if source_path is None:
            raise LegacyMigrationError(
                "approval source must be a filesystem SQLite source"
            )
        if source_path.resolve() != Path(artifact.source_database).resolve():
            raise LegacyMigrationError(
                "approval source path does not match backup identity"
            )
        live = _source_rows(
            source_path,
            source_name=artifact.source_name,
            source_table=artifact.source_table,
            read_only=True,
        )
        if (
            live.source_identity != artifact.source_identity
            or live.source_schema_fingerprint != artifact.source_schema_fingerprint
            or live.source_data_fingerprint != artifact.source_data_fingerprint
        ):
            raise LegacyMigrationError(
                "source identity or fingerprint no longer matches backup"
            )


def _source_path_value(source: Any) -> Path | None:
    if isinstance(source, Database):
        return None if source.database == ":memory:" else Path(source.database)
    if isinstance(source, (str, Path)):
        return Path(source)
    return None


# Short aliases keep the public API discoverable for operators and tests.
create_legacy_backup = create_verified_legacy_backup
verify_backup_artifact = verify_legacy_backup
approve_legacy_dry_run = LegacyMigrationApproval.from_dry_run


def _coerce_rows(
    rows: Iterable[Mapping[str, Any]] | Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    if isinstance(rows, Mapping):
        return (rows,)
    result: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError("legacy rows must be mappings")
        result.append(row)
    return tuple(result)


def _source_rows(
    source: Any,
    *,
    source_name: str,
    source_table: str,
    read_only: bool,
) -> _SourceRows:
    source_name = _source_name(source_name)
    source_table = _identifier(source_table, "source table")
    if isinstance(source, sqlite3.Connection):
        rows = _rows_from_connection(source, source_table)
        _identity_value, schema, data = _source_metadata(
            source, source_table, rows, path=None
        )
        return _SourceRows(
            rows,
            source_name,
            source_table,
            source_connection=source,
            delete_supported=True,
            source_schema_fingerprint=schema,
            source_data_fingerprint=data,
        )
    if isinstance(source, Database):
        if source.database == ":memory:":
            with source.connection() as connection:
                rows = _rows_from_connection(connection, source_table)
            return _SourceRows(rows, source_name, source_table)
        path = Path(source.database)
        if read_only:
            connection = sqlite3.connect(_readonly_uri(path), uri=True)
            try:
                rows = _rows_from_connection(connection, source_table)
                identity, schema, data = _source_metadata(
                    connection, source_table, rows, path=path
                )
            finally:
                connection.close()
            return _SourceRows(
                rows,
                source_name,
                source_table,
                source_path=path,
                delete_supported=True,
                source_identity=identity,
                source_schema_fingerprint=schema,
                source_data_fingerprint=data,
            )
        connection = sqlite3.connect(str(path))
        rows = _rows_from_connection(connection, source_table)
        identity, schema, data = _source_metadata(
            connection, source_table, rows, path=path
        )
        connection.close()
        return _SourceRows(
            rows,
            source_name,
            source_table,
            source_path=path,
            delete_supported=True,
            source_identity=identity,
            source_schema_fingerprint=schema,
            source_data_fingerprint=data,
        )
    if isinstance(source, (str, Path)):
        path = Path(source)
        if read_only:
            connection = sqlite3.connect(_readonly_uri(path), uri=True)
            try:
                rows = _rows_from_connection(connection, source_table)
                identity, schema, data = _source_metadata(
                    connection, source_table, rows, path=path
                )
            finally:
                connection.close()
            return _SourceRows(
                rows,
                source_name,
                source_table,
                source_path=path,
                delete_supported=True,
                source_identity=identity,
                source_schema_fingerprint=schema,
                source_data_fingerprint=data,
            )
        connection = sqlite3.connect(str(path))
        rows = _rows_from_connection(connection, source_table)
        identity, schema, data = _source_metadata(
            connection, source_table, rows, path=path
        )
        connection.close()
        return _SourceRows(
            rows,
            source_name,
            source_table,
            source_path=path,
            delete_supported=True,
            source_identity=identity,
            source_schema_fingerprint=schema,
            source_data_fingerprint=data,
        )

    rows = _coerce_rows(source)
    # A synthetic iterable is intentionally read-only and has no deletion
    # callback.  Keep its order unless every row has an ID, in which case
    # source-ID ordering makes fixture results deterministic across callers.
    if all(_source_row_id(row) is not None for row in rows):

        def row_order(row: Mapping[str, Any]) -> tuple[int, int | str, str]:
            source_id = _source_row_id(row) or ""
            try:
                return (0, int(source_id), _fingerprint(row))
            except ValueError:
                return (1, source_id, _fingerprint(row))

        rows = tuple(sorted(rows, key=row_order))
    return _SourceRows(
        rows,
        source_name,
        source_table,
        source_schema_fingerprint=hashlib.sha256(b"synthetic-schema").hexdigest(),
        source_data_fingerprint=_rows_fingerprint(rows),
    )


def dry_run_legacy_migration(
    source: Any,
    *,
    source_name: str = DEFAULT_SOURCE_NAME,
    source_table: str = LEGACY_TABLE,
) -> LegacyDryRunReport:
    """Classify all legacy rows without mutating the source or target."""

    source_rows = _source_rows(
        source,
        source_name=source_name,
        source_table=source_table,
        read_only=True,
    )
    classified = tuple(
        classify_legacy_row(
            row,
            source_name=source_rows.source_name,
            source_table=source_rows.source_table,
        )
        for row in source_rows.rows
    )
    return LegacyDryRunReport(
        classified,
        source_rows.source_name,
        source_rows.source_table,
        source_rows.source_identity,
        source_rows.source_schema_fingerprint,
        source_rows.source_data_fingerprint,
    )


def _recheck_source_snapshot(
    source_rows: _SourceRows, *, ignore_file_identity: bool = False
) -> None:
    """Re-read the source immediately before/inside the target transaction."""

    if source_rows.source_path is not None:
        current = _source_rows(
            source_rows.source_path,
            source_name=source_rows.source_name,
            source_table=source_rows.source_table,
            read_only=True,
        )
        if (
            (
                not ignore_file_identity
                and current.source_identity != source_rows.source_identity
            )
            or current.source_schema_fingerprint
            != source_rows.source_schema_fingerprint
            or current.source_data_fingerprint != source_rows.source_data_fingerprint
        ):
            raise LegacyMigrationError(
                f"{LegacyReason.SOURCE_SNAPSHOT_CHANGED.value}: source changed after read"
            )
        return
    if source_rows.source_connection is not None:
        rows = _rows_from_connection(
            source_rows.source_connection, source_rows.source_table
        )
        if _rows_fingerprint(rows) != source_rows.source_data_fingerprint:
            raise LegacyMigrationError(
                f"{LegacyReason.SOURCE_SNAPSHOT_CHANGED.value}: source changed after read"
            )


def _surrogate_classification(
    classification: LegacyRowClassification, ordinal: int
) -> LegacyRowClassification:
    """Give malformed rows a deterministic accounting key without deletion."""

    if classification.source_row_id is not None:
        return classification
    return replace(
        classification,
        source_row_id=(f"surrogate:{classification.source_fingerprint}:{ordinal}"),
    )


def _target_path(target: Any) -> Path | None:
    if isinstance(target, Database):
        return None if target.database == ":memory:" else Path(target.database)
    if isinstance(target, (str, Path)):
        return Path(target)
    if isinstance(target, sqlite3.Connection):
        try:
            row = target.execute("PRAGMA database_list").fetchone()
        except sqlite3.Error:
            return None
        if row is not None and row[2]:
            return Path(str(row[2]))
    return None


@contextmanager
def _target_transaction(target: Any) -> Iterator[sqlite3.Connection]:
    if isinstance(target, Database):
        with target.transaction() as connection:
            yield connection
        return
    if isinstance(target, sqlite3.Connection):
        connection = target
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        savepoint = f"legacy_import_{id(connection)}"
        nested = connection.in_transaction
        if nested:
            connection.execute(f"SAVEPOINT {savepoint}")
        else:
            connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            if nested:
                connection.execute(f"ROLLBACK TO {savepoint}")
                connection.execute(f"RELEASE {savepoint}")
            else:
                connection.execute("ROLLBACK")
            raise
        else:
            if nested:
                connection.execute(f"RELEASE {savepoint}")
            else:
                connection.execute("COMMIT")
        return
    if isinstance(target, (str, Path)):
        connection = sqlite3.connect(str(target), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
        finally:
            connection.close()
        return
    raise TypeError("target must be Database, sqlite3.Connection, or a database path")


@contextmanager
def _source_fence(
    source_rows: _SourceRows,
    target: Any,
    target_path: Path | None,
) -> Iterator[None]:
    """Hold an IMMEDIATE source lock while canonical rows are assembled."""

    source_path = source_rows.source_path
    source_connection = source_rows.source_connection
    connection_path: Path | None = None
    if source_connection is not None:
        try:
            database_row = source_connection.execute("PRAGMA database_list").fetchone()
            if database_row is not None and database_row[2]:
                connection_path = Path(str(database_row[2]))
        except sqlite3.Error:
            connection_path = None
    same_database = (
        source_path is not None
        and target_path is not None
        and source_path.resolve() == target_path.resolve()
    ) or (
        source_path is None
        and connection_path is not None
        and target_path is not None
        and connection_path.resolve() == target_path.resolve()
    )
    if same_database or source_path is None:
        connection = source_connection
        owns_transaction = False
        if connection is not None and connection is not target:
            if connection.in_transaction:
                raise LegacyMigrationError(
                    "caller-owned source connection is already in a transaction"
                )
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "readonly" not in str(exc).lower():
                    raise
                # A read-only caller-owned connection can still support a
                # non-destructive import; verified deletion approval requires
                # a filesystem source and never reaches this path.
                yield
                return
            owns_transaction = True
            current = _rows_from_connection(connection, source_rows.source_table)
            if _rows_fingerprint(current) != source_rows.source_data_fingerprint:
                connection.execute("ROLLBACK")
                raise LegacyMigrationError(
                    f"{LegacyReason.SOURCE_SNAPSHOT_CHANGED.value}: source changed"
                )
        try:
            yield
        except BaseException:
            if (
                owns_transaction
                and connection is not None
                and connection.in_transaction
            ):
                connection.execute("ROLLBACK")
            raise
        else:
            if (
                owns_transaction
                and connection is not None
                and connection.in_transaction
            ):
                connection.execute("COMMIT")
        return
    if (
        not source_path.exists()
        or source_path.is_symlink()
        or not source_path.is_file()
    ):
        raise LegacyMigrationError("source disappeared before transaction fence")
    fence = sqlite3.connect(str(source_path), isolation_level=None, timeout=5.0)
    try:
        fence.execute("BEGIN IMMEDIATE")
        current = _rows_from_connection(fence, source_rows.source_table)
        if (
            _rows_fingerprint(current) != source_rows.source_data_fingerprint
            or _schema_fingerprint(fence, source_rows.source_table)
            != source_rows.source_schema_fingerprint
            or _source_identity(
                source_path,
                source_table=source_rows.source_table,
                schema_fingerprint=source_rows.source_schema_fingerprint or "",
                data_fingerprint=source_rows.source_data_fingerprint or "",
            )
            != source_rows.source_identity
        ):
            fence.execute("ROLLBACK")
            raise LegacyMigrationError(
                f"{LegacyReason.SOURCE_SNAPSHOT_CHANGED.value}: source changed"
            )
        try:
            yield
        except BaseException:
            fence.execute("ROLLBACK")
            raise
        else:
            fence.execute("COMMIT")
    finally:
        fence.close()


@contextmanager
def _migration_transaction(
    target: Any,
    source_rows: _SourceRows,
    target_path: Path | None,
) -> Iterator[sqlite3.Connection]:
    with _source_fence(source_rows, target, target_path):
        with _target_transaction(target) as connection:
            yield connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    table = _identifier(table, "table name")
    rows = connection.execute(
        f"PRAGMA table_info({_quote_identifier(table, 'table name')})"
    ).fetchall()
    if not rows:
        raise LegacySchemaError(f"target table {table!r} is missing")
    return {str(row[1]) for row in rows}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    table = _identifier(table, "table name")
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _column_not_null(connection: sqlite3.Connection, table: str, column: str) -> bool:
    for row in connection.execute(
        f"PRAGMA table_info({_quote_identifier(table, 'table name')})"
    ).fetchall():
        if str(row[1]) == column:
            return bool(row[3])
    raise LegacySchemaError(f"target table {table!r} lacks column {column!r}")


def _request_table(connection: sqlite3.Connection) -> str:
    """Return only the canonical normalized request table."""

    if _table_exists(connection, "requests"):
        return "requests"
    raise LegacySchemaError(
        "target must use canonical requests table; legacy media_requests is source-only"
    )


def _validate_target_schema(connection: sqlite3.Connection) -> None:
    """Fail closed unless the checked-in canonical ledger is fully installed."""

    if not _table_exists(connection, "schema_migrations"):
        raise LegacySchemaError("target schema migration ledger is missing")
    migration_row = connection.execute(
        "SELECT MAX(version) FROM schema_migrations WHERE completed_at IS NOT NULL"
    ).fetchone()
    if migration_row is None or migration_row[0] is None or int(migration_row[0]) < 3:
        raise LegacySchemaError("target schema version must be at least 3")
    required_tables = (
        "requests",
        "subscriptions",
        "subscription_units",
        "quarantined_records",
        "migration_lineage",
        "migration_expansions",
        "legacy_source_mappings",
    )
    for table in required_tables:
        if not _table_exists(connection, table):
            raise LegacySchemaError(f"canonical target table {table!r} is missing")
    required_columns: dict[str, set[str]] = {
        "requests": {
            "id",
            "request_key",
            "requested_by_user_id",
            "requested_by_chat_id",
            "media_type",
            "provider_id",
            "title",
            "mode",
            "status",
            "provider_item_id",
            "arr_id",
            "payload_json",
        },
        "subscriptions": {
            "id",
            "request_id",
            "user_id",
            "chat_id",
            "destination",
            "notification_class",
            "media_type",
            "provider_id",
            "season_number",
            "mode",
            "generation",
            "baseline",
            "status",
        },
        "subscription_units": {
            "id",
            "subscription_id",
            "logical_unit_key",
            "unit_type",
            "provider_id",
            "season_number",
        },
        "migration_lineage": {
            "id",
            "migration_id",
            "source_table",
            "source_row_id",
            "source_fingerprint",
            "disposition",
            "reason_code",
            "expansion_count",
        },
        "legacy_source_mappings": {
            "source_name",
            "source_table",
            "source_row_id",
            "source_fingerprint",
            "disposition",
            "reason",
            "details_json",
        },
    }
    for table, columns in required_columns.items():
        missing = columns - _table_columns(connection, table)
        if missing:
            raise LegacySchemaError(
                f"canonical target table {table!r} lacks columns: {sorted(missing)!r}"
            )


def _ensure_lineage_tables(connection: sqlite3.Connection) -> None:
    _validate_target_schema(connection)


def _mapping_row(
    connection: sqlite3.Connection,
    classification: LegacyRowClassification,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM legacy_source_mappings
        WHERE source_name = ? AND source_table = ? AND source_row_id = ?
        """,
        (
            classification.source_name,
            classification.source_table,
            classification.source_row_id,
        ),
    ).fetchone()


def _mapping_supports_delete_candidate(connection: sqlite3.Connection) -> bool:
    """Check the checked-in mapping constraint without rewriting migrations."""

    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'legacy_source_mappings'"
    ).fetchone()
    return row is not None and "delete_candidate" in str(row[0]).lower()


def _mapping_accounting_disposition(row: Any) -> str:
    physical = str(row["disposition"])
    if physical != LegacyDisposition.QUARANTINED.value:
        return physical
    try:
        details = json.loads(row["details_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        details = {}
    if isinstance(details, dict) and details.get("accounting_disposition"):
        return str(details["accounting_disposition"])
    return physical


def _safe_key(value: str) -> str:
    result = _SAFE_KEY.sub("_", value.strip())
    return result.strip("._") or "legacy"


def _request_key(classification: LegacyRowClassification) -> str:
    canonical = json.dumps(
        {
            "source_name": classification.source_name,
            "source_table": classification.source_table,
            "source_row_id": classification.source_row_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:32]
    return ":".join(
        (
            "legacy",
            _safe_key(classification.source_name),
            _safe_key(classification.source_table),
            digest,
        )
    )


def _identity_values(identity: LegacyIdentity | None) -> dict[str, Any]:
    if identity is None:
        return {}
    return {
        "tmdb_id": identity.tmdb_id,
        "tvdb_id": identity.tvdb_id,
        "imdb_id": identity.imdb_id,
        "external_provider": identity.provider,
        "external_id": identity.provider_id,
    }


def _provider_identity(identity: LegacyIdentity) -> tuple[str, str]:
    if identity.media_type == "movie":
        if identity.tmdb_id is not None:
            return "tmdb", str(identity.tmdb_id)
        if identity.imdb_id is not None:
            return "imdb", identity.imdb_id
        if identity.tvdb_id is not None:
            return "tvdb", str(identity.tvdb_id)
    else:
        if identity.tvdb_id is not None:
            return "tvdb", str(identity.tvdb_id)
        if identity.tmdb_id is not None:
            return "tmdb", str(identity.tmdb_id)
        if identity.imdb_id is not None:
            return "imdb", identity.imdb_id
    if identity.provider is not None and identity.provider_id is not None:
        return identity.provider, identity.provider_id
    raise LegacyMigrationError("stable identity has no provider ID")


def _legacy_arr_id(source_row: Mapping[str, Any], media_type: str) -> int | None:
    names = (
        ("radarr_movie_id", "arr_id", "provider_item_id")
        if media_type == "movie"
        else ("sonarr_series_id", "arr_id", "provider_item_id")
    )
    values = _lookup_values(source_row, *names)
    if not values:
        return None
    parsed: list[int] = []
    for value in values:
        item, valid = _positive_id(value)
        if not valid or item is None:
            return None
        parsed.append(item)
    if len(set(parsed)) != 1:
        return None
    return parsed[0]


def _legacy_arr_state(
    source_row: Mapping[str, Any], media_type: str
) -> tuple[int | None, bool, bool]:
    names = (
        ("radarr_movie_id", "arr_id", "provider_item_id")
        if media_type == "movie"
        else ("sonarr_series_id", "arr_id", "provider_item_id")
    )
    values = _lookup_values(source_row, *names)
    if not values:
        return None, False, True
    arr_id = _legacy_arr_id(source_row, media_type)
    return arr_id, True, arr_id is not None


def _legacy_status(source_row: Mapping[str, Any]) -> str:
    status, known = _status(source_row)
    if not known or status is None:
        return "requested"
    return status


def _target_seasons(row: Mapping[str, Any]) -> tuple[int, ...]:
    keys = set(row.keys())  # type: ignore[attr-defined]
    value = next(
        (
            row[name]
            for name in ("seasons_json", "season_numbers", "seasons")
            if name in keys
        ),
        None,
    )
    if value is None:
        return ()
    decoded, reason = _decode_seasons(value)
    return decoded or () if reason is None else ()


def _find_equivalent_request(
    connection: sqlite3.Connection,
    classification: LegacyRowClassification,
    identity: LegacyIdentity,
) -> int | None:
    table = _request_table(connection)
    columns = _table_columns(connection, table)
    destination_column = "chat_id" if "chat_id" in columns else "requested_by_chat_id"
    required = {"id", "media_type", destination_column}
    if not required <= columns:
        raise LegacySchemaError(f"target {table} lacks migration columns")
    if classification.media_type == "series" and not (
        {"seasons_json", "season_numbers", "seasons"} & columns
    ):
        raise LegacySchemaError(f"target {table} lacks series season columns")
    candidates = connection.execute(
        f"SELECT * FROM {_quote_identifier(table, 'target table')} "
        f"WHERE {_quote_identifier('media_type')} = ? "
        f"AND {_quote_identifier(destination_column)} = ? "
        f"ORDER BY {_quote_identifier('id')}",
        (classification.media_type, classification.destination),
    ).fetchall()
    source_aliases = dict(identity.aliases)
    source_seasons = (
        classification.seasons if classification.media_type == "series" else ()
    )
    for candidate in candidates:
        candidate_identity: dict[str, str] = {}
        for provider, column in (
            ("tmdb", "tmdb_id"),
            ("tvdb", "tvdb_id"),
            ("imdb", "imdb_id"),
        ):
            if column in columns and candidate[column] is not None:
                candidate_identity[provider] = str(candidate[column]).lower()
        if (
            "external_provider" in columns
            and "external_id" in columns
            and candidate["external_provider"] is not None
            and candidate["external_id"] is not None
        ):
            candidate_identity[str(candidate["external_provider"]).lower()] = str(
                candidate["external_id"]
            ).lower()
        if not candidate_identity:
            continue
        if (
            identity.provider is not None
            and "external_provider" in columns
            and candidate["external_provider"] is not None
            and str(candidate["external_provider"]).lower() != identity.provider
        ):
            continue
        overlap = set(source_aliases) & set(candidate_identity)
        if not overlap:
            continue
        if any(
            source_aliases[name].lower() != candidate_identity[name] for name in overlap
        ):
            # A different provider identity for the same destination is a
            # distinct request, not a conflict.  Source-row conflicts are
            # rejected earlier by ``_identity``; do not let an unrelated
            # request in the same chat quarantine this one.
            continue
        if (
            classification.media_type == "series"
            and _target_seasons(candidate) != source_seasons
        ):
            continue
        return int(candidate["id"])
    return None


def _reconcile_request_state(
    connection: sqlite3.Connection,
    request_id: int,
    source_row: Mapping[str, Any],
    classification: LegacyRowClassification,
) -> None:
    """Fill missing Arr/provider state without regressing a canonical row."""

    table = _request_table(connection)
    columns = _table_columns(connection, table)
    current = connection.execute(
        f"SELECT * FROM {_quote_identifier(table)} WHERE id = ?", (request_id,)
    ).fetchone()
    if current is None:
        raise LegacyMigrationError("equivalent request disappeared during import")
    values: dict[str, Any] = {}
    arr_id = _legacy_arr_id(source_row, classification.media_type or "movie")
    if arr_id is not None:
        for column, value in (("provider_item_id", str(arr_id)), ("arr_id", arr_id)):
            if column in columns and current[column] is None:
                values[column] = value
    status = _legacy_status(source_row)
    if "status" in columns and current["status"] in {None, "requested", "pending"}:
        values["status"] = status
    if "payload_json" in columns:
        payload = current["payload_json"]
        try:
            parsed = json.loads(payload) if payload else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        parsed.update(
            {
                "legacy_status": status,
                "radarr_movie_id": _legacy_arr_id(source_row, "movie"),
                "sonarr_series_id": _legacy_arr_id(source_row, "series"),
            }
        )
        values["payload_json"] = json.dumps(parsed, sort_keys=True)
    if not values:
        return
    values["updated_at"] = utc_timestamp()
    assignments = ", ".join(f"{_quote_identifier(column)} = ?" for column in values)
    connection.execute(
        f"UPDATE {_quote_identifier(table)} SET {assignments} WHERE id = ?",
        (*values.values(), request_id),
    )


def _insert_dynamic(
    connection: sqlite3.Connection,
    table: str,
    values: Mapping[str, Any],
) -> int:
    columns = _table_columns(connection, table)
    selected = [
        (column, value) for column, value in values.items() if column in columns
    ]
    if not selected:
        raise LegacySchemaError(f"no compatible columns for target table {table!r}")
    names = ", ".join(
        _quote_identifier(column, "target column") for column, _ in selected
    )
    placeholders = ", ".join("?" for _ in selected)
    cursor = connection.execute(
        f"INSERT INTO {_quote_identifier(table, 'target table')} ({names}) VALUES ({placeholders})",
        tuple(value for _, value in selected),
    )
    if cursor.lastrowid is None:
        raise LegacyMigrationError(f"SQLite did not return inserted {table} ID")
    return int(cursor.lastrowid)


def _insert_or_get_item(
    connection: sqlite3.Connection,
    request_id: int,
    classification: LegacyRowClassification,
    identity: LegacyIdentity,
    season: int | None,
    *,
    terminal: bool,
) -> int:
    _table_columns(connection, "request_items")
    provider, external_id = _provider_identity(identity)
    media_item_type = "season" if classification.media_type == "series" else "movie"
    existing = connection.execute(
        """
        SELECT id FROM request_items
        WHERE request_id = ? AND media_type = ? AND season_number IS ?
        ORDER BY id LIMIT 1
        """,
        (request_id, media_item_type, season),
    ).fetchone()
    if existing is not None:
        return int(existing[0])
    values: dict[str, Any] = {
        "request_id": request_id,
        "media_type": media_item_type,
        "provider": provider,
        "external_id": external_id,
        "title": None,
        "season_number": season,
        "episode_number": None,
        "status": "fulfilled" if terminal else "pending",
        "metadata_json": json.dumps(
            {
                "source": classification.source_name,
                "source_table": classification.source_table,
                "source_row_id": classification.source_row_id,
            },
            sort_keys=True,
        ),
        "created_at": utc_timestamp(),
        "updated_at": utc_timestamp(),
    }
    return _insert_dynamic(connection, "request_items", values)


def _insert_or_get_subscription_units(
    connection: sqlite3.Connection,
    request_id: int,
    classification: LegacyRowClassification,
    identity: LegacyIdentity,
    source_row: Mapping[str, Any],
    *,
    terminal: bool,
) -> tuple[int, ...]:
    """Create independent requester subscriptions and derived units.

    This is used when ledger migration 0003 is present.  A terminal legacy
    row intentionally gets no subscription or delivery obligation.
    """

    if terminal or not (
        _table_exists(connection, "subscriptions")
        and _table_exists(connection, "subscription_units")
    ):
        return ()
    subscription_columns = _table_columns(connection, "subscriptions")
    _table_columns(connection, "subscription_units")
    provider, provider_id = _provider_identity(identity)
    legacy_user_value = _lookup(source_row, "requested_by_user_id", "user_id")
    legacy_user_id = _positive_id(legacy_user_value)[0]
    user_column = (
        "user_id" if "user_id" in subscription_columns else "requested_by_user_id"
    )
    chat_column = (
        "chat_id" if "chat_id" in subscription_columns else "requested_by_chat_id"
    )
    if (
        user_column not in subscription_columns
        or chat_column not in subscription_columns
    ):
        raise LegacySchemaError("subscriptions lacks direct legacy user/chat columns")
    if legacy_user_id is None and _column_not_null(
        connection, "subscriptions", user_column
    ):
        # A Telegram chat ID is not a user identity.  Never manufacture one
        # merely to satisfy a NOT NULL target column.
        raise LegacyMigrationError(LegacyReason.MISSING_REQUESTER_ID.value)
    seasons: tuple[int | None, ...] = (
        tuple(classification.seasons)
        if classification.media_type == "series"
        else (None,)
    )
    item_ids: list[int] = []
    for season in seasons:
        existing = connection.execute(
            f"""
            SELECT {_quote_identifier("id")} FROM {_quote_identifier("subscriptions")}
            WHERE {_quote_identifier(user_column)} = ?
              AND {_quote_identifier(chat_column)} = ?
              AND {_quote_identifier("provider_id")} = ?
              AND {_quote_identifier("media_type")} = ?
              AND {_quote_identifier("season_number")} IS ?
              AND {_quote_identifier("generation")} = 1
            ORDER BY {_quote_identifier("id")} LIMIT 1
            """,
            (
                legacy_user_id,
                classification.destination,
                provider_id,
                classification.media_type,
                season,
            ),
        ).fetchone()
        if existing is None:
            values = {
                "request_id": request_id,
                "user_id": legacy_user_id,
                "chat_id": classification.destination,
                "requested_by_user_id": legacy_user_id,
                "requested_by_chat_id": classification.destination,
                "requested_by_username": _bounded_text(
                    _lookup(source_row, "requested_by_username", "username"),
                    max_bytes=256,
                ),
                # RequestWorkflow uses the canonical chat ID as destination;
                # retaining it here keeps distinct Telegram chats distinct in
                # grouping and source lineage.
                "destination": str(classification.destination),
                "notification_class": "requester",
                "media_type": classification.media_type,
                "provider_id": provider_id,
                "tmdb_id": identity.tmdb_id,
                "tvdb_id": identity.tvdb_id,
                "imdb_id": identity.imdb_id,
                "season_number": season,
                "mode": "season_completion"
                if classification.media_type == "series"
                else "movie",
                "generation": 1,
                "baseline": 1,
                "status": "active",
                "created_at": _lookup(source_row, "created_at") or utc_timestamp(),
                "updated_at": _lookup(source_row, "updated_at") or utc_timestamp(),
            }
            selected = {
                key: value
                for key, value in values.items()
                if key in subscription_columns
            }
            subscription_id = _insert_dynamic(connection, "subscriptions", selected)
        else:
            subscription_id = int(existing[0])
        # The unit key describes the canonical media obligation, not the
        # source-row key.  Equivalent legacy rows therefore reuse the same
        # subscription unit instead of creating duplicate derived units while
        # retaining separate source-row lineage.
        logical_key = (
            f"{classification.media_type}:{provider}:{provider_id}:movie"
            if season is None
            else f"{classification.media_type}:{provider}:{provider_id}:season:{season}"
        )
        unit_existing = connection.execute(
            "SELECT id FROM subscription_units WHERE subscription_id = ? AND logical_unit_key = ?",
            (subscription_id, logical_key),
        ).fetchone()
        if unit_existing is not None:
            item_ids.append(int(unit_existing[0]))
            continue
        unit_values = {
            "subscription_id": subscription_id,
            "logical_unit_key": logical_key,
            "unit_type": "movie" if season is None else "season",
            "provider_id": provider_id,
            "season_number": season,
            "expected": 1,
            "status": "tracking",
            "metadata_json": json.dumps(
                {
                    "source": classification.source_name,
                    "source_table": classification.source_table,
                    "source_row_id": classification.source_row_id,
                },
                sort_keys=True,
            ),
            "created_at": _lookup(source_row, "created_at") or utc_timestamp(),
            "updated_at": _lookup(source_row, "updated_at") or utc_timestamp(),
        }
        item_ids.append(_insert_dynamic(connection, "subscription_units", unit_values))
    return tuple(item_ids)


def _insert_quarantine(
    connection: sqlite3.Connection,
    classification: LegacyRowClassification,
) -> None:
    columns = _table_columns(connection, "quarantined_records")
    detail_json = json.dumps(
        {
            "source_fingerprint": classification.source_fingerprint,
            "media_type": classification.media_type,
        },
        sort_keys=True,
    )
    # Migration 0003 uses source/reason_code/detail_json.  The compatibility
    # table created above uses descriptive source_name/reason/payload_json
    # names.  Supplying only columns present keeps both shapes supported.
    values: dict[str, Any] = {
        "source": classification.source_name,
        "source_name": classification.source_name,
        "source_table": classification.source_table,
        "source_id": classification.source_row_id,
        "source_row_id": classification.source_row_id,
        "record_type": classification.media_type or "legacy_media_request",
        "reason_code": classification.reason,
        "reason": classification.reason,
        "disposition": LegacyDisposition.QUARANTINED.value,
        "detail_json": detail_json,
        "payload_json": detail_json,
        "status": "open",
        "created_at": utc_timestamp(),
        "updated_at": utc_timestamp(),
    }
    selected = [(key, value) for key, value in values.items() if key in columns]
    names = ", ".join(
        _quote_identifier(key, "quarantine column") for key, _ in selected
    )
    placeholders = ", ".join("?" for _ in selected)
    try:
        connection.execute(
            f"INSERT INTO {_quote_identifier('quarantined_records')} "
            f"({names}) VALUES ({placeholders})",
            tuple(value for _, value in selected),
        )
    except sqlite3.IntegrityError as exc:
        # A future quarantine schema may use a different uniqueness key.  A
        # duplicate quarantine is harmless; retain the import transaction.
        if "UNIQUE constraint failed" not in str(exc):
            raise


def _record_mapping(
    connection: sqlite3.Connection,
    classification: LegacyRowClassification,
    disposition: LegacyDisposition,
    reason: str,
    *,
    target_request_id: int | None = None,
    derived_item_count: int = 0,
    details: Mapping[str, Any] | None = None,
) -> None:
    now = utc_timestamp()
    logical_disposition = disposition.value
    detail_values = dict(details or {})
    stored_disposition = logical_disposition
    if (
        disposition is LegacyDisposition.DELETE_CANDIDATE
        and not _mapping_supports_delete_candidate(connection)
    ):
        # Migration 0004 deliberately keeps proposals out of its CHECKed
        # final-disposition column.  Preserve the logical candidate and its
        # two-phase intent in details_json until the checked-in schema grows
        # a dedicated intent table/column.
        stored_disposition = LegacyDisposition.QUARANTINED.value
        detail_values["accounting_disposition"] = logical_disposition
    payload = json.dumps(detail_values, sort_keys=True, separators=(",", ":"))
    existing = _mapping_row(connection, classification)
    if existing is not None:
        old_fingerprint = str(existing["source_fingerprint"])
        old_disposition = str(existing["disposition"])
        old_logical_disposition = _mapping_accounting_disposition(existing)
        if old_fingerprint != classification.source_fingerprint:
            # A source-row identity is a durable event, not a mutable pointer.
            # The quarantine record carries the new fingerprint; the original
            # mapping remains intact for rollback and audit.
            return
        if (
            old_logical_disposition == logical_disposition
            and str(existing["reason"]) == reason
        ):
            if stored_disposition != old_disposition or payload != str(
                existing["details_json"]
            ):
                connection.execute(
                    "UPDATE legacy_source_mappings SET disposition = ?, "
                    "details_json = ?, updated_at = ? WHERE source_name = ? "
                    "AND source_table = ? AND source_row_id = ?",
                    (
                        stored_disposition,
                        payload,
                        now,
                        classification.source_name,
                        classification.source_table,
                        classification.source_row_id,
                    ),
                )
            return
        allowed_transition = (
            old_logical_disposition == LegacyDisposition.DELETE_CANDIDATE.value
            and disposition is LegacyDisposition.DELETED_AFTER_APPROVAL
        )
        if not allowed_transition:
            raise LegacyMigrationError("source disposition is immutable")
        connection.execute(
            "UPDATE legacy_source_mappings SET disposition = ?, reason = ?, "
            "target_request_id = ?, derived_item_count = ?, details_json = ?, "
            "updated_at = ? WHERE source_name = ? AND source_table = ? "
            "AND source_row_id = ? AND source_fingerprint = ?",
            (
                stored_disposition,
                reason,
                target_request_id,
                derived_item_count,
                payload,
                now,
                classification.source_name,
                classification.source_table,
                classification.source_row_id,
                classification.source_fingerprint,
            ),
        )
        return
    connection.execute(
        """
        INSERT INTO legacy_source_mappings
            (source_name, source_table, source_row_id, source_fingerprint,
             disposition, reason, target_request_id, derived_item_count,
             details_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            classification.source_name,
            classification.source_table,
            classification.source_row_id,
            classification.source_fingerprint,
            stored_disposition,
            reason,
            target_request_id,
            derived_item_count,
            payload,
            now,
            now,
        ),
    )


def _mapped_item_ids(
    connection: sqlite3.Connection,
    request_id: int | None,
) -> tuple[int, ...]:
    """Recover derived IDs for an idempotent re-run's report."""

    if request_id is None:
        return ()
    if _table_exists(connection, "subscription_units") and _table_exists(
        connection, "subscriptions"
    ):
        rows = connection.execute(
            """
            SELECT u.id FROM subscription_units AS u
            JOIN subscriptions AS s ON s.id = u.subscription_id
            WHERE s.request_id = ? ORDER BY u.id
            """,
            (request_id,),
        ).fetchall()
        return tuple(int(row[0]) for row in rows)
    if _table_exists(connection, "request_items"):
        rows = connection.execute(
            "SELECT id FROM request_items WHERE request_id = ? ORDER BY id",
            (request_id,),
        ).fetchall()
        return tuple(int(row[0]) for row in rows)
    return ()


def _record_lineage(
    connection: sqlite3.Connection,
    classification: LegacyRowClassification,
    disposition: LegacyDisposition,
    reason: str,
    *,
    target_table: str | None = None,
    target_row_id: int | None = None,
    target_item_ids: Sequence[int] = (),
) -> None:
    """Write canonical migration lineage when the ledger table is installed."""

    if disposition is LegacyDisposition.DELETE_CANDIDATE:
        # The normalized lineage CHECK intentionally excludes a proposal that
        # has not yet been approved/executed.  The source mapping table still
        # accounts for that dry-run candidate.
        return
    if not _table_exists(connection, "migration_lineage"):
        return
    migration_id = classification.source_name
    lineage_columns = _table_columns(connection, "migration_lineage")
    select_columns = (
        "id, disposition, reason_code, target_table, target_row_id, expansion_count"
    )
    if "source_fingerprint" in lineage_columns:
        select_columns += ", source_fingerprint"
    existing = connection.execute(
        """
        SELECT """
        + select_columns
        + """ FROM migration_lineage
        WHERE migration_id = ? AND source_table = ? AND source_row_id = ?
        """,
        (migration_id, classification.source_table, classification.source_row_id),
    ).fetchone()
    if existing is None:
        columns = [
            "migration_id",
            "source_table",
            "source_row_id",
            "disposition",
            "reason_code",
            "target_table",
            "target_row_id",
            "expansion_count",
            "created_at",
        ]
        values: list[Any] = [
            migration_id,
            classification.source_table,
            classification.source_row_id,
            disposition.value,
            reason,
            target_table,
            None if target_row_id is None else str(target_row_id),
            len(target_item_ids),
            utc_timestamp(),
        ]
        if "source_fingerprint" in lineage_columns:
            columns.append("source_fingerprint")
            values.append(classification.source_fingerprint)
        quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
        placeholders = ", ".join("?" for _ in values)
        cursor = connection.execute(
            f"""
            INSERT INTO migration_lineage
                ({quoted_columns}) VALUES ({placeholders})
            """,
            tuple(values),
        )
        lineage_id = int(cursor.lastrowid or 0)
    else:
        lineage_id = int(existing[0])
        old_fingerprint = (
            str(existing[6]) if "source_fingerprint" in lineage_columns else None
        )
        if old_fingerprint and old_fingerprint != classification.source_fingerprint:
            return
        expected = (
            disposition.value,
            reason,
            target_table,
            None if target_row_id is None else str(target_row_id),
            len(target_item_ids),
        )
        actual = tuple(existing[index] for index in range(1, 6))
        if actual != expected:
            raise LegacyMigrationError("migration lineage disposition is immutable")
        # Re-running an identical lineage event must not rewrite its timestamp
        # or expansion children.
        return
    if not _table_exists(connection, "migration_expansions"):
        return
    # A changed source row is reclassified and updates the existing lineage
    # record.  Remove the prior derived seasons before writing the new set so
    # a quarantine/update cannot leave stale obligations attached to it.
    connection.execute(
        "DELETE FROM migration_expansions WHERE lineage_id = ?", (lineage_id,)
    )
    if not target_item_ids:
        return
    for item_id, season in zip(target_item_ids, classification.seasons or (None,)):
        connection.execute(
            """
            INSERT INTO migration_expansions
                (lineage_id, target_table, target_row_id, season_number, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                lineage_id,
                "subscription_units"
                if _table_exists(connection, "subscription_units")
                else "request_items",
                str(item_id),
                season,
                utc_timestamp(),
            ),
        )


def _approved_reason(
    classification: LegacyRowClassification,
    approve_deletes: bool,
    approved_delete_reasons: set[str] | None,
) -> bool:
    if not classification.delete_candidate:
        return False
    if approved_delete_reasons is not None:
        return classification.reason in approved_delete_reasons
    return approve_deletes


class LegacyMigrationImporter:
    """Importer facade for callers that prefer an object API."""

    def __init__(
        self,
        target: Any,
        source: Any | None = None,
        *,
        source_name: str = DEFAULT_SOURCE_NAME,
        source_table: str = LEGACY_TABLE,
    ) -> None:
        self.target = target
        self.source = source
        self.source_name = source_name
        self.source_table = _identifier(source_table, "source table")

    def import_rows(
        self,
        rows: Iterable[Mapping[str, Any]] | Mapping[str, Any] | Any | None = None,
        *,
        approve_deletes: bool = False,
        approved_delete: bool | None = None,
        approve_delete: bool | None = None,
        approved_delete_reasons: Iterable[str] | None = None,
        delete_source: bool = True,
        approval: LegacyMigrationApproval | None = None,
        backup_artifact: LegacyBackupArtifact | None = None,
    ) -> LegacyImportReport:
        if rows is None:
            if self.source is None:
                raise TypeError("rows or a source supplied to the importer is required")
            rows = self.source
        return import_legacy_rows(
            rows,
            self.target,
            source_name=self.source_name,
            source_table=self.source_table,
            approve_deletes=(
                approve_deletes
                if approved_delete is None and approve_delete is None
                else bool(
                    approved_delete if approved_delete is not None else approve_delete
                )
            ),
            approved_delete_reasons=approved_delete_reasons,
            delete_source=delete_source,
            approval=approval,
            backup_artifact=backup_artifact,
        )

    run = import_rows
    execute = import_rows

    def dry_run(self, source: Any | None = None) -> LegacyDryRunReport:
        candidate = self.source if source is None else source
        if candidate is None:
            raise TypeError("source or a source supplied to the importer is required")
        return dry_run_legacy_migration(
            candidate,
            source_name=self.source_name,
            source_table=self.source_table,
        )

    def import_source(
        self,
        source: Any,
        **kwargs: Any,
    ) -> LegacyImportReport:
        return import_legacy_rows(
            source,
            self.target,
            source_name=self.source_name,
            source_table=self.source_table,
            **kwargs,
        )


class LegacyMigrationClassifier:
    """Small object facade around the pure row classifier."""

    def __init__(
        self,
        *,
        source_name: str = DEFAULT_SOURCE_NAME,
        source_table: str = LEGACY_TABLE,
    ) -> None:
        self.source_name = source_name
        self.source_table = _identifier(source_table, "source table")

    def classify(self, row: Mapping[str, Any]) -> LegacyRowClassification:
        return classify_legacy_row(
            row,
            source_name=self.source_name,
            source_table=self.source_table,
        )

    def classify_rows(
        self, rows: Iterable[Mapping[str, Any]] | Mapping[str, Any]
    ) -> tuple[LegacyRowClassification, ...]:
        return tuple(self.classify(row) for row in _coerce_rows(rows))

    def dry_run(self, source: Any) -> LegacyDryRunReport:
        return dry_run_legacy_migration(
            source,
            source_name=self.source_name,
            source_table=self.source_table,
        )


def import_legacy_rows(
    source: Any,
    target: Any,
    *,
    source_name: str = DEFAULT_SOURCE_NAME,
    source_table: str = LEGACY_TABLE,
    approve_deletes: bool = False,
    approved_delete: bool | None = None,
    approve_delete: bool | None = None,
    approved_delete_reasons: Iterable[str] | None = None,
    delete_source: bool = True,
    approval: LegacyMigrationApproval | None = None,
    backup_artifact: LegacyBackupArtifact | None = None,
) -> LegacyImportReport:
    """Import legacy rows into a migrated companion DB transactionally.

    ``approve_deletes`` is retained as a compatibility switch but cannot
    authorize deletion by itself.  A verified immutable backup and an
    approval bound to the exact dry-run hash/counts/reasons are required; only
    the three narrow missing-required-field reasons can then be deleted.
    """

    if approved_delete is not None or approve_delete is not None:
        approve_deletes = bool(
            approved_delete if approved_delete is not None else approve_delete
        )
    source_rows = _source_rows(
        source,
        source_name=source_name,
        source_table=source_table,
        read_only=True,
    )
    raw_classifications = tuple(
        classify_legacy_row(
            row,
            source_name=source_rows.source_name,
            source_table=source_rows.source_table,
        )
        for row in source_rows.rows
    )
    dry_run = LegacyDryRunReport(
        raw_classifications,
        source_rows.source_name,
        source_rows.source_table,
        source_rows.source_identity,
        source_rows.source_schema_fingerprint,
        source_rows.source_data_fingerprint,
    )
    candidates = tuple(row for row in raw_classifications if row.delete_candidate)
    if backup_artifact is not None and approval is None:
        raise LegacyMigrationError(
            "backup artifact must be bound to an exact dry-run approval"
        )
    approved_reasons: set[str] | None = None
    if approval is not None:
        if (
            approval.source_name != source_rows.source_name
            or approval.source_table != source_rows.source_table
            or approval.dry_run_hash != dry_run.dry_run_hash
            or approval.source_rows != dry_run.source_rows
            or dict(approval.disposition_counts) != dry_run.disposition_counts
            or dict(approval.reason_counts) != dry_run.reason_counts
        ):
            raise LegacyMigrationError(
                "approval does not match the exact current dry-run plan"
            )
        if backup_artifact is not None and backup_artifact != approval.backup:
            raise LegacyMigrationError("approval and supplied backup artifact differ")
        verify_legacy_backup(approval.backup, source=source)
        approved_reasons = set(approval.approved_delete_reasons)
    elif candidates and (approve_deletes or approved_delete_reasons):
        raise LegacyMigrationError(
            "deletion requires a verified backup and exact dry-run approval"
        )
    # Surrogate IDs are only used for durable quarantine/accounting.  The
    # source itself remains non-deletable when its true ID is malformed.
    classifications = tuple(
        _surrogate_classification(row, ordinal)
        for ordinal, row in enumerate(raw_classifications)
    )
    _recheck_source_snapshot(source_rows)
    if approval is None and approved_delete_reasons is not None:
        approved_reasons = {
            reason.value if isinstance(reason, Enum) else str(reason)
            for reason in approved_delete_reasons
        }
    elif approval is None:
        approved_reasons = None
    outcomes: list[LegacyImportOutcome] = []
    pending_deletes: list[LegacyRowClassification] = []
    same_db_deletions = False
    target_path = _target_path(target)
    source_path = source_rows.source_path

    with _migration_transaction(target, source_rows, target_path) as connection:
        _validate_target_schema(connection)
        request_table = _request_table(connection)
        _table_columns(connection, request_table)
        _ensure_lineage_tables(connection)
        _recheck_source_snapshot(source_rows)
        for classification in classifications:
            existing_mapping = _mapping_row(connection, classification)
            if existing_mapping is not None:
                old_fingerprint = str(existing_mapping["source_fingerprint"])
                old_disposition = _mapping_accounting_disposition(existing_mapping)
                if old_fingerprint != classification.source_fingerprint:
                    # A reused source ID is a new event.  Do not overwrite or
                    # conflate the immutable prior disposition; force an
                    # operator to plan the changed snapshot explicitly.
                    raise LegacyMigrationError(
                        f"{LegacyReason.SOURCE_ROW_CHANGED.value}: source row "
                        f"{classification.source_row_id} changed after disposition "
                        f"{old_disposition}"
                    )
                if (
                    old_disposition != LegacyDisposition.DELETE_CANDIDATE.value
                    or not _approved_reason(
                        classification, approve_deletes, approved_reasons
                    )
                ):
                    try:
                        disposition = LegacyDisposition(old_disposition)
                    except ValueError:
                        disposition = LegacyDisposition.QUARANTINED
                    target_request_id = (
                        int(existing_mapping["target_request_id"])
                        if existing_mapping["target_request_id"] is not None
                        else None
                    )
                    mapped_item_ids = _mapped_item_ids(connection, target_request_id)
                    outcomes.append(
                        LegacyImportOutcome(
                            classification,
                            disposition,
                            str(existing_mapping["reason"]),
                            target_request_id=target_request_id,
                            target_item_ids=mapped_item_ids,
                            source_deleted=disposition
                            is LegacyDisposition.DELETED_AFTER_APPROVAL,
                        )
                    )
                    continue

            if classification.disposition is LegacyDisposition.QUARANTINED:
                _insert_quarantine(connection, classification)
                _record_mapping(
                    connection,
                    classification,
                    LegacyDisposition.QUARANTINED,
                    classification.reason,
                    details={"source_fingerprint": classification.source_fingerprint},
                )
                _record_lineage(
                    connection,
                    classification,
                    LegacyDisposition.QUARANTINED,
                    classification.reason,
                )
                outcomes.append(
                    LegacyImportOutcome(
                        classification,
                        LegacyDisposition.QUARANTINED,
                        classification.reason,
                    )
                )
                continue

            if classification.delete_candidate:
                approved = _approved_reason(
                    classification, approve_deletes, approved_reasons
                )
                can_delete = approved and delete_source and source_rows.delete_supported
                if can_delete and source_path is not None and target_path is not None:
                    can_delete = source_path.resolve() == target_path.resolve()
                if can_delete and source_rows.source_connection is not None:
                    # A caller-owned connection can be read-only; defer the
                    # delete until after the target transaction in that case.
                    can_delete = (
                        source_path is None and source_rows.source_connection is target
                    )
                if can_delete:
                    # Same-connection/same-target deletion is committed with
                    # the mapping, preserving one transaction boundary.
                    row_id = classification.source_row_id
                    deleted = (
                        connection.execute(
                            f"DELETE FROM {_quote_identifier(classification.source_table, 'source table')} "
                            f"WHERE {_quote_identifier('id')} = ?",
                            (row_id,),
                        ).rowcount
                        == 1
                    )
                    if deleted:
                        same_db_deletions = True
                        _record_mapping(
                            connection,
                            classification,
                            LegacyDisposition.DELETED_AFTER_APPROVAL,
                            classification.reason,
                            details={
                                "approved": True,
                                "source_deleted": True,
                                "deletion_intent": "finalized",
                                "approval_hash": approval.approval_hash
                                if approval is not None
                                else None,
                                "backup_sha256": approval.backup.backup_sha256
                                if approval is not None
                                else None,
                            },
                        )
                        _record_lineage(
                            connection,
                            classification,
                            LegacyDisposition.DELETED_AFTER_APPROVAL,
                            classification.reason,
                        )
                        outcomes.append(
                            LegacyImportOutcome(
                                classification,
                                LegacyDisposition.DELETED_AFTER_APPROVAL,
                                classification.reason,
                                source_deleted=True,
                            )
                        )
                        continue
                _record_mapping(
                    connection,
                    classification,
                    LegacyDisposition.DELETE_CANDIDATE,
                    classification.reason,
                    details={
                        "approved": approved,
                        "deletion_intent": (
                            "prepared"
                            if approved
                            and delete_source
                            and source_rows.delete_supported
                            else "not_requested"
                        ),
                        "approval_hash": approval.approval_hash
                        if approval is not None and approved
                        else None,
                        "backup_sha256": approval.backup.backup_sha256
                        if approval is not None and approved
                        else None,
                        "source_deletion_requested": delete_source,
                        "source_deletion_available": source_rows.delete_supported,
                    },
                )
                pending_deletes.extend(
                    [classification]
                    if approved and delete_source and source_rows.delete_supported
                    else []
                )
                outcomes.append(
                    LegacyImportOutcome(
                        classification,
                        LegacyDisposition.DELETE_CANDIDATE,
                        classification.reason,
                    )
                )
                continue

            if classification.identity is None or classification.destination is None:
                # Terminal history is archived even when the old row no
                # longer carries a stable provider ID or destination.  It is
                # not safe to manufacture a canonical request (the ledger
                # requires a provider ID), but it is safe and important to
                # retain the terminal source-row disposition in lineage.
                if classification.terminal:
                    terminal_source_row = next(
                        (
                            row
                            for row in source_rows.rows
                            if _source_row_id(row) == classification.source_row_id
                            and _fingerprint(row) == classification.source_fingerprint
                        ),
                        {},
                    )
                    _record_mapping(
                        connection,
                        classification,
                        LegacyDisposition.TERMINALLY_ARCHIVED,
                        classification.reason,
                        details={
                            "source_fingerprint": classification.source_fingerprint,
                            "archived_without_target_request": True,
                            "legacy_status": _legacy_status(terminal_source_row),
                            "arr_id": _legacy_arr_id(
                                terminal_source_row,
                                classification.media_type or "movie",
                            ),
                            "notified": bool(
                                _lookup(
                                    terminal_source_row,
                                    "notified_available_at",
                                    "notified_at",
                                    "fulfilled_at",
                                )
                            ),
                        },
                    )
                    _record_lineage(
                        connection,
                        classification,
                        LegacyDisposition.TERMINALLY_ARCHIVED,
                        classification.reason,
                    )
                    outcomes.append(
                        LegacyImportOutcome(
                            classification,
                            LegacyDisposition.TERMINALLY_ARCHIVED,
                            classification.reason,
                        )
                    )
                    continue
                # Defensive guard: a future classifier extension must not
                # accidentally write a partial request.
                quarantine = LegacyRowClassification(
                    classification.source_row_id,
                    LegacyDisposition.QUARANTINED,
                    LegacyReason.MISSING_IDENTITY.value
                    if classification.identity is None
                    else LegacyReason.MISSING_DESTINATION.value,
                    classification.media_type,
                    classification.destination,
                    classification.identity,
                    classification.seasons,
                    classification.source_fingerprint,
                    source_table=classification.source_table,
                    source_name=classification.source_name,
                )
                _insert_quarantine(connection, quarantine)
                _record_mapping(
                    connection,
                    quarantine,
                    LegacyDisposition.QUARANTINED,
                    quarantine.reason,
                )
                _record_lineage(
                    connection,
                    quarantine,
                    LegacyDisposition.QUARANTINED,
                    quarantine.reason,
                )
                outcomes.append(
                    LegacyImportOutcome(
                        quarantine, LegacyDisposition.QUARANTINED, quarantine.reason
                    )
                )
                continue

            identity = classification.identity
            source_row = next(
                row
                for row in source_rows.rows
                if _source_row_id(row) == classification.source_row_id
                and _fingerprint(row) == classification.source_fingerprint
            )
            if (
                not classification.terminal
                and request_table == "requests"
                and _table_exists(connection, "subscriptions")
                and _column_not_null(connection, "subscriptions", "user_id")
                and _positive_id(
                    _lookup(source_row, "requested_by_user_id", "user_id")
                )[0]
                is None
            ):
                quarantine = replace(
                    classification,
                    disposition=LegacyDisposition.QUARANTINED,
                    reason=LegacyReason.MISSING_REQUESTER_ID.value,
                    delete_candidate=False,
                )
                _insert_quarantine(connection, quarantine)
                _record_mapping(
                    connection,
                    quarantine,
                    LegacyDisposition.QUARANTINED,
                    quarantine.reason,
                )
                _record_lineage(
                    connection,
                    quarantine,
                    LegacyDisposition.QUARANTINED,
                    quarantine.reason,
                )
                outcomes.append(
                    LegacyImportOutcome(
                        quarantine, LegacyDisposition.QUARANTINED, quarantine.reason
                    )
                )
                continue
            try:
                existing_request = (
                    connection.execute(
                        f"SELECT {_quote_identifier('id')} FROM "
                        f"{_quote_identifier(request_table, 'target table')} "
                        f"WHERE {_quote_identifier('request_key')} = ?",
                        (_request_key(classification),),
                    ).fetchone()
                    if "request_key" in _table_columns(connection, request_table)
                    else None
                )
                # Terminal source rows are historical facts.  Do not fold
                # them into an unrelated pending request for the same chat;
                # only an exact source mapping/request key is idempotently
                # reusable.  Pending rows may use semantic equivalence.
                equivalent_id = (
                    int(existing_request[0])
                    if existing_request is not None
                    else None
                    if classification.terminal
                    else _find_equivalent_request(connection, classification, identity)
                )
            except LegacyMigrationError as exc:
                quarantine = LegacyRowClassification(
                    classification.source_row_id,
                    LegacyDisposition.QUARANTINED,
                    str(exc),
                    classification.media_type,
                    classification.destination,
                    classification.identity,
                    classification.seasons,
                    classification.source_fingerprint,
                    source_table=classification.source_table,
                    source_name=classification.source_name,
                )
                _insert_quarantine(connection, quarantine)
                _record_mapping(
                    connection,
                    quarantine,
                    LegacyDisposition.QUARANTINED,
                    quarantine.reason,
                )
                _record_lineage(
                    connection,
                    quarantine,
                    LegacyDisposition.QUARANTINED,
                    quarantine.reason,
                )
                outcomes.append(
                    LegacyImportOutcome(
                        quarantine, LegacyDisposition.QUARANTINED, quarantine.reason
                    )
                )
                continue

            terminal = classification.terminal
            item_ids: list[int] = []
            if equivalent_id is not None:
                target_request_id = equivalent_id
                disposition = (
                    LegacyDisposition.TERMINALLY_ARCHIVED
                    if terminal
                    else LegacyDisposition.EQUIVALENTLY_MERGED
                )
                reason = (
                    classification.reason
                    if terminal
                    else LegacyReason.VALID_PENDING_MOVIE.value
                    if classification.media_type == "movie"
                    else LegacyReason.VALID_PENDING_SERIES.value
                )
            else:
                provider, provider_id = _provider_identity(identity)
                request_values = {
                    "request_key": _request_key(classification),
                    "user_id": _positive_id(
                        _lookup(source_row, "requested_by_user_id", "user_id")
                    )[0],
                    "requested_by_user_id": _positive_id(
                        _lookup(source_row, "requested_by_user_id", "user_id")
                    )[0],
                    "requested_by_chat_id": classification.destination,
                    "chat_id": classification.destination,
                    "requested_by_username": _bounded_text(
                        _lookup(source_row, "requested_by_username", "username"),
                        max_bytes=256,
                    ),
                    "username": _bounded_text(
                        _lookup(source_row, "requested_by_username", "username"),
                        max_bytes=256,
                    ),
                    "media_type": classification.media_type,
                    "provider_id": provider_id,
                    "title": _bounded_text(
                        _lookup(source_row, "title", "name"), max_bytes=512
                    )
                    or f"Legacy {classification.media_type} {provider_id}",
                    "year": _legacy_year(source_row)[0],
                    "tmdb_id": identity.tmdb_id,
                    "tvdb_id": identity.tvdb_id,
                    "imdb_id": identity.imdb_id,
                    "external_provider": provider,
                    "external_id": provider_id,
                    "seasons_json": json.dumps(classification.seasons)
                    if classification.media_type == "series"
                    else None,
                    "mode": "season_completion"
                    if classification.media_type == "series"
                    else "movie",
                    # Preserve the exact legacy lifecycle state, including
                    # in-flight ``notifying``/Arr states.  The normalized
                    # ledger has no lossy status translation requirement.
                    "status": _legacy_status(source_row),
                    "provider_item_id": str(
                        _legacy_arr_id(source_row, classification.media_type or "movie")
                    )
                    if _legacy_arr_id(source_row, classification.media_type or "movie")
                    is not None
                    else None,
                    "arr_id": _legacy_arr_id(
                        source_row, classification.media_type or "movie"
                    ),
                    "payload_json": json.dumps(
                        {
                            "legacy_source": classification.source_name,
                            "legacy_table": classification.source_table,
                            "legacy_row_id": classification.source_row_id,
                            "legacy_status": _legacy_status(source_row),
                            "radarr_movie_id": _legacy_arr_id(source_row, "movie"),
                            "sonarr_series_id": _legacy_arr_id(source_row, "series"),
                            "notified_available_at": _bounded_text(
                                _lookup(
                                    source_row,
                                    "notified_available_at",
                                    "notified_at",
                                    "fulfilled_at",
                                ),
                                max_bytes=128,
                            ),
                        },
                        sort_keys=True,
                    ),
                    "idempotency_key": f"{_request_key(classification)}:idempotency",
                    # The legacy table is not required to carry timestamps in
                    # every known fixture.  Do not pass ``NULL`` through to
                    # the canonical table's NOT NULL/defaulted audit fields:
                    # use the source values when present and let the import
                    # timestamp fill the gap otherwise.
                    "created_at": _bounded_text(
                        _lookup(source_row, "created_at"), max_bytes=64
                    )
                    or utc_timestamp(),
                    "updated_at": _bounded_text(
                        _lookup(source_row, "updated_at"), max_bytes=64
                    )
                    or utc_timestamp(),
                }
                target_request_id = _insert_dynamic(
                    connection, request_table, request_values
                )
                disposition = (
                    LegacyDisposition.TERMINALLY_ARCHIVED
                    if terminal
                    else LegacyDisposition.MIGRATED
                )
                reason = classification.reason

            if equivalent_id is not None and request_table == "requests":
                _reconcile_request_state(
                    connection,
                    target_request_id,
                    source_row,
                    classification,
                )
            if request_table == "requests" and _table_exists(
                connection, "subscriptions"
            ):
                item_ids.extend(
                    _insert_or_get_subscription_units(
                        connection,
                        target_request_id,
                        classification,
                        identity,
                        source_row,
                        terminal=terminal,
                    )
                )
            else:
                if not terminal:
                    item_seasons = (
                        classification.seasons
                        if classification.media_type == "series"
                        else (None,)
                    )
                    for season in item_seasons:
                        item_ids.append(
                            _insert_or_get_item(
                                connection,
                                target_request_id,
                                classification,
                                identity,
                                season,
                                terminal=False,
                            )
                        )
            _record_mapping(
                connection,
                classification,
                disposition,
                reason,
                target_request_id=target_request_id,
                derived_item_count=len(item_ids),
                details={
                    "source_fingerprint": classification.source_fingerprint,
                    "provider": _provider_identity(identity)[0],
                    "seasons": list(classification.seasons),
                },
            )
            _record_lineage(
                connection,
                classification,
                disposition,
                reason,
                target_table=request_table,
                target_row_id=target_request_id,
                target_item_ids=tuple(item_ids),
            )
            outcomes.append(
                LegacyImportOutcome(
                    classification,
                    disposition,
                    reason,
                    target_request_id=target_request_id,
                    target_item_ids=tuple(item_ids),
                )
            )

        # Detect a writer that raced the source read while target rows were
        # being assembled; raising here rolls back the entire target
        # transaction rather than committing a mixed snapshot.
        if not same_db_deletions:
            same_database = (
                source_path is not None
                and target_path is not None
                and source_path.resolve() == target_path.resolve()
            )
            _recheck_source_snapshot(source_rows, ignore_file_identity=same_database)

    # Separate source databases are deleted only after a successful target
    # commit.  If a source row changed between dry-run and this point, leave it
    # intact and downgrade the report outcome to a candidate.
    if pending_deletes and delete_source and source_rows.delete_supported:
        for classification in pending_deletes:
            deleted = _delete_source_row(source_rows, classification)
            if deleted:
                for index, outcome in enumerate(outcomes):
                    if outcome.source_row_id == classification.source_row_id:
                        outcomes[index] = LegacyImportOutcome(
                            outcome.classification,
                            LegacyDisposition.DELETED_AFTER_APPROVAL,
                            classification.reason,
                            source_deleted=True,
                        )
                with _target_transaction(target) as connection:
                    _record_mapping(
                        connection,
                        classification,
                        LegacyDisposition.DELETED_AFTER_APPROVAL,
                        classification.reason,
                        details={
                            "approved": True,
                            "source_deleted": True,
                            "deletion_intent": "finalized",
                            "approval_hash": approval.approval_hash
                            if approval is not None
                            else None,
                            "backup_sha256": approval.backup.backup_sha256
                            if approval is not None
                            else None,
                        },
                    )
                    _record_lineage(
                        connection,
                        classification,
                        LegacyDisposition.DELETED_AFTER_APPROVAL,
                        classification.reason,
                    )
    return LegacyImportReport(
        tuple(outcomes), source_rows.source_name, source_rows.source_table
    )


def _delete_source_row(
    source_rows: _SourceRows, classification: LegacyRowClassification
) -> bool:
    if source_rows.source_path is not None:
        if (
            not source_rows.source_path.exists()
            or source_rows.source_path.is_symlink()
            or not source_rows.source_path.is_file()
        ):
            return False
        connection = sqlite3.connect(str(source_rows.source_path), timeout=5.0)
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                f"SELECT * FROM {_quote_identifier(classification.source_table, 'source table')} "
                f"WHERE {_quote_identifier('id')} = ?",
                (classification.source_row_id,),
            ).fetchone()
            if current is None:
                connection.execute("ROLLBACK")
                return False
            columns = [
                str(item[0])
                for item in connection.execute(
                    f"SELECT * FROM {_quote_identifier(classification.source_table, 'source table')} "
                    f"WHERE {_quote_identifier('id')} = ? LIMIT 1",
                    (classification.source_row_id,),
                ).description
                or ()
            ]
            row = dict(zip(columns, current))
            if _fingerprint(row) != classification.source_fingerprint:
                connection.execute("ROLLBACK")
                return False
            deleted = (
                connection.execute(
                    f"DELETE FROM {_quote_identifier(classification.source_table, 'source table')} "
                    f"WHERE {_quote_identifier('id')} = ?",
                    (classification.source_row_id,),
                ).rowcount
                == 1
            )
            connection.execute("COMMIT" if deleted else "ROLLBACK")
            return deleted
        except sqlite3.Error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return False
        finally:
            connection.close()
    if source_rows.source_connection is not None:
        # Never force a write on a caller-owned connection unless it is known
        # to be writable and not already in a transaction owned elsewhere.
        connection = source_rows.source_connection
        if connection.in_transaction:
            return False
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_rows = _rows_from_connection(
                connection, classification.source_table
            )
            current = next(
                (
                    row
                    for row in current_rows
                    if _source_row_id(row) == classification.source_row_id
                ),
                None,
            )
            if (
                current is None
                or _fingerprint(current) != classification.source_fingerprint
            ):
                return False
            deleted = (
                connection.execute(
                    f"DELETE FROM {_quote_identifier(classification.source_table, 'source table')} "
                    f"WHERE {_quote_identifier('id')} = ?",
                    (classification.source_row_id,),
                ).rowcount
                == 1
            )
            connection.execute("COMMIT" if deleted else "ROLLBACK")
            return deleted
        except sqlite3.Error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return False
    return False


def reconcile_legacy_deletion_intents(
    source: Any,
    target: Any,
    *,
    approval: LegacyMigrationApproval,
) -> LegacyImportReport:
    """Finalize or safely resume prepared separate-database deletions.

    A crash between source deletion and target finalization leaves a durable
    ``prepared`` mapping.  Reconciliation requires the same approval/backup,
    verifies the immutable artifact, and then either finalizes an already
    absent row or deletes the still-identical row under an ``IMMEDIATE``
    source lock.  A changed row remains untouched and is quarantined.
    """

    if not isinstance(approval, LegacyMigrationApproval):
        raise LegacyMigrationError("reconciliation requires exact migration approval")
    verify_legacy_backup(approval.backup)
    source_path = _source_path_value(source)
    if (
        source_path is None
        or source_path.resolve() != Path(approval.backup.source_database).resolve()
    ):
        raise LegacyMigrationError("reconciliation source does not match approval")
    source_rows = _source_rows(
        source,
        source_name=approval.source_name,
        source_table=approval.source_table,
        read_only=True,
    )
    outcomes: list[LegacyImportOutcome] = []
    with _target_transaction(target) as connection:
        _validate_target_schema(connection)
        mappings = connection.execute(
            "SELECT * FROM legacy_source_mappings "
            "WHERE source_name = ? AND source_table = ? "
            "AND disposition IN (?, ?) ORDER BY source_row_id",
            (
                approval.source_name,
                approval.source_table,
                LegacyDisposition.DELETE_CANDIDATE.value,
                LegacyDisposition.QUARANTINED.value,
            ),
        ).fetchall()
        for mapping in mappings:
            try:
                details = json.loads(mapping["details_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                details = {}
            if not isinstance(details, dict) or (
                _mapping_accounting_disposition(mapping)
                != LegacyDisposition.DELETE_CANDIDATE.value
                or details.get("deletion_intent") != "prepared"
            ):
                continue
            if details.get("approval_hash") not in {None, approval.approval_hash}:
                raise LegacyMigrationError("deletion intent approval hash mismatch")
            source_id = str(mapping["source_row_id"])
            source_fingerprint = str(mapping["source_fingerprint"])
            base = LegacyRowClassification(
                source_id,
                LegacyDisposition.DELETE_CANDIDATE,
                str(mapping["reason"]),
                None,
                None,
                None,
                (),
                source_fingerprint,
                delete_candidate=True,
                source_table=approval.source_table,
                source_name=approval.source_name,
            )
            current = next(
                (row for row in source_rows.rows if _source_row_id(row) == source_id),
                None,
            )
            if current is not None:
                if _fingerprint(current) != source_fingerprint:
                    changed = replace(
                        base,
                        disposition=LegacyDisposition.QUARANTINED,
                        reason=LegacyReason.SOURCE_ROW_CHANGED.value,
                        delete_candidate=False,
                    )
                    _insert_quarantine(connection, changed)
                    outcomes.append(
                        LegacyImportOutcome(
                            changed, changed.disposition, changed.reason
                        )
                    )
                    continue
                deleted = _delete_source_row(source_rows, base)
                if not deleted:
                    outcomes.append(
                        LegacyImportOutcome(base, base.disposition, base.reason)
                    )
                    continue
            finalized = replace(
                base,
                disposition=LegacyDisposition.DELETED_AFTER_APPROVAL,
                delete_candidate=False,
            )
            _record_mapping(
                connection,
                finalized,
                LegacyDisposition.DELETED_AFTER_APPROVAL,
                base.reason,
                details={
                    "approved": True,
                    "source_deleted": True,
                    "deletion_intent": "finalized",
                    "approval_hash": approval.approval_hash,
                    "backup_sha256": approval.backup.backup_sha256,
                },
            )
            _record_lineage(
                connection,
                finalized,
                LegacyDisposition.DELETED_AFTER_APPROVAL,
                base.reason,
            )
            outcomes.append(
                LegacyImportOutcome(
                    finalized,
                    LegacyDisposition.DELETED_AFTER_APPROVAL,
                    base.reason,
                    source_deleted=True,
                )
            )
    return LegacyImportReport(
        tuple(outcomes), approval.source_name, approval.source_table
    )


reconcile_deletion_intents = reconcile_legacy_deletion_intents


# Common aliases make the API easy to discover while retaining one
# implementation and one accounting vocabulary.
def classify_legacy_rows(
    rows: Iterable[Mapping[str, Any]] | Mapping[str, Any],
    **kwargs: Any,
) -> tuple[LegacyRowClassification, ...]:
    return tuple(classify_legacy_row(row, **kwargs) for row in _coerce_rows(rows))


LegacyClassifier = LegacyMigrationClassifier
LegacyMigration = LegacyMigrationImporter
LegacyMigrationPlan = LegacyDryRunReport
DryRunResult = LegacyDryRunReport
MigrationResult = LegacyImportReport
MigrationDisposition = LegacyDisposition
RowDisposition = LegacyDisposition
classify_legacy = classify_legacy_row
dry_run = dry_run_legacy_migration
plan_legacy_migration = dry_run_legacy_migration
import_legacy = import_legacy_rows
run_legacy_migration = import_legacy_rows
migrate_legacy = import_legacy_rows


__all__ = [
    "DEFAULT_SOURCE_NAME",
    "LEGACY_TABLE",
    "LegacyClassifier",
    "LegacyDisposition",
    "LegacyDryRunReport",
    "LegacyBackupArtifact",
    "LegacyMigrationApproval",
    "LegacyIdentity",
    "LegacyImportOutcome",
    "LegacyImportReport",
    "LegacyMigrationError",
    "LegacyMigrationClassifier",
    "LegacyMigration",
    "LegacyMigrationImporter",
    "LegacyMigrationPlan",
    "LegacyReason",
    "LegacyRowClassification",
    "LegacySchemaError",
    "create_verified_legacy_backup",
    "create_legacy_backup",
    "verify_legacy_backup",
    "verify_backup_artifact",
    "approve_legacy_dry_run",
    "reconcile_legacy_deletion_intents",
    "reconcile_deletion_intents",
    "DryRunResult",
    "MigrationResult",
    "MigrationDisposition",
    "RowDisposition",
    "classify_legacy",
    "classify_legacy_row",
    "classify_legacy_rows",
    "dry_run",
    "dry_run_legacy_migration",
    "import_legacy",
    "import_legacy_rows",
    "migrate_legacy",
    "plan_legacy_migration",
    "run_legacy_migration",
]
