"""SQLite foundation for Media Companion.

The application deliberately keeps SQLite access in this module.  Connections
are short-lived, foreign keys and WAL are enabled on every connection, and
schema changes are applied in one transaction with a checksum recorded for
each migration.  The helpers at the bottom of the module are intentionally
small primitives that higher-level repositories can compose without reaching
for ad-hoc SQL transaction handling.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import tempfile
from pathlib import Path
import time
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .migrations import MIGRATIONS, Migration, validate_migrations


DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_CLAIM_LEASE_SECONDS = 300
MAX_CLAIM_LEASE_SECONDS = 300
DEFAULT_CONFIRMATION_TTL_SECONDS = 300
DEFAULT_CLOCK_ROLLBACK_TOLERANCE_SECONDS = 30
RETENTION_DAYS = 60
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DatabaseError(RuntimeError):
    """Base class for database foundation errors."""


class MigrationError(DatabaseError):
    """Base class for migration errors."""


class MigrationIntegrityError(MigrationError):
    """Raised when an applied migration no longer matches its checksum."""


class MigrationOrderError(MigrationError):
    """Raised when the applied migration history is not contiguous."""


class ClockRollbackError(DatabaseError):
    """Raised when the persisted wall clock moves backwards too far."""


class BackupVerificationError(DatabaseError):
    """Raised when a native SQLite backup does not pass verification."""


@dataclass(frozen=True, slots=True)
class MigrationRecord:
    version: int
    name: str
    checksum: str
    applied_at: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """Result of applying the migration set."""

    applied: tuple[MigrationRecord, ...]
    current_version: int

    @property
    def applied_versions(self) -> tuple[int, ...]:
        return tuple(record.version for record in self.applied)


@dataclass(frozen=True, slots=True)
class ClaimToken:
    """An opaque lease token returned after an atomic claim."""

    token: str
    table: str
    row_id: int | str
    expires_at: str
    version: int | None = None
    leader_epoch: int = 0

    def __str__(self) -> str:
        return self.token


@dataclass(frozen=True, slots=True)
class BackupReport:
    """Verification details for a SQLite-native backup."""

    destination: Path
    integrity_check: str
    foreign_key_errors: tuple[tuple[Any, ...], ...]
    source_schema_checksum: str
    destination_schema_checksum: str
    source_data_checksum: str
    destination_data_checksum: str
    source_table_counts: tuple[tuple[str, int], ...]
    destination_table_counts: tuple[tuple[str, int], ...]
    pages_copied: int
    verified: bool
    quick_check: str = "ok"
    file_sha256: str = ""
    file_size_bytes: int = 0
    inventory_id: int | None = None


@dataclass(frozen=True, slots=True)
class LeaderLease:
    """Durable leader lease/fencing value."""

    lease_name: str
    owner: str
    token: str
    epoch: int
    expires_at: str
    version: int


@dataclass(frozen=True, slots=True)
class BackupInventoryRecord:
    """Hash and retention metadata for one verified backup."""

    id: int
    path: Path
    sha256: str
    size_bytes: int
    source_database: str
    verified: bool
    rollback_snapshot: bool
    created_at: str
    expires_at: str
    expired_at: str | None = None

    def __bool__(self) -> bool:
        return self.verified


def utc_now() -> datetime:
    """Return an aware UTC timestamp for callers that need one."""

    return datetime.now(timezone.utc)


def utc_timestamp(value: datetime | None = None) -> str:
    """Format a timestamp consistently for SQLite lexical comparisons."""

    timestamp = value or utc_now()
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)
    return timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty ISO-8601 string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("timestamp must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_timestamp(value: str) -> str:
    return utc_timestamp(_parse_timestamp(value))


def _safe_error_text(error: object | None, *, max_bytes: int = 512) -> str | None:
    """Store only a bounded, non-sensitive error class/digest."""

    if error is None:
        return None
    text = str(error).replace("\x00", " ").replace("\n", " ").strip()
    encoded = text.encode("utf-8", "replace")
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    kind = type(error).__name__
    value = f"{kind}:{digest}"
    return value[:max_bytes]


def _redact_accounting_value(value: Any, *, depth: int = 0) -> Any:
    """Keep migration details bounded and free of raw operational material."""

    if depth > 4:
        return "<redacted>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in list(value.items())[:64]:
            key_text = str(key)[:128]
            lowered = key_text.lower()
            if any(
                marker in lowered
                for marker in (
                    "secret",
                    "token",
                    "password",
                    "path",
                    "payload",
                    "body",
                    "raw",
                    "username",
                    "url",
                    "uri",
                    "header",
                    "credential",
                    "authorization",
                    "cookie",
                    "assertion",
                    "nonce",
                    "requester",
                    "chat",
                    "user",
                    "title",
                    "message",
                )
            ):
                result[key_text] = "<redacted>"
            else:
                result[key_text] = _redact_accounting_value(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _redact_accounting_value(item, depth=depth + 1) for item in list(value)[:64]
        ]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str):
            return value[:512]
        return value
    return f"<{type(value).__name__}>"


def _accounting_details_json(details: Mapping[str, Any] | None) -> str | None:
    if details is None:
        return None
    encoded = json.dumps(
        _redact_accounting_value(details),
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > 8192:
        raise ValueError("migration accounting details exceed 8 KiB")
    return encoded


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


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _secure_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"state directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _secure_file(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"state file must not be a symlink: {path}")
    if path.exists():
        os.chmod(path, 0o600)


def _identifier(value: str, label: str = "identifier") -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _token_text(value: str | ClaimToken) -> str:
    token = value.token if isinstance(value, ClaimToken) else value
    if (
        not isinstance(token, str)
        or not token.strip()
        or len(token.encode("utf-8")) > 256
    ):
        raise ValueError("claim token must be a bounded non-empty token")
    return token


def _as_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return {} if value is None else value


class Database:
    """Short-lived SQLite connections with migrations and CAS primitives."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        migrations: Iterable[Migration] | None = None,
    ) -> None:
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms < 0
        ):
            raise ValueError("busy_timeout_ms must be non-negative")
        self.database = str(path)
        self.path = Path(path) if self.database != ":memory:" else Path(":memory:")
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.migrations = validate_migrations(migrations or MIGRATIONS)
        self._is_memory = (
            self.database == ":memory:"
            or self.database.startswith("file::memory:")
            or (self.database.startswith("file:") and "mode=memory" in self.database)
        )
        if self.database.startswith("file:") and not self._is_memory:
            raise ValueError(
                "file URI databases are unsupported; use a filesystem path so permissions can be enforced"
            )
        if self.database != ":memory:" and self.path.is_symlink():
            raise ValueError("database path must not be a symlink")
        self._uri = self.database.startswith("file:") or self._is_memory
        self._target = self.database
        self._anchor: sqlite3.Connection | None = None
        if self.database == ":memory:":
            # A plain ``:memory:`` database is scoped to one connection.  Keep
            # a private anchor and use a shared-memory URI so each short-lived
            # worker connection sees the same ledger while this Database lives.
            self._target = f"file:media_companion_{id(self)}?mode=memory&cache=shared"
            self._uri = True

        if self.database != ":memory:" and not self._uri:
            _secure_directory(self.path.parent)
        if self._is_memory:
            self._anchor = sqlite3.connect(
                self._target,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
                uri=True,
                check_same_thread=False,
            )
            self._configure_connection(self._anchor)

    def connect(self) -> sqlite3.Connection:
        """Open a configured connection.

        The returned connection is in autocommit mode.  Callers that need an
        atomic unit of work should use :meth:`transaction`; this keeps a
        forgotten commit from leaking across requests while retaining explicit
        transaction boundaries for writes.
        """

        connection = sqlite3.connect(
            self._target,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            uri=self._uri,
            check_same_thread=False,
        )
        self._configure_connection(connection)
        return connection

    def _configure_connection(self, connection: sqlite3.Connection) -> None:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        # SQLite returns ``memory`` for an in-memory database; that is the only
        # case where WAL cannot be selected.  File-backed databases must use
        # WAL so readers do not block the short write transactions below.
        if not self._is_memory:
            journal_mode = str(
                connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            )
            if journal_mode.lower() != "wal":
                if connection is not self._anchor:
                    connection.close()
                raise DatabaseError(
                    f"SQLite refused WAL mode for {self.database!r} (got {journal_mode!r})"
                )
        connection.execute("PRAGMA synchronous = FULL")
        if not self._is_memory:
            self._secure_database_files()

    def _secure_database_files(self) -> None:
        """Re-apply private modes after SQLite creates WAL sidecars."""

        if not self._is_memory:
            _secure_file(self.path)
            _secure_file(Path(f"{self.path}-wal"))
            _secure_file(Path(f"{self.path}-shm"))

    def close(self) -> None:
        """Close the shared in-memory anchor, if one is in use."""

        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()
            self._secure_database_files()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside a committed or rolled-back transaction."""

        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
        finally:
            connection.close()
            self._secure_database_files()

    @staticmethod
    def _epoch_ms(value: datetime) -> int:
        return int(value.timestamp() * 1000)

    def observe_clock(
        self,
        now: datetime | None = None,
        *,
        tolerance_seconds: int = DEFAULT_CLOCK_ROLLBACK_TOLERANCE_SECONDS,
    ) -> datetime:
        """Persist and validate wall-clock monotonicity for claim operations."""

        if (
            isinstance(tolerance_seconds, bool)
            or not isinstance(tolerance_seconds, int)
            or tolerance_seconds < 0
            or tolerance_seconds > DEFAULT_CLOCK_ROLLBACK_TOLERANCE_SECONDS
        ):
            raise ValueError(
                "clock tolerance must be between 0 and "
                f"{DEFAULT_CLOCK_ROLLBACK_TOLERANCE_SECONDS} seconds"
            )
        current = now or utc_now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("clock timestamp must be timezone-aware")
        current = current.astimezone(timezone.utc)
        current_ms = self._epoch_ms(current)
        rollback_detected = False
        try:
            with self.transaction() as connection:
                row = connection.execute(
                    "SELECT last_seen_epoch_ms, blocked, tolerance_ms "
                    "FROM clock_state WHERE clock_name = 'database'"
                ).fetchone()
                stored_tolerance = (
                    tolerance_seconds * 1000 if row is None else int(row[2])
                )
                if stored_tolerance < 0 or stored_tolerance > (
                    DEFAULT_CLOCK_ROLLBACK_TOLERANCE_SECONDS * 1000
                ):
                    raise ClockRollbackError("persisted clock tolerance is invalid")
                if row is not None and row[1]:
                    raise ClockRollbackError(
                        "database clock remains blocked after rollback"
                    )
                if (
                    row is not None
                    and row[0] is not None
                    and current_ms < int(row[0]) - stored_tolerance
                ):
                    # Commit the block before raising so every subsequent
                    # worker fails closed until an operator clears it.
                    connection.execute(
                        "UPDATE clock_state SET blocked = 1, blocked_at = ?, "
                        "updated_at = ? WHERE clock_name = 'database'",
                        (utc_timestamp(current), utc_timestamp(current)),
                    )
                    rollback_detected = True
                if rollback_detected:
                    pass
                elif row is None:
                    connection.execute(
                        "INSERT INTO clock_state"
                        " (clock_name, last_seen_epoch_ms, tolerance_ms, updated_at)"
                        " VALUES ('database', ?, ?, ?)",
                        (current_ms, tolerance_seconds * 1000, utc_timestamp(current)),
                    )
                else:
                    connection.execute(
                        "UPDATE clock_state SET last_seen_epoch_ms = MAX("
                        "COALESCE(last_seen_epoch_ms, ?), ?), updated_at = ? "
                        "WHERE clock_name = 'database'",
                        (current_ms, current_ms, utc_timestamp(current)),
                    )
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
        if rollback_detected:
            raise ClockRollbackError("database clock rollback blocks new claims")
        return current

    def clear_clock_rollback(self, now: datetime | None = None) -> None:
        """Clear a clock block only after an operator has verified system time."""

        current = now or utc_now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("clock timestamp must be timezone-aware")
        current = current.astimezone(timezone.utc)
        with self.transaction() as connection:
            connection.execute(
                "UPDATE clock_state SET last_seen_epoch_ms = ?, blocked = 0, "
                "blocked_at = NULL, updated_at = ? WHERE clock_name = 'database'",
                (self._epoch_ms(current), utc_timestamp(current)),
            )

    def acquire_leader(
        self,
        owner: str,
        *,
        lease_name: str = "media",
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> LeaderLease | None:
        """Acquire one durable fencing epoch using an immediate transaction."""

        if (
            not isinstance(owner, str)
            or not owner.strip()
            or len(owner.encode("utf-8")) > 256
        ):
            raise ValueError("leader owner must not be blank")
        if not isinstance(lease_name, str) or not _IDENTIFIER.fullmatch(lease_name):
            raise ValueError("leader lease name is invalid")
        self._validate_lease_seconds(lease_seconds)
        current = self.observe_clock(now)
        current_text = utc_timestamp(current)
        expiry = utc_timestamp(current + timedelta(seconds=lease_seconds))
        token = _new_token()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT epoch, owner, claim_token, expires_at, version "
                "FROM leader_leases WHERE lease_name = ?",
                (lease_name,),
            ).fetchone()
            if row is not None and row[3] is not None and str(row[3]) > current_text:
                if str(row[1]) != owner:
                    return None
            epoch = (0 if row is None else int(row[0])) + 1
            version = 0 if row is None else int(row[4]) + 1
            connection.execute(
                "INSERT INTO leader_leases"
                " (lease_name, epoch, owner, claim_token, claimed_at, expires_at, version, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(lease_name) DO UPDATE SET epoch = excluded.epoch,"
                " owner = excluded.owner, claim_token = excluded.claim_token,"
                " claimed_at = excluded.claimed_at, expires_at = excluded.expires_at,"
                " version = excluded.version, updated_at = excluded.updated_at",
                (
                    lease_name,
                    epoch,
                    owner,
                    token,
                    current_text,
                    expiry,
                    version,
                    current_text,
                ),
            )
            return LeaderLease(lease_name, owner, token, epoch, expiry, version)

    def renew_leader(
        self,
        lease: LeaderLease,
        *,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> LeaderLease | None:
        self._validate_lease_seconds(lease_seconds)
        current = self.observe_clock(now)
        current_text = utc_timestamp(current)
        expiry = utc_timestamp(current + timedelta(seconds=lease_seconds))
        with self.transaction() as connection:
            result = connection.execute(
                "UPDATE leader_leases SET expires_at = ?, version = version + 1, "
                "updated_at = ? WHERE lease_name = ? AND owner = ? "
                "AND claim_token = ? AND epoch = ? AND expires_at > ? "
                "AND version = ?",
                (
                    expiry,
                    current_text,
                    lease.lease_name,
                    lease.owner,
                    lease.token,
                    lease.epoch,
                    current_text,
                    lease.version,
                ),
            )
            if result.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT version FROM leader_leases WHERE lease_name = ?",
                (lease.lease_name,),
            ).fetchone()
            return LeaderLease(
                lease.lease_name,
                lease.owner,
                lease.token,
                lease.epoch,
                expiry,
                int(row[0]),
            )

    def release_leader(self, lease: LeaderLease) -> bool:
        with self.transaction() as connection:
            result = connection.execute(
                "UPDATE leader_leases SET owner = NULL, claim_token = NULL, "
                "expires_at = NULL, version = version + 1, updated_at = ? "
                "WHERE lease_name = ? AND owner = ? AND claim_token = ? "
                "AND epoch = ? AND version = ?",
                (
                    utc_timestamp(),
                    lease.lease_name,
                    lease.owner,
                    lease.token,
                    lease.epoch,
                    lease.version,
                ),
            )
            return result.rowcount == 1

    def current_leader(
        self,
        lease_name: str = "media",
        *,
        now: datetime | None = None,
    ) -> LeaderLease | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT owner, claim_token, epoch, expires_at, version "
                "FROM leader_leases WHERE lease_name = ?",
                (lease_name,),
            ).fetchone()
        if row is None or row[0] is None or row[1] is None or row[3] is None:
            return None
        current = now or utc_now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("leader clock timestamp must be timezone-aware")
        if _parse_timestamp(str(row[3])) <= current.astimezone(timezone.utc):
            return None
        return LeaderLease(
            lease_name, str(row[0]), str(row[1]), int(row[2]), str(row[3]), int(row[4])
        )

    @staticmethod
    def _validate_lease_seconds(lease_seconds: int) -> None:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
            or lease_seconds > MAX_CLAIM_LEASE_SECONDS
        ):
            raise ValueError(
                f"lease_seconds must be between 1 and {MAX_CLAIM_LEASE_SECONDS}"
            )

    @staticmethod
    def _positive_row_id(value: int, label: str = "row_id") -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            or value > 9_007_199_254_740_991
        ):
            raise ValueError(f"{label} must be a positive safe integer")
        return value

    def migrate(self) -> MigrationReport:
        """Apply one ordered migration per locked, resumable transaction."""

        self._ensure_management_tables()
        connection = self.connect()
        applied_records: list[MigrationRecord] = []
        try:
            while True:
                next_migration: Migration | None = None
                connection.execute("BEGIN IMMEDIATE")
                try:
                    existing = self._read_migration_rows(connection)
                    self._validate_applied_history(existing)
                    self._validate_user_version(connection, existing)
                    migration_by_version = {
                        migration.version: migration for migration in self.migrations
                    }
                    for record in existing:
                        migration = migration_by_version.get(record.version)
                        if migration is None:
                            raise MigrationIntegrityError(
                                f"database contains unknown migration version {record.version}"
                            )
                        if (
                            record.name != migration.name
                            or record.checksum != migration.checksum
                        ):
                            raise MigrationIntegrityError(
                                "migration "
                                f"{record.version} ({record.name!r}) does not match "
                                "the checked-in migration payload"
                            )
                    next_migration = next(
                        (
                            migration
                            for migration in self.migrations
                            if migration.version
                            > (existing[-1].version if existing else 0)
                        ),
                        None,
                    )
                    if next_migration is None:
                        connection.execute("COMMIT")
                        # ``user_version`` is a recovery hint, not the source
                        # of truth.  Repair a stale marker only after the
                        # checksummed ledger rows are committed.
                        highest = existing[-1].version if existing else 0
                        connection.execute(f"PRAGMA user_version = {highest}")
                        break
                    started = time.monotonic()
                    started_at = utc_timestamp()
                    connection.execute(
                        """
                        INSERT INTO migration_accounting
                            (migration_version, migration_name, source_name, status,
                             started_at)
                        VALUES (?, ?, 'schema', 'running', ?)
                        ON CONFLICT(migration_version, source_name)
                        DO UPDATE SET migration_name = excluded.migration_name,
                                      status = excluded.status,
                                      started_at = excluded.started_at,
                                      completed_at = NULL,
                                      source_rows = 0,
                                      migrated_rows = 0,
                                      skipped_rows = 0,
                                      failed_rows = 0,
                                      details_json = NULL,
                                      error_text = NULL
                        """,
                        (next_migration.version, next_migration.name, started_at),
                    )
                    next_migration.run(connection)
                    duration_ms = max(0, int((time.monotonic() - started) * 1000))
                    applied_at = utc_timestamp()
                    connection.execute(
                        """
                        INSERT INTO schema_migrations
                            (version, name, checksum, started_at, completed_at,
                             applied_at, duration_ms)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            next_migration.version,
                            next_migration.name,
                            next_migration.checksum,
                            started_at,
                            applied_at,
                            applied_at,
                            duration_ms,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE migration_accounting
                        SET status = 'completed', completed_at = ?,
                            details_json = ?, error_text = NULL
                        WHERE migration_version = ? AND source_name = 'schema'
                        """,
                        (
                            applied_at,
                            json.dumps({"duration_ms": duration_ms}, sort_keys=True),
                            next_migration.version,
                        ),
                    )
                    connection.execute("COMMIT")
                    # Keep the marker no higher than the committed row ledger.
                    connection.execute(
                        f"PRAGMA user_version = {next_migration.version}"
                    )
                    applied_records.append(
                        MigrationRecord(
                            next_migration.version,
                            next_migration.name,
                            next_migration.checksum,
                            applied_at,
                            duration_ms,
                        )
                    )
                except BaseException as exc:
                    try:
                        connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    if next_migration is not None:
                        self._record_failed_migration(next_migration, exc)
                    raise
        finally:
            connection.close()
            self._secure_database_files()

        latest = self.migrations[-1].version if self.migrations else 0
        return MigrationReport(tuple(applied_records), latest)

    def migration_status(self) -> tuple[MigrationRecord, ...]:
        """Read the checksummed migration history without changing the DB."""

        with self.connection() as connection:
            try:
                return tuple(self._read_migration_rows(connection))
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return ()
                raise

    run_migrations = migrate

    def record_migration_accounting(
        self,
        migration_version: int,
        migration_name: str,
        *,
        source_name: str = "data",
        status: str = "planned",
        source_rows: int = 0,
        migrated_rows: int = 0,
        skipped_rows: int = 0,
        failed_rows: int = 0,
        details: Mapping[str, Any] | None = None,
        error_text: str | None = None,
    ) -> int:
        """Insert/update a future data-migration accounting row.

        This is an accounting-only hook.  It does not copy, transform, or
        delete any application data.
        """

        if (
            isinstance(migration_version, bool)
            or not isinstance(migration_version, int)
            or migration_version < 1
        ):
            raise ValueError("migration_version must be positive")
        if (
            not isinstance(migration_name, str)
            or not migration_name.strip()
            or len(migration_name) > 256
        ):
            raise ValueError("migration_name must be bounded and non-empty")
        if (
            not isinstance(source_name, str)
            or not source_name.strip()
            or len(source_name) > 256
        ):
            raise ValueError("source_name must be bounded and non-empty")
        if status not in {"planned", "running", "completed", "failed", "skipped"}:
            raise ValueError(f"invalid migration accounting status: {status!r}")
        values = (source_rows, migrated_rows, skipped_rows, failed_rows)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise ValueError("migration accounting row counts cannot be negative")
        if status in {"completed", "failed", "skipped"} and source_rows != sum(
            values[1:]
        ):
            raise ValueError("completed migration accounting must conserve source rows")
        now = utc_timestamp()
        details_json = _accounting_details_json(details)
        safe_error = _safe_error_text(error_text)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT status, migration_name, source_rows, migrated_rows, "
                "skipped_rows, failed_rows, details_json, error_text "
                "FROM migration_accounting WHERE migration_version = ? "
                "AND source_name = ?",
                (migration_version, source_name),
            ).fetchone()
            if existing is not None and str(existing[0]) == "completed":
                if (
                    str(existing[1]) != migration_name
                    or tuple(int(existing[index]) for index in range(2, 6))
                    != (source_rows, migrated_rows, skipped_rows, failed_rows)
                    or existing[6] != details_json
                    or existing[7] != safe_error
                ):
                    raise MigrationIntegrityError(
                        "completed migration accounting is immutable"
                    )
                return int(
                    connection.execute(
                        "SELECT id FROM migration_accounting WHERE "
                        "migration_version = ? AND source_name = ?",
                        (migration_version, source_name),
                    ).fetchone()[0]
                )
            connection.execute(
                """
                INSERT INTO migration_accounting
                    (migration_version, migration_name, source_name, status,
                     source_rows, migrated_rows, skipped_rows, failed_rows,
                     started_at, completed_at, details_json, error_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(migration_version, source_name)
                DO UPDATE SET migration_name = excluded.migration_name,
                              status = excluded.status,
                              source_rows = excluded.source_rows,
                              migrated_rows = excluded.migrated_rows,
                              skipped_rows = excluded.skipped_rows,
                              failed_rows = excluded.failed_rows,
                              completed_at = excluded.completed_at,
                              details_json = excluded.details_json,
                              error_text = excluded.error_text
                """,
                (
                    migration_version,
                    migration_name,
                    source_name,
                    status,
                    *values,
                    now if status in {"running", "completed", "failed"} else None,
                    now if status in {"completed", "failed", "skipped"} else None,
                    details_json,
                    safe_error,
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM migration_accounting
                WHERE migration_version = ? AND source_name = ?
                """,
                (migration_version, source_name),
            ).fetchone()
            if (
                row is None
            ):  # pragma: no cover - defensive; INSERT above must return one
                raise DatabaseError("migration accounting row was not stored")
            return int(row[0])

    def record_migration_lineage(
        self,
        migration_id: str,
        source_table: str,
        source_row_id: int | str,
        disposition: str,
        *,
        reason_code: str | None = None,
        target_table: str | None = None,
        target_row_id: int | str | None = None,
        expansions: Sequence[Mapping[str, Any]] = (),
        source_fingerprint: str | None = None,
    ) -> int:
        """Record one synthetic/source-row disposition and derived expansions.

        This is deliberately an accounting primitive, not a migration engine:
        it never reads the legacy database or mutates a target record.  A
        caller can use it in a dry-run transaction to prove that every source
        row has exactly one disposition and that per-season expansion remains
        derived lineage rather than a false one-to-one row count.
        """

        dispositions = {
            "migrated",
            "equivalently_merged",
            "terminally_archived",
            "deleted_after_approval",
            "quarantined",
        }
        if disposition not in dispositions:
            raise ValueError(f"invalid migration disposition: {disposition!r}")
        if not migration_id or not source_table:
            raise ValueError("migration_id and source_table must not be blank")
        _identifier(source_table, "source table")
        if target_table is not None:
            _identifier(target_table, "target table")
        if disposition in {"migrated", "equivalently_merged"} and (
            target_table is None or target_row_id is None
        ):
            raise ValueError("migrated lineage requires a target identity")
        source_key = str(source_row_id)
        if not source_key or len(source_key) > 256:
            raise ValueError("source_row_id must not be blank")
        if reason_code is not None and (
            not isinstance(reason_code, str)
            or not re.fullmatch(r"[a-z0-9_.-]{1,128}", reason_code)
        ):
            raise ValueError("reason_code must be a bounded code")
        fingerprint = (
            source_fingerprint
            or hashlib.sha256(
                f"{migration_id}\0{source_table}\0{source_key}".encode("utf-8")
            ).hexdigest()
        )
        if not _SHA256.fullmatch(fingerprint):
            raise ValueError("source_fingerprint must be lowercase SHA-256")
        if any(
            expansion.get("season_number") is not None
            and (
                isinstance(expansion["season_number"], bool)
                or not isinstance(expansion["season_number"], int)
                or expansion["season_number"] < 0
            )
            for expansion in expansions
        ):
            raise ValueError("expansion season_number must be a non-negative integer")
        normalized_expansions: list[tuple[str, str | None, int | None]] = []
        for expansion in expansions:
            target = expansion.get("target_table")
            if not isinstance(target, str) or not target:
                raise ValueError("each migration expansion needs a target table")
            _identifier(target, "expansion target table")
            target_id = expansion.get("target_row_id")
            normalized_expansions.append(
                (
                    target,
                    None if target_id is None else str(target_id),
                    expansion.get("season_number"),
                )
            )
        if len(set(normalized_expansions)) != len(normalized_expansions):
            raise ValueError("migration expansions must be unique")
        normalized_expansions.sort()

        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT id, disposition, reason_code, target_table, target_row_id, "
                "expansion_count, source_fingerprint FROM migration_lineage "
                "WHERE migration_id = ? AND source_table = ? AND source_row_id = ?",
                (migration_id, source_table, source_key),
            ).fetchone()
            if existing is not None:
                expected = (
                    disposition,
                    reason_code,
                    target_table,
                    None if target_row_id is None else str(target_row_id),
                    len(normalized_expansions),
                    fingerprint,
                )
                actual = tuple(existing[index] for index in range(1, 7))
                if actual != expected:
                    raise MigrationIntegrityError(
                        "migration lineage disposition is immutable"
                    )
                stored_expansions = tuple(
                    tuple(row)
                    for row in connection.execute(
                        "SELECT target_table, target_row_id, season_number "
                        "FROM migration_expansions WHERE lineage_id = ? "
                        "ORDER BY id",
                        (int(existing[0]),),
                    ).fetchall()
                )
                if stored_expansions != tuple(normalized_expansions):
                    raise MigrationIntegrityError(
                        "migration lineage expansions are immutable"
                    )
                return int(existing[0])
            result = connection.execute(
                """
                INSERT INTO migration_lineage
                    (migration_id, source_table, source_row_id, disposition,
                     reason_code, target_table, target_row_id, expansion_count,
                     source_fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    migration_id,
                    source_table,
                    source_key,
                    disposition,
                    reason_code,
                    target_table,
                    None if target_row_id is None else str(target_row_id),
                    len(normalized_expansions),
                    fingerprint,
                ),
            )
            del result
            row = connection.execute(
                """
                SELECT id FROM migration_lineage
                WHERE migration_id = ? AND source_table = ? AND source_row_id = ?
                """,
                (migration_id, source_table, source_key),
            ).fetchone()
            if (
                row is None
            ):  # pragma: no cover - defensive; UPSERT above must return one
                raise DatabaseError("migration lineage row was not stored")
            lineage_id = int(row[0])
            for target, target_id, season_number in normalized_expansions:
                connection.execute(
                    """
                    INSERT INTO migration_expansions
                        (lineage_id, target_table, target_row_id, season_number)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        lineage_id,
                        target,
                        target_id,
                        season_number,
                    ),
                )
            return lineage_id

    def record_legacy_source_mapping(
        self,
        source_name: str,
        source_table: str,
        source_row_id: int | str,
        source_fingerprint: str,
        disposition: str,
        *,
        reason: str,
        target_request_id: int | None = None,
        derived_item_count: int = 0,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist an immutable, redacted legacy-row disposition.

        This is the hook a lazy legacy importer can call after its own source
        validation.  It never accepts raw source payloads and rejects a
        changed decision for an already-accounted row.
        """

        if (
            not isinstance(source_name, str)
            or not source_name.strip()
            or len(source_name) > 256
        ):
            raise ValueError("source_name must be bounded and non-empty")
        _identifier(source_table, "source table")
        source_key = str(source_row_id)
        if not source_key or len(source_key) > 256:
            raise ValueError("source_row_id must be bounded and non-empty")
        if not _SHA256.fullmatch(source_fingerprint):
            raise ValueError("source_fingerprint must be lowercase SHA-256")
        if disposition not in {
            "migrated",
            "equivalently_merged",
            "terminally_archived",
            "delete_candidate",
            "deleted_after_approval",
            "quarantined",
        }:
            raise ValueError("invalid legacy source disposition")
        if not isinstance(reason, str) or not re.fullmatch(
            r"[a-z0-9_.-]{1,128}", reason
        ):
            raise ValueError("reason must be a bounded redacted code")
        if (
            isinstance(derived_item_count, bool)
            or not isinstance(derived_item_count, int)
            or derived_item_count < 0
        ):
            raise ValueError("derived_item_count must be non-negative")
        if target_request_id is not None:
            self._positive_row_id(target_request_id, "target_request_id")
        details_json = _accounting_details_json(details) or "{}"
        now = utc_timestamp()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT source_fingerprint, disposition, reason, target_request_id, "
                "derived_item_count, details_json FROM legacy_source_mappings "
                "WHERE source_name = ? AND source_table = ? AND source_row_id = ?",
                (source_name, source_table, source_key),
            ).fetchone()
            expected = (
                source_fingerprint,
                disposition,
                reason,
                target_request_id,
                derived_item_count,
                details_json,
            )
            if row is not None:
                if tuple(row) != expected:
                    raise MigrationIntegrityError("legacy source mapping is immutable")
                return
            connection.execute(
                "INSERT INTO legacy_source_mappings"
                " (source_name, source_table, source_row_id, source_fingerprint,"
                " disposition, reason, target_request_id, derived_item_count,"
                " details_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_name,
                    source_table,
                    source_key,
                    source_fingerprint,
                    disposition,
                    reason,
                    target_request_id,
                    derived_item_count,
                    details_json,
                    now,
                    now,
                ),
            )

    def consume_actor_nonce(
        self,
        nonce: str,
        expires_at: int,
        *,
        now: float | None = None,
    ) -> bool:
        """Atomically record a hash-only actor nonce if it is still fresh.

        The plaintext nonce is never written to SQLite.  Expired rows are
        removed in the same immediate transaction, so a replay cannot race a
        cleanup pass and a fresh assertion cannot be rejected by an old row.
        """

        if not isinstance(nonce, str) or not nonce or len(nonce.encode("utf-8")) > 512:
            raise ValueError("nonce must be a bounded non-empty string")
        if (
            not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or abs(expires_at) > 9_007_199_254_740_991
        ):
            raise TypeError("expires_at must be a safe integer")
        current = time.time() if now is None else float(now)
        if not math.isfinite(current):
            raise ValueError("now must be finite")
        if expires_at <= current:
            return False
        nonce_hash = hashlib.sha256(nonce.encode("utf-8", "strict")).hexdigest()
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM actor_nonces WHERE nonce_hash IN ("
                "SELECT nonce_hash FROM actor_nonces WHERE expires_at <= ? "
                "ORDER BY nonce_hash LIMIT 100)",
                (int(current),),
            )
            # The bounded sweep above is maintenance only.  Always remove the
            # presented hash itself when its prior reservation has expired so
            # backlog size cannot turn a valid fresh assertion into a replay.
            connection.execute(
                "DELETE FROM actor_nonces WHERE nonce_hash = ? AND expires_at <= ?",
                (nonce_hash, int(current)),
            )
            result = connection.execute(
                """
                INSERT OR IGNORE INTO actor_nonces
                    (nonce_hash, consumed_at, expires_at)
                VALUES (?, ?, ?)
                """,
                (nonce_hash, int(current), expires_at),
            )
            return result.rowcount == 1

    def cleanup_actor_nonces(
        self,
        *,
        now: float | None = None,
        limit: int = 100,
    ) -> int:
        """Delete expired hash-only actor nonces and return the row count."""

        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("nonce cleanup limit must be positive")
        current = time.time() if now is None else float(now)
        if not math.isfinite(current):
            raise ValueError("now must be finite")
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT nonce_hash FROM actor_nonces WHERE expires_at <= ? "
                "ORDER BY nonce_hash LIMIT ?",
                (int(current), limit),
            ).fetchall()
            if not rows:
                return 0
            placeholders = ", ".join("?" for _ in rows)
            result = connection.execute(
                f"DELETE FROM actor_nonces WHERE nonce_hash IN ({placeholders})",
                tuple(str(row[0]) for row in rows),
            )
            return result.rowcount

    consume_nonce = consume_actor_nonce
    cleanup_nonces = cleanup_actor_nonces

    def nonce_replay_store(self) -> "SQLiteNonceReplayStore":
        """Return the auth protocol adapter backed by this database."""

        return SQLiteNonceReplayStore(self)

    nonce_store = nonce_replay_store

    def confirmation_store(
        self,
        *,
        ttl: int = DEFAULT_CONFIRMATION_TTL_SECONDS,
        policy_version: str = "1",
    ) -> "SQLiteConfirmationTokenStore":
        """Return the hash-only confirmation protocol adapter."""

        return SQLiteConfirmationTokenStore(
            self, ttl=ttl, policy_version=policy_version
        )

    confirmation_token_store = confirmation_store

    def migration_disposition_counts(self, migration_id: str) -> dict[str, int]:
        """Return anonymous disposition counts for a dry-run artifact."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT disposition, COUNT(*)
                FROM migration_lineage
                WHERE migration_id = ?
                GROUP BY disposition
                ORDER BY disposition
                """,
                (migration_id,),
            ).fetchall()
            return {str(row[0]): int(row[1]) for row in rows}

    def validate_migration_conservation(
        self,
        migration_id: str,
        *,
        expected_source_rows: int | None = None,
    ) -> dict[str, Any]:
        """Return exact lineage disposition accounting or fail closed."""

        if (
            not isinstance(migration_id, str)
            or not migration_id
            or len(migration_id) > 256
        ):
            raise ValueError("migration_id must be bounded and non-empty")
        if expected_source_rows is not None and (
            isinstance(expected_source_rows, bool)
            or not isinstance(expected_source_rows, int)
            or expected_source_rows < 0
        ):
            raise ValueError("expected_source_rows must be non-negative")
        counts = self.migration_disposition_counts(migration_id)
        total = sum(counts.values())
        if expected_source_rows is not None and total != expected_source_rows:
            raise MigrationIntegrityError(
                f"lineage conservation failed: expected {expected_source_rows}, got {total}"
            )
        with self.connection() as connection:
            duplicate = connection.execute(
                "SELECT source_table, source_row_id, COUNT(*) FROM migration_lineage "
                "WHERE migration_id = ? GROUP BY source_table, source_row_id HAVING COUNT(*) > 1",
                (migration_id,),
            ).fetchone()
        if duplicate is not None:
            raise MigrationIntegrityError(
                "source row has multiple lineage dispositions"
            )
        return {
            "migration_id": migration_id,
            "source_rows": total,
            "dispositions": counts,
        }

    def backup(
        self,
        destination: str | Path,
        *,
        pages: int = -1,
        sleep: float = 0.0,
        verify: bool = True,
        expires_at: datetime | None = None,
        rollback_snapshot: bool = False,
    ) -> BackupReport:
        """Create a quiescent, hashed, atomically published SQLite backup."""

        if isinstance(pages, bool) or not isinstance(pages, int) or pages < -1:
            raise ValueError("pages must be -1 or a non-negative integer")
        if (
            isinstance(sleep, bool)
            or not isinstance(sleep, (int, float))
            or not math.isfinite(float(sleep))
            or sleep < 0
        ):
            raise ValueError("backup sleep must be a finite non-negative number")
        destination_path = Path(destination)
        if self.database != ":memory:" and not self.database.startswith("file:"):
            source_paths = {
                self.path.resolve(),
                Path(f"{self.path}-wal").resolve(),
                Path(f"{self.path}-shm").resolve(),
            }
            if destination_path.resolve() in source_paths:
                raise ValueError(
                    "backup destination must differ from the source database and its WAL files"
                )
        _secure_directory(destination_path.parent)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination_path.name}.",
            suffix=".tmp",
            dir=str(destination_path.parent),
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        _secure_file(temporary_path)
        quiesce = self.connect()
        source = self.connect()
        target = sqlite3.connect(str(temporary_path), isolation_level=None)
        pages_copied = 0
        report: BackupReport | None = None
        quiesce_in_transaction = False

        def progress(_status: int, remaining: int, total: int) -> None:
            nonlocal pages_copied
            pages_copied = max(pages_copied, total - remaining)

        try:
            # IMMEDIATE prevents application writers from changing the source
            # while the rollback snapshot and its verification are produced.
            # Hold the writer lock on a dedicated connection.  SQLite's
            # backup API cannot copy from a connection that is itself inside
            # ``BEGIN IMMEDIATE``; a separate lock connection gives us the
            # same quiescent guarantee without making backup spin forever.
            quiesce.execute("BEGIN IMMEDIATE")
            quiesce_in_transaction = True
            target.execute("PRAGMA foreign_keys = ON")
            target.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            source.backup(target, pages=pages, sleep=sleep, progress=progress)
            target.commit()
            report = self._verify_backup_connections(
                source, target, temporary_path, pages_copied
            )
            if verify and not report.verified:
                raise BackupVerificationError(
                    f"SQLite backup verification failed for {destination_path}"
                )
            target.close()
            _secure_file(temporary_path)
            _fsync_file(temporary_path)
            os.replace(temporary_path, destination_path)
            _secure_file(destination_path)
            _fsync_file(destination_path)
            _fsync_directory(destination_path.parent)
            file_sha256, file_size = _sha256_file(destination_path)
            self._verify_backup_openable(destination_path)
            report = BackupReport(
                destination=destination_path,
                integrity_check=report.integrity_check,
                foreign_key_errors=report.foreign_key_errors,
                source_schema_checksum=report.source_schema_checksum,
                destination_schema_checksum=report.destination_schema_checksum,
                source_data_checksum=report.source_data_checksum,
                destination_data_checksum=report.destination_data_checksum,
                source_table_counts=report.source_table_counts,
                destination_table_counts=report.destination_table_counts,
                pages_copied=report.pages_copied,
                verified=report.verified,
                quick_check=report.quick_check,
                file_sha256=file_sha256,
                file_size_bytes=file_size,
            )
        finally:
            target.close()
            if quiesce_in_transaction:
                try:
                    quiesce.execute("COMMIT")
                except sqlite3.Error:
                    try:
                        quiesce.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
            quiesce.close()
            source.close()
            if temporary_path.exists():
                temporary_path.unlink()

        if report is None:
            raise BackupVerificationError("SQLite backup did not produce a report")
        if verify and not report.verified:
            raise BackupVerificationError(
                f"SQLite backup verification failed for {destination_path}"
            )
        inventory_id = self.register_backup(
            report,
            expires_at=expires_at
            or (utc_now() + timedelta(days=7 if rollback_snapshot else RETENTION_DAYS)),
            rollback_snapshot=rollback_snapshot,
        )
        report = BackupReport(
            destination=report.destination,
            integrity_check=report.integrity_check,
            foreign_key_errors=report.foreign_key_errors,
            source_schema_checksum=report.source_schema_checksum,
            destination_schema_checksum=report.destination_schema_checksum,
            source_data_checksum=report.source_data_checksum,
            destination_data_checksum=report.destination_data_checksum,
            source_table_counts=report.source_table_counts,
            destination_table_counts=report.destination_table_counts,
            pages_copied=report.pages_copied,
            verified=report.verified,
            quick_check=report.quick_check,
            file_sha256=report.file_sha256,
            file_size_bytes=report.file_size_bytes,
            inventory_id=inventory_id,
        )
        return report

    backup_to = backup
    backup_sqlite = backup

    def verify_backup(self, destination: str | Path) -> BackupReport:
        """Verify an existing SQLite backup against this database."""

        destination_path = Path(destination)
        if not destination_path.exists():
            raise FileNotFoundError(destination_path)
        if destination_path.is_symlink() or not destination_path.is_file():
            raise BackupVerificationError(
                "backup verification target is not a regular file"
            )
        if self.database != ":memory:" and not self.database.startswith("file:"):
            source_paths = {
                self.path.resolve(),
                Path(f"{self.path}-wal").resolve(),
                Path(f"{self.path}-shm").resolve(),
            }
            if destination_path.resolve() in source_paths:
                raise ValueError(
                    "backup verification target must differ from the source database and its WAL files"
                )
        _secure_file(destination_path)
        quiesce = self.connect()
        source = self.connect()
        target = sqlite3.connect(
            f"file:{destination_path.resolve().as_posix()}?mode=ro",
            isolation_level=None,
            uri=True,
        )
        try:
            quiesce.execute("BEGIN IMMEDIATE")
            report = self._verify_backup_connections(
                source, target, destination_path, 0
            )
            target.close()
            self._verify_backup_openable(destination_path)
            file_sha256, file_size = _sha256_file(destination_path)
            verified_report = BackupReport(
                destination=destination_path,
                integrity_check=report.integrity_check,
                foreign_key_errors=report.foreign_key_errors,
                source_schema_checksum=report.source_schema_checksum,
                destination_schema_checksum=report.destination_schema_checksum,
                source_data_checksum=report.source_data_checksum,
                destination_data_checksum=report.destination_data_checksum,
                source_table_counts=report.source_table_counts,
                destination_table_counts=report.destination_table_counts,
                pages_copied=report.pages_copied,
                verified=report.verified,
                quick_check=report.quick_check,
                file_sha256=file_sha256,
                file_size_bytes=file_size,
            )
            if not verified_report.verified:
                raise BackupVerificationError(
                    f"SQLite backup verification failed for {destination_path}"
                )
            return verified_report
        finally:
            target.close()
            try:
                quiesce.execute("COMMIT")
            except sqlite3.Error:
                try:
                    quiesce.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            quiesce.close()
            source.close()

    def register_backup(
        self,
        report: BackupReport,
        *,
        expires_at: datetime,
        rollback_snapshot: bool = False,
    ) -> int:
        """Record a verified backup and its bounded retention deadline."""

        if not report.verified or not _SHA256.fullmatch(report.file_sha256):
            raise BackupVerificationError(
                "only verified hashed backups may be inventoried"
            )
        if not report.destination.is_file() or report.destination.is_symlink():
            raise BackupVerificationError("backup destination is not a regular file")
        if not isinstance(expires_at, datetime):
            raise ValueError("backup expiry must be a datetime")
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("backup expiry must be timezone-aware")
        if (
            isinstance(report.file_size_bytes, bool)
            or not isinstance(report.file_size_bytes, int)
            or report.file_size_bytes < 0
        ):
            raise ValueError("backup size cannot be negative")
        actual_hash, actual_size = _sha256_file(report.destination)
        if actual_hash != report.file_sha256 or actual_size != report.file_size_bytes:
            raise BackupVerificationError(
                "backup hash or size changed before inventory"
            )
        _secure_directory(report.destination.parent)
        _secure_file(report.destination)
        current = utc_now()
        expiry = expires_at.astimezone(timezone.utc)
        max_expiry = current + timedelta(
            days=7 if rollback_snapshot else RETENTION_DAYS
        )
        if expiry <= current or expiry > max_expiry:
            raise ValueError("backup expiry exceeds its retention window")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO backup_inventory"
                " (path, sha256, size_bytes, source_database, verified,"
                " rollback_snapshot, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, 1, ?, ?, ?)"
                " ON CONFLICT(path) DO UPDATE SET sha256 = excluded.sha256,"
                " size_bytes = excluded.size_bytes, verified = 1,"
                " rollback_snapshot = excluded.rollback_snapshot,"
                " created_at = excluded.created_at, expires_at = excluded.expires_at,"
                " expired_at = NULL",
                (
                    str(report.destination.resolve()),
                    report.file_sha256,
                    report.file_size_bytes,
                    self.database,
                    int(rollback_snapshot),
                    utc_timestamp(),
                    utc_timestamp(expiry),
                ),
            )
            row = connection.execute(
                "SELECT id FROM backup_inventory WHERE path = ?",
                (str(report.destination.resolve()),),
            ).fetchone()
            if row is None:  # pragma: no cover - guarded by the upsert
                raise DatabaseError("backup inventory row was not stored")
            return int(row[0])

    def list_backup_inventory(
        self, *, include_expired: bool = False
    ) -> tuple[BackupInventoryRecord, ...]:
        with self.connection() as connection:
            query = (
                "SELECT id, path, sha256, size_bytes, source_database, verified, "
                "rollback_snapshot, created_at, expires_at, expired_at "
                "FROM backup_inventory"
            )
            if not include_expired:
                query += " WHERE expired_at IS NULL"
            query += " ORDER BY id"
            rows = connection.execute(query).fetchall()
        return tuple(
            BackupInventoryRecord(
                int(row[0]),
                Path(str(row[1])),
                str(row[2]),
                int(row[3]),
                str(row[4]),
                bool(row[5]),
                bool(row[6]),
                str(row[7]),
                str(row[8]),
                None if row[9] is None else str(row[9]),
            )
            for row in rows
        )

    def expire_backup_inventory(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
        delete_files: bool = False,
    ) -> tuple[Path, ...]:
        """Expire a bounded batch of backup records and optionally unlink files."""

        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be positive")
        current = now or utc_now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("backup expiry clock must be timezone-aware")
        current = current.astimezone(timezone.utc)
        current_text = utc_timestamp(current)
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT id, path FROM backup_inventory WHERE expired_at IS NULL "
                "AND expires_at <= ? ORDER BY id LIMIT ?",
                (current_text, limit),
            ).fetchall()
            paths = tuple(Path(str(row[1])) for row in rows)
            for row in rows:
                connection.execute(
                    "UPDATE backup_inventory SET expired_at = ? WHERE id = ?",
                    (current_text, int(row[0])),
                )
        if delete_files:
            for path in paths:
                try:
                    if path.is_file() and not path.is_symlink():
                        path.unlink()
                except FileNotFoundError:
                    pass
        return paths

    def scrub_terminal_personal_data(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
        retention_days: int = RETENTION_DAYS,
    ) -> dict[str, int]:
        """Scrub bounded batches of terminal personal data.

        The operation intentionally updates or removes only rows whose
        terminal timestamp is at least ``retention_days`` old.  ``unknown``
        deliveries and open/quarantined work are never touched.  It returns
        per-table counts so a scheduler can resume without claiming that a
        single pass is complete.
        """

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or retention_days < 1
        ):
            raise ValueError("limit and retention_days must be positive integers")
        current = now or utc_now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("scrub clock must be timezone-aware")
        current = current.astimezone(timezone.utc)
        cutoff = utc_timestamp(current - timedelta(days=retention_days))
        current_text = utc_timestamp(current)
        counts: dict[str, int] = {}

        with self.transaction() as connection:

            def delete_rows(
                table: str,
                where: str,
                values: Sequence[Any],
                id_column: str = "rowid",
            ) -> None:
                table_name = _identifier(table, "table name")
                identity = (
                    "rowid"
                    if id_column == "rowid"
                    else _identifier(id_column, "id column")
                )
                ids = connection.execute(
                    f"SELECT {identity} FROM {table_name} WHERE {where} "
                    f"ORDER BY {identity} LIMIT ?",
                    (*values, limit),
                ).fetchall()
                if not ids:
                    counts[table_name] = 0
                    return
                placeholders = ", ".join("?" for _ in ids)
                result = connection.execute(
                    f"DELETE FROM {table_name} WHERE {identity} IN ({placeholders})",
                    tuple(row[0] for row in ids),
                )
                counts[table_name] = result.rowcount

            def update_rows(
                table: str,
                where: str,
                values: Sequence[Any],
                assignments: str,
                assignment_values: Sequence[Any] = (),
                id_column: str = "id",
            ) -> None:
                table_name = _identifier(table, "table name")
                identity = (
                    "rowid"
                    if id_column == "rowid"
                    else _identifier(id_column, "id column")
                )
                ids = connection.execute(
                    f"SELECT {identity} FROM {table_name} WHERE {where} ORDER BY {identity} LIMIT ?",
                    (*values, limit),
                ).fetchall()
                if not ids:
                    counts[table_name] = 0
                    return
                placeholders = ", ".join("?" for _ in ids)
                result = connection.execute(
                    f"UPDATE {table_name} SET {assignments} "
                    f"WHERE {identity} IN ({placeholders})",
                    (*assignment_values, *(int(row[0]) for row in ids)),
                )
                counts[table_name] = result.rowcount

            # Ephemeral actor-bound records are removed by expiry, not by the
            # 60-day terminal-data cutoff.  Keep every deletion bounded so a
            # large backlog cannot monopolize the ledger transaction.
            delete_rows(
                "request_candidates",
                "expires_at <= ?",
                (current_text,),
                id_column="handle_hash",
            )
            delete_rows(
                "idempotency_keys",
                "expires_at IS NOT NULL AND expires_at <= ?",
                (current_text,),
            )
            delete_rows(
                "retention_dedupe",
                "expires_at IS NOT NULL AND expires_at <= ?",
                (current_text,),
                id_column="dedupe_key",
            )
            delete_rows(
                "provider_operations",
                "status = 'succeeded' AND updated_at <= ?",
                (cutoff,),
                id_column="operation_key",
            )

            update_rows(
                "requests",
                "status IN ('visible_in_plex', 'fulfilled', 'failed', 'blocked', "
                "'canceled', 'cancelled', 'abandoned', 'quarantined', 'deleted') "
                "AND updated_at <= ? AND (requested_by_user_id IS NOT NULL OR "
                "requested_by_chat_id IS NOT NULL OR requested_by_username IS NOT NULL "
                "OR actor_update_id IS NOT NULL OR payload_json IS NOT NULL "
                "OR plex_baseline_json IS NOT NULL)",
                (cutoff,),
                "requested_by_user_id = NULL, requested_by_chat_id = NULL, "
                "requested_by_username = NULL, actor_update_id = NULL, "
                "payload_json = NULL, plex_baseline_json = NULL",
            )
            # Subscriptions carry NOT NULL actor IDs in the deployed schema;
            # deleting only old terminal rows avoids a fake numeric identity
            # while retaining active/unknown obligations.
            subscription_rows = connection.execute(
                "SELECT id FROM subscriptions WHERE status IN "
                "('fulfilled', 'disabled', 'failed', 'canceled', 'cancelled', "
                "'abandoned', 'quarantined') AND "
                "COALESCE(fulfilled_at, disabled_at, updated_at) <= ? "
                "ORDER BY id LIMIT ?",
                (cutoff, limit),
            ).fetchall()
            if subscription_rows:
                ids = tuple(int(row[0]) for row in subscription_rows)
                placeholders = ", ".join("?" for _ in ids)
                deleted = connection.execute(
                    f"DELETE FROM subscriptions WHERE id IN ({placeholders})",
                    ids,
                ).rowcount
            else:
                deleted = 0
            counts["subscriptions"] = deleted

            update_rows(
                "deliveries",
                "status IN ('sent', 'assumed_sent', 'failed', 'abandoned', "
                "'canceled', 'cancelled', 'superseded', 'delivery_blocked') "
                "AND COALESCE(terminal_at, sent_at, abandoned_at, updated_at) <= ? "
                "AND (chat_id <> 0 OR telegram_message_id IS NOT NULL OR "
                "error_text IS NOT NULL OR unknown_reason IS NOT NULL OR "
                "last_error_class IS NOT NULL)",
                (cutoff,),
                "chat_id = 0, telegram_message_id = NULL, error_text = NULL, "
                "unknown_reason = NULL, last_error_class = NULL",
            )
            update_rows(
                "notification_groups",
                "status IN ('sent', 'complete', 'closed', 'superseded', "
                "'canceled', 'cancelled') AND updated_at <= ? AND "
                "(chat_id <> 0 OR payload_json IS NOT NULL)",
                (cutoff,),
                "chat_id = 0, payload_json = NULL",
            )
            update_rows(
                "delivery_chunks",
                "status IN ('sent', 'assumed_sent', 'failed', 'abandoned', "
                "'canceled', 'cancelled', 'superseded') AND updated_at <= ? AND "
                "(telegram_message_id IS NOT NULL OR payload_json <> '{}' OR "
                "error_text IS NOT NULL OR unknown_reason IS NOT NULL)",
                (cutoff,),
                "telegram_message_id = NULL, payload_json = '{}', error_text = NULL, "
                "unknown_reason = NULL",
            )
            update_rows(
                "delivery_memberships",
                "status IN ('fulfilled', 'disabled', 'canceled', 'cancelled', "
                "'superseded') AND updated_at <= ? AND outcome IS NOT NULL",
                (cutoff,),
                "outcome = NULL",
            )
            update_rows(
                "request_commands",
                "status IN ('succeeded', 'failed', 'canceled', 'cancelled') "
                "AND updated_at <= ? AND last_error IS NOT NULL",
                (cutoff,),
                "last_error = NULL",
            )
            update_rows(
                "event_inbox",
                "status IN ('handled', 'ignored', 'failed', 'quarantined') "
                "AND updated_at <= ? AND (error_text IS NOT NULL OR "
                "sanitized_payload_json IS NOT NULL)",
                (cutoff,),
                "error_text = NULL, sanitized_payload_json = NULL",
            )
            update_rows(
                "quarantined_records",
                "status IN ('resolved', 'closed', 'deleted') AND "
                "COALESCE(resolved_at, updated_at) <= ? AND "
                "(source_id IS NOT NULL OR source_row_id IS NOT NULL OR "
                "reason <> 'redacted' OR detail_json IS NOT NULL OR "
                "payload_json IS NOT NULL OR resolved_by IS NOT NULL)",
                (cutoff,),
                "source_id = NULL, source_row_id = NULL, reason = 'redacted', "
                "detail_json = NULL, payload_json = NULL, resolved_by = NULL",
            )
            update_rows(
                "audit_events",
                "created_at <= ? AND (actor_user_id IS NOT NULL OR actor_chat_id IS NOT NULL "
                "OR metadata_json IS NOT NULL)",
                (cutoff,),
                "actor_user_id = NULL, actor_chat_id = NULL, metadata_json = NULL",
            )
            update_rows(
                "legacy_source_mappings",
                "updated_at <= ? AND (details_json <> '{}' OR target_request_id IS NOT NULL "
                "OR reason <> 'redacted')",
                (cutoff,),
                "details_json = '{}', target_request_id = NULL, reason = 'redacted'",
                id_column="rowid",
            )
            confirmation_rows = connection.execute(
                "SELECT token_hash FROM confirmation_capabilities WHERE expires_at <= ? "
                "OR (state IN ('consumed', 'expired', 'revoked') AND updated_at <= ?) "
                "ORDER BY token_hash LIMIT ?",
                (int(current.timestamp()), cutoff, limit),
            ).fetchall()
            if confirmation_rows:
                placeholders = ", ".join("?" for _ in confirmation_rows)
                confirmation_deleted = connection.execute(
                    "DELETE FROM confirmation_capabilities WHERE token_hash IN ("
                    f"{placeholders})",
                    tuple(str(row[0]) for row in confirmation_rows),
                ).rowcount
            else:
                confirmation_deleted = 0
            counts["confirmation_capabilities"] = confirmation_deleted
            nonce_rows = connection.execute(
                "SELECT nonce_hash FROM actor_nonces WHERE expires_at <= ? "
                "ORDER BY nonce_hash LIMIT ?",
                (int(current.timestamp()), limit),
            ).fetchall()
            if nonce_rows:
                placeholders = ", ".join("?" for _ in nonce_rows)
                nonce_deleted = connection.execute(
                    f"DELETE FROM actor_nonces WHERE nonce_hash IN ({placeholders})",
                    tuple(str(row[0]) for row in nonce_rows),
                ).rowcount
            else:
                nonce_deleted = 0
            counts["actor_nonces"] = nonce_deleted
        return counts

    scrub_retention = scrub_terminal_personal_data
    scrub_personal_data = scrub_terminal_personal_data
    cleanup_retention = scrub_terminal_personal_data

    def validate_canonical_schema(self, *, require_latest: bool = True) -> None:
        """Validate the target tables required by legacy cutover primitives."""

        required: dict[str, set[str]] = {
            "requests": {"id", "request_key", "media_type", "provider_id", "status"},
            "subscriptions": {"id", "user_id", "chat_id", "provider_id", "status"},
            "subscription_units": {
                "id",
                "subscription_id",
                "logical_unit_key",
                "status",
            },
            "event_inbox": {"id", "event_key", "payload_hash", "status"},
            "notification_groups": {
                "id",
                "group_key",
                "chat_id",
                "status",
                "season_number",
                "window_generation",
            },
            "deliveries": {
                "id",
                "idempotency_key",
                "chat_id",
                "status",
                "claim_token",
                "claim_epoch",
                "obligation_key",
            },
            "delivery_memberships": {"id", "delivery_id", "subscription_id", "status"},
            "delivery_chunks": {
                "id",
                "delivery_id",
                "ordinal",
                "chunk_count",
                "status",
            },
            "activation": {"activation_id", "status", "version"},
            "leader_leases": {"lease_name", "epoch", "version", "claim_token"},
            "claim_leases": {
                "resource_type",
                "resource_id",
                "claim_token",
                "claim_epoch",
            },
            "clock_state": {"clock_name", "last_seen_epoch_ms", "blocked"},
            "backup_inventory": {"path", "sha256", "verified", "expires_at"},
            "migration_accounting": {"migration_version", "source_name", "status"},
            "migration_lineage": {
                "migration_id",
                "source_table",
                "source_row_id",
                "source_fingerprint",
                "disposition",
            },
            "migration_expansions": {"lineage_id", "target_table", "season_number"},
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
        with self.connection() as connection:
            for table, columns in required.items():
                present = self._table_columns(connection, table)
                missing = columns - present
                if missing:
                    raise MigrationIntegrityError(
                        f"canonical table {table!r} is missing {sorted(missing)!r}"
                    )
            required_objects = {
                "uq_subscriptions_movie_generation",
                "uq_subscriptions_season_generation",
                "uq_delivery_memberships_without_unit",
                "uq_notification_groups_open_generation",
                "uq_deliveries_obligation_key",
                "uq_delivery_chunks_stable_key",
                "uq_migration_expansion_identity",
                "event_inbox_status_insert_guard",
                "delivery_status_transition_guard",
                "migration_accounting_conservation_update_guard",
                "migration_lineage_immutable_guard",
                "legacy_mapping_identity_guard",
            }
            placeholders = ", ".join("?" for _ in required_objects)
            object_rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('index', 'trigger') "
                f"AND name IN ({placeholders})",
                tuple(sorted(required_objects)),
            ).fetchall()
            present_objects = {str(row[0]) for row in object_rows}
            missing_objects = required_objects - present_objects
            if missing_objects:
                raise MigrationIntegrityError(
                    "canonical schema is missing required constraints/indexes "
                    f"{sorted(missing_objects)!r}"
                )
            records = self._read_migration_rows(connection)
            self._validate_applied_history(records)
            self._validate_user_version(connection, records)
            by_version = {migration.version: migration for migration in self.migrations}
            for record in records:
                migration = by_version.get(record.version)
                if (
                    migration is None
                    or record.name != migration.name
                    or record.checksum != migration.checksum
                ):
                    raise MigrationIntegrityError(
                        f"migration {record.version} does not match its checksum"
                    )
            if require_latest:
                if not records or records[-1].version != self.migrations[-1].version:
                    raise MigrationIntegrityError(
                        "canonical schema is not at latest migration"
                    )
            forbidden = {
                "users",
                "identities",
                "chat_bindings",
                "pairing_codes",
                "telegram_users",
                "telegram_user",
                "telegram_user_registry",
                "user_registry",
                "user_identities",
                "actor_users",
                "chat_users",
                "allowlist",
                "blocked_users",
            }
            forbidden_names = tuple(sorted(forbidden))
            present_forbidden = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN (%s)" % ",".join("?" for _ in forbidden_names),
                forbidden_names,
            ).fetchall()
            if present_forbidden:
                raise MigrationIntegrityError(
                    "companion schema must not contain a second actor registry"
                )

    def legacy_cutover_contract(self) -> dict[str, Any]:
        """Return the exact safe hook contract for a lazy legacy importer.

        The importer remains the owner of source reads and policy decisions;
        this method supplies target validation and the durable lineage tables
        without importing or executing it implicitly.
        """

        self.validate_canonical_schema()
        return {
            "target_schema_version": self.migrations[-1].version,
            "lineage_tables": (
                "legacy_source_mappings",
                "migration_lineage",
                "migration_expansions",
            ),
            "accounting_table": "migration_accounting",
            "backup_required": True,
            "backup_must_be_verified": True,
            "legacy_importer_module": "media_companion.legacy_migration",
            "legacy_importer_entrypoint": "import_legacy_rows",
            "lazy_import_required": True,
            "integration_required": (
                "caller must lazy-import the legacy importer, pass this contract "
                "and verified backup, and invoke source/deletion policy explicitly"
            ),
            "source_dispositions": (
                "migrated",
                "equivalently_merged",
                "terminally_archived",
                "delete_candidate",
                "deleted_after_approval",
                "quarantined",
            ),
        }

    def prepare_legacy_cutover(
        self,
        backup_destination: str | Path,
        *,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Create the seven-day rollback snapshot and return importer inputs.

        This is intentionally an explicit two-step contract: callers must
        pass the returned ``contract`` and verified ``backup`` to the lazy
        legacy importer, which remains responsible for source reads and
        approval/deletion policy.  No importer is imported or run implicitly.
        """

        contract = self.legacy_cutover_contract()
        report = self.backup(
            backup_destination,
            expires_at=expires_at or (utc_now() + timedelta(days=7)),
            rollback_snapshot=True,
        )
        if not report.verified or report.inventory_id is None:
            raise BackupVerificationError(
                "legacy cutover requires an inventoried verified backup"
            )
        return {"contract": contract, "backup": report}

    @staticmethod
    def _verify_backup_openable(path: Path) -> None:
        connection = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro",
            isolation_level=None,
            uri=True,
        )
        try:
            quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if quick.lower() != "ok" or integrity.lower() != "ok":
                raise BackupVerificationError(f"backup reopen checks failed for {path}")
        finally:
            connection.close()

    def claim(
        self,
        table: str,
        row_id: int | str,
        *,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
        token: str | None = None,
        now: datetime | None = None,
        worker_id: str | None = None,
        leader_epoch: int | None = None,
        id_column: str = "id",
        allowed_statuses: Sequence[str] | None = None,
    ) -> ClaimToken | None:
        """Atomically claim a row using a random token and lease expiry.

        A claim succeeds only when the row has no active token and is not in a
        terminal status.  Expired tokens can be reclaimed.  The token must be
        supplied to :meth:`complete_claim`, :meth:`release_claim`, or
        :meth:`compare_and_swap`; an integer row id alone is never sufficient.
        """

        self._validate_lease_seconds(lease_seconds)
        if leader_epoch is not None and (
            isinstance(leader_epoch, bool)
            or not isinstance(leader_epoch, int)
            or leader_epoch < 0
        ):
            raise ValueError("leader_epoch must be a non-negative integer")
        table = _identifier(table, "table name")
        id_column = _identifier(id_column, "id column")
        if token is not None and (
            not isinstance(token, str) or len(token.encode("utf-8")) < 32
        ):
            raise ValueError("caller-supplied claim token is too short")
        claim_token = _token_text(token) if token is not None else _new_token()
        claimed_at = self.observe_clock(now)
        claimed_at_text = utc_timestamp(claimed_at)
        expires_at = utc_timestamp(claimed_at + timedelta(seconds=lease_seconds))

        with self.transaction() as connection:
            effective_epoch = 0 if leader_epoch is None else leader_epoch
            columns = self._table_columns(connection, table)
            required = {id_column, "claim_token", "claim_expires_at"}
            missing = required - columns
            if missing:
                raise DatabaseError(
                    f"table {table!r} cannot be claimed; missing {sorted(missing)!r}"
                )
            if "claim_epoch" in columns:
                leader = connection.execute(
                    "SELECT epoch, owner, expires_at FROM leader_leases "
                    "WHERE lease_name = 'media'"
                ).fetchone()
                leader_live = (
                    leader is not None
                    and leader[1] is not None
                    and leader[2] is not None
                    and str(leader[2]) > claimed_at_text
                )
                if leader_live:
                    if leader_epoch is None or int(leader[0]) != leader_epoch:
                        return None
                    effective_epoch = int(leader[0])
                elif leader_epoch is not None and leader_epoch > 0:
                    # A fencing epoch is meaningful only while its leader is
                    # live.  Do not mint a claim that can never be completed,
                    # or let a stale owner continue after its lease expired.
                    return None
            predicates = [
                f"{id_column} = ?",
                "(claim_token IS NULL OR claim_expires_at IS NULL OR claim_expires_at <= ?)",
            ]
            parameters: list[Any] = [row_id, claimed_at_text]
            if "available_at" in columns:
                predicates.append("(available_at IS NULL OR available_at <= ?)")
                parameters.append(claimed_at_text)
            if "status" in columns:
                statuses = tuple(
                    allowed_statuses
                    or (
                        "pending",
                        "queued",
                        "ready",
                        "received",
                        "processing",
                        "claimed",
                        "observed",
                        "requested",
                        "accepted",
                        "retry",
                        "retry_wait",
                    )
                )
                if not statuses:
                    raise ValueError("allowed_statuses cannot be empty")
                placeholders = ", ".join("?" for _ in statuses)
                predicates.append(f"status IN ({placeholders})")
                parameters.extend(statuses)

            assignments = ["claim_token = ?", "claim_expires_at = ?"]
            update_parameters: list[Any] = [claim_token, expires_at]
            if "status" in columns:
                assignments.append("status = 'claimed'")
            if "attempts" in columns:
                assignments.append("attempts = attempts + 1")
            if "version" in columns:
                assignments.append("version = version + 1")
            if "claim_version" in columns:
                assignments.append(
                    "claim_version = version + 1"
                    if "version" in columns
                    else "claim_version = 1"
                )
            if "claim_epoch" in columns:
                assignments.append("claim_epoch = ?")
                update_parameters.append(effective_epoch)
            if "claim_worker" in columns:
                assignments.append("claim_worker = ?")
                update_parameters.append(worker_id)
            if "claimed_at" in columns:
                assignments.append("claimed_at = ?")
                update_parameters.append(claimed_at_text)
            if "updated_at" in columns:
                assignments.append("updated_at = ?")
                update_parameters.append(claimed_at_text)
            result = connection.execute(
                f"UPDATE {table} SET {', '.join(assignments)} WHERE {' AND '.join(predicates)}",
                (*update_parameters, *parameters),
            )
            if result.rowcount != 1:
                return None
            version = None
            if "version" in columns:
                row = connection.execute(
                    f"SELECT version FROM {table} WHERE {id_column} = ?",
                    (row_id,),
                ).fetchone()
                version = None if row is None else int(row[0])
            return ClaimToken(
                claim_token,
                table,
                row_id,
                expires_at,
                version,
                effective_epoch,
            )

    claim_row = claim

    def claim_token(
        self,
        table: str,
        row_id: int | str,
        *,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
        token: str | None = None,
        now: datetime | None = None,
    ) -> str | None:
        """Return only the opaque token for string-oriented worker callers."""

        claim = self.claim(
            table,
            row_id,
            lease_seconds=lease_seconds,
            token=token,
            now=now,
        )
        return None if claim is None else claim.token

    def claim_delivery(
        self,
        row_id: int | str,
        *,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
        token: str | None = None,
        worker_id: str | None = None,
        leader_epoch: int | None = None,
        now: datetime | None = None,
    ) -> ClaimToken | None:
        """Claim one canonical delivery row."""

        return self.claim(
            "deliveries",
            row_id,
            lease_seconds=lease_seconds,
            token=token,
            worker_id=worker_id,
            leader_epoch=leader_epoch,
            now=now,
        )

    def claim_delivery_chunk(
        self,
        row_id: int | str,
        *,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
        token: str | None = None,
        worker_id: str | None = None,
        leader_epoch: int | None = None,
        now: datetime | None = None,
    ) -> ClaimToken | None:
        """Claim one canonical delivery chunk row."""

        return self.claim(
            "delivery_chunks",
            row_id,
            lease_seconds=lease_seconds,
            token=token,
            worker_id=worker_id,
            leader_epoch=leader_epoch,
            now=now,
        )

    def claim_event(
        self,
        row_id: int | str,
        *,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
        token: str | None = None,
        worker_id: str | None = None,
        leader_epoch: int | None = None,
        now: datetime | None = None,
    ) -> ClaimToken | None:
        """Claim one event-inbox row for webhook/event processing."""

        return self.claim(
            "event_inbox",
            row_id,
            lease_seconds=lease_seconds,
            token=token,
            worker_id=worker_id,
            leader_epoch=leader_epoch,
            now=now,
        )

    def claim_request_command(
        self,
        row_id: int | str,
        *,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
        token: str | None = None,
        worker_id: str | None = None,
        leader_epoch: int | None = None,
        now: datetime | None = None,
    ) -> ClaimToken | None:
        """Claim one request command for provider-side work."""

        return self.claim(
            "request_commands",
            row_id,
            lease_seconds=lease_seconds,
            token=token,
            worker_id=worker_id,
            leader_epoch=leader_epoch,
            now=now,
        )

    def complete_claim(
        self,
        table: str,
        row_id: int | str,
        token: str | ClaimToken,
        *,
        status: str | None = None,
        updates: Mapping[str, Any] | None = None,
        id_column: str = "id",
        now: datetime | None = None,
    ) -> bool:
        """Complete a claimed row only when the opaque token still matches."""

        claim = token if isinstance(token, ClaimToken) else None
        return self._finish_claim(
            table,
            row_id,
            _token_text(token),
            status=self._default_completion_status(table) if status is None else status,
            updates=updates,
            id_column=id_column,
            clear=True,
            claim_epoch=None if claim is None else claim.leader_epoch,
            claim_version=None if claim is None else claim.version,
            now=now,
        )

    def release_claim(
        self,
        table: str,
        row_id: int | str,
        token: str | ClaimToken,
        *,
        status: str = "pending",
        error: str | None = None,
        retry_at: datetime | str | None = None,
        id_column: str = "id",
        now: datetime | None = None,
    ) -> bool:
        """Release a claim using CAS semantics, optionally scheduling retry."""

        updates: dict[str, Any] = {}
        if error is not None or retry_at is not None:
            with self.connection() as connection:
                columns = self._table_columns(
                    connection, _identifier(table, "table name")
                )
            if error is not None:
                if "last_error" in columns:
                    updates["last_error"] = _safe_error_text(error)
                elif "error_text" in columns:
                    updates["error_text"] = _safe_error_text(error)
                else:
                    raise DatabaseError(f"table {table!r} has no error column")
            if retry_at is not None:
                retry_value = (
                    utc_timestamp(retry_at)
                    if isinstance(retry_at, datetime)
                    else _canonical_timestamp(retry_at)
                )
                if "available_at" in columns:
                    updates["available_at"] = retry_value
                elif "retry_due_at" in columns:
                    updates["retry_due_at"] = retry_value
                else:
                    raise DatabaseError(
                        f"table {table!r} has no retry scheduling column"
                    )
        return self._finish_claim(
            table,
            row_id,
            _token_text(token),
            status=status,
            updates=updates,
            id_column=id_column,
            clear=True,
            claim_epoch=(
                None if not isinstance(token, ClaimToken) else token.leader_epoch
            ),
            claim_version=(
                None if not isinstance(token, ClaimToken) else token.version
            ),
            now=now,
        )

    def renew_claim(
        self,
        table: str,
        row_id: int | str,
        token: str | ClaimToken,
        *,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
        id_column: str = "id",
        now: datetime | None = None,
    ) -> ClaimToken | None:
        """Extend a live claim without allowing an expired token to return."""

        self._validate_lease_seconds(lease_seconds)
        table = _identifier(table, "table name")
        id_column = _identifier(id_column, "id column")
        token_value = _token_text(token)
        current = self.observe_clock(now)
        now_text = utc_timestamp(current)
        expires_at = utc_timestamp(current + timedelta(seconds=lease_seconds))
        with self.transaction() as connection:
            columns = self._table_columns(connection, table)
            required = {id_column, "claim_token", "claim_expires_at"}
            missing = required - columns
            if missing:
                raise DatabaseError(
                    f"table {table!r} cannot be renewed; missing {sorted(missing)!r}"
                )
            epoch_predicate: int | None = None
            if "claim_epoch" in columns:
                leader_live, active_epoch = self._leader_state(connection, now_text)
                if isinstance(token, ClaimToken):
                    if token.leader_epoch > 0 and (
                        not leader_live or active_epoch != token.leader_epoch
                    ):
                        return None
                    if token.leader_epoch == 0 and leader_live:
                        return None
                epoch_predicate = active_epoch if leader_live else 0
            assignments = ["claim_expires_at = ?"]
            parameters: list[Any] = [expires_at]
            if "updated_at" in columns:
                assignments.append("updated_at = ?")
                parameters.append(now_text)
            if "version" in columns:
                assignments.append("version = version + 1")
            if "claim_version" in columns:
                assignments.append("claim_version = claim_version + 1")
            predicates = [f"{id_column} = ?", "claim_token = ?", "claim_expires_at > ?"]
            predicate_parameters: list[Any] = [row_id, token_value, now_text]
            if epoch_predicate is not None:
                predicates.append("COALESCE(claim_epoch, 0) = ?")
                predicate_parameters.append(epoch_predicate)
            if isinstance(token, ClaimToken):
                if "claim_version" in columns and token.version is not None:
                    predicates.append("claim_version = ?")
                    predicate_parameters.append(token.version)
            result = connection.execute(
                f"UPDATE {table} SET {', '.join(assignments)} "
                f"WHERE {' AND '.join(predicates)}",
                (*parameters, *predicate_parameters),
            )
            if result.rowcount != 1:
                return None
            version = None
            if "version" in columns:
                row = connection.execute(
                    f"SELECT version FROM {table} WHERE {id_column} = ?",
                    (row_id,),
                ).fetchone()
                version = None if row is None else int(row[0])
            leader_epoch = 0
            if "claim_epoch" in columns:
                row = connection.execute(
                    f"SELECT claim_epoch FROM {table} WHERE {id_column} = ?",
                    (row_id,),
                ).fetchone()
                leader_epoch = 0 if row is None or row[0] is None else int(row[0])
            return ClaimToken(
                token_value, table, row_id, expires_at, version, leader_epoch
            )

    def compare_and_swap(
        self,
        table: str,
        row_id: int | str,
        expected_version: int | ClaimToken | Mapping[str, Any] | None = None,
        updates: Mapping[str, Any] | None = None,
        *,
        expected: Mapping[str, Any] | None = None,
        id_column: str = "id",
        now: datetime | None = None,
    ) -> bool:
        """Update a row only if its version/token predicates still match.

        ``expected_version`` is the common optimistic-lock form.  For a
        token-based CAS, pass ``expected={"claim_token": token}``; arbitrary
        expected column values are supported as well.
        """

        if isinstance(expected_version, ClaimToken):
            if expected is not None:
                raise TypeError("pass expected predicates once")
            expected = {"claim_token": expected_version}
            expected_version = None
        if isinstance(expected_version, Mapping):
            if expected is not None:
                raise TypeError("pass expected predicates once")
            expected = expected_version
            expected_version = None
        predicates: dict[str, Any] = dict(expected or {})
        claim_expectation: ClaimToken | None = None
        for key, value in tuple(predicates.items()):
            if isinstance(value, ClaimToken):
                claim_expectation = value
                predicates[key] = value.token
        if expected_version is not None:
            predicates["version"] = expected_version
        if not predicates:
            raise ValueError("compare_and_swap requires expected_version or expected")
        if updates is None:
            raise ValueError("compare_and_swap requires updates")
        if not updates:
            raise ValueError("compare_and_swap updates cannot be empty")
        table = _identifier(table, "table name")
        id_column = _identifier(id_column, "id column")
        current_text = utc_timestamp(self.observe_clock(now))

        with self.transaction() as connection:
            columns = self._table_columns(connection, table)
            for column in (*predicates.keys(), *updates.keys()):
                if column not in columns:
                    raise DatabaseError(f"unknown column {column!r} on table {table!r}")
            assignments: list[str] = []
            parameters: list[Any] = []
            for column, value in updates.items():
                assignments.append(f"{_identifier(column, 'column')} = ?")
                parameters.append(value)
            if "version" in columns and "version" not in updates:
                assignments.append("version = version + 1")
            if "updated_at" in columns and "updated_at" not in updates:
                assignments.append("updated_at = ?")
                parameters.append(current_text)
            where = [f"{id_column} = ?"]
            where_parameters: list[Any] = [row_id]
            for column, value in predicates.items():
                identifier = _identifier(column, "column")
                if value is None:
                    where.append(f"{identifier} IS NULL")
                else:
                    where.append(f"{identifier} = ?")
                    where_parameters.append(value)
            if "claim_token" in predicates and "claim_expires_at" in columns:
                where.append("claim_expires_at > ?")
                where_parameters.append(current_text)
            if (
                claim_expectation is not None
                and "claim_version" in columns
                and claim_expectation.version is not None
            ):
                where.append("claim_version = ?")
                where_parameters.append(claim_expectation.version)
            if "claim_epoch" in columns and "claim_token" in predicates:
                leader_live, active_epoch = self._leader_state(connection, current_text)
                if claim_expectation is not None:
                    if claim_expectation.leader_epoch > 0 and (
                        not leader_live
                        or active_epoch != claim_expectation.leader_epoch
                    ):
                        return False
                    if claim_expectation.leader_epoch == 0 and leader_live:
                        return False
                where.append("COALESCE(claim_epoch, 0) = ?")
                where_parameters.append(active_epoch if leader_live else 0)
            result = connection.execute(
                f"UPDATE {table} SET {', '.join(assignments)} WHERE {' AND '.join(where)}",
                (*parameters, *where_parameters),
            )
            return result.rowcount == 1

    cas_update = compare_and_swap
    cas = compare_and_swap

    def claim_resource(
        self,
        resource_type: str,
        resource_id: int | str,
        *,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
        now: datetime | None = None,
        owner: str | None = None,
        token: str | None = None,
        leader_epoch: int | None = None,
    ) -> ClaimToken | None:
        """Claim an arbitrary resource in the generic ``claim_leases`` table."""

        self._validate_lease_seconds(lease_seconds)
        if leader_epoch is not None and (
            isinstance(leader_epoch, bool)
            or not isinstance(leader_epoch, int)
            or leader_epoch < 0
        ):
            raise ValueError("leader_epoch must be a non-negative integer")
        if token is not None and (
            not isinstance(token, str) or len(token.encode("utf-8")) < 32
        ):
            raise ValueError("caller-supplied claim token is too short")
        claim_token = _token_text(token) if token is not None else _new_token()
        now = self.observe_clock(now)
        claimed_at = utc_timestamp(now)
        expires_at = utc_timestamp(now + timedelta(seconds=lease_seconds))
        with self.transaction() as connection:
            effective_epoch = 0 if leader_epoch is None else leader_epoch
            leader = connection.execute(
                "SELECT epoch, owner, expires_at FROM leader_leases "
                "WHERE lease_name = 'media'"
            ).fetchone()
            if (
                leader is not None
                and leader[1] is not None
                and leader[2] is not None
                and str(leader[2]) > claimed_at
            ):
                if leader_epoch is None or int(leader[0]) != leader_epoch:
                    return None
                effective_epoch = int(leader[0])
            elif leader_epoch is not None and leader_epoch > 0:
                return None
            existing = connection.execute(
                """
                SELECT claim_token, version, expires_at, claim_epoch
                FROM claim_leases
                WHERE resource_type = ? AND resource_id = ?
                """,
                (resource_type, str(resource_id)),
            ).fetchone()
            if existing is None:
                try:
                    connection.execute(
                        """
                        INSERT INTO claim_leases
                            (resource_type, resource_id, claim_token, owner,
                             claimed_at, expires_at, version, claim_epoch)
                        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                        """,
                        (
                            resource_type,
                            str(resource_id),
                            claim_token,
                            owner,
                            claimed_at,
                            expires_at,
                            effective_epoch,
                        ),
                    )
                except sqlite3.IntegrityError:
                    return None
                return ClaimToken(
                    claim_token,
                    "claim_leases",
                    str(resource_id),
                    expires_at,
                    0,
                    effective_epoch,
                )
            if str(existing[2]) > claimed_at:
                return None
            result = connection.execute(
                """
                UPDATE claim_leases
                SET claim_token = ?, owner = ?, claimed_at = ?, expires_at = ?,
                    version = version + 1, claim_epoch = ?
                WHERE resource_type = ? AND resource_id = ?
                  AND (expires_at <= ? OR expires_at IS NULL)
                """,
                (
                    claim_token,
                    owner,
                    claimed_at,
                    expires_at,
                    effective_epoch,
                    resource_type,
                    str(resource_id),
                    claimed_at,
                ),
            )
            if result.rowcount != 1:
                return None
            return ClaimToken(
                claim_token,
                "claim_leases",
                str(resource_id),
                expires_at,
                int(existing[1]) + 1,
                effective_epoch,
            )

    def release_resource(
        self,
        resource_type: str,
        resource_id: int | str,
        token: str | ClaimToken,
        *,
        now: datetime | None = None,
    ) -> bool:
        current_text = utc_timestamp(self.observe_clock(now))
        with self.transaction() as connection:
            leader_live, active_epoch = self._leader_state(connection, current_text)
            if isinstance(token, ClaimToken):
                if token.leader_epoch > 0 and (
                    not leader_live or active_epoch != token.leader_epoch
                ):
                    return False
                if token.leader_epoch == 0 and leader_live:
                    return False
            expected_epoch = active_epoch if leader_live else 0
            result = connection.execute(
                """
                DELETE FROM claim_leases
                WHERE resource_type = ? AND resource_id = ? AND claim_token = ?
                  AND (? IS NULL OR version = ?)
                  AND COALESCE(claim_epoch, 0) = ?
                  AND expires_at > ?
                """,
                (
                    resource_type,
                    str(resource_id),
                    _token_text(token),
                    None if not isinstance(token, ClaimToken) else token.version,
                    None if not isinstance(token, ClaimToken) else token.version,
                    expected_epoch,
                    current_text,
                ),
            )
            return result.rowcount == 1

    def renew_resource(
        self,
        resource_type: str,
        resource_id: int | str,
        token: str | ClaimToken,
        *,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> ClaimToken | None:
        """Renew a generic resource lease only while its token is live."""

        self._validate_lease_seconds(lease_seconds)
        token_value = _token_text(token)
        now = self.observe_clock(now)
        now_text = utc_timestamp(now)
        expires_at = utc_timestamp(now + timedelta(seconds=lease_seconds))
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT version, claim_epoch FROM claim_leases
                WHERE resource_type = ? AND resource_id = ?
                  AND claim_token = ? AND expires_at > ?
                """,
                (resource_type, str(resource_id), token_value, now_text),
            ).fetchone()
            if row is None:
                return None
            leader_live, active_epoch = self._leader_state(connection, now_text)
            row_epoch = 0 if row[1] is None else int(row[1])
            if isinstance(token, ClaimToken):
                if token.leader_epoch > 0 and (
                    not leader_live or active_epoch != token.leader_epoch
                ):
                    return None
                if token.leader_epoch == 0 and leader_live:
                    return None
            if leader_live:
                if row_epoch != active_epoch:
                    return None
            elif row_epoch != 0:
                return None
            extra_where = ""
            extra_parameters: tuple[Any, ...] = ()
            if isinstance(token, ClaimToken):
                extra_where = " AND version = ? AND claim_epoch = ?"
                extra_parameters = (token.version, token.leader_epoch)
            result = connection.execute(
                """
                UPDATE claim_leases
                SET expires_at = ?, version = version + 1
                WHERE resource_type = ? AND resource_id = ?
                  AND claim_token = ? AND expires_at > ?
                """
                + extra_where,
                (
                    expires_at,
                    resource_type,
                    str(resource_id),
                    token_value,
                    now_text,
                    *extra_parameters,
                ),
            )
            if result.rowcount != 1:
                return None
            return ClaimToken(
                token_value,
                "claim_leases",
                str(resource_id),
                expires_at,
                int(row[0]) + 1,
                0 if row[1] is None else int(row[1]),
            )

    def _finish_claim(
        self,
        table: str,
        row_id: int | str,
        token: str,
        *,
        status: str,
        updates: Mapping[str, Any] | None,
        id_column: str,
        clear: bool,
        claim_epoch: int | None = None,
        claim_version: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        table = _identifier(table, "table name")
        id_column = _identifier(id_column, "id column")
        update_values = dict(updates or {})
        update_values["status"] = status
        current = self.observe_clock(now)
        now_text = utc_timestamp(current)
        with self.transaction() as connection:
            columns = self._table_columns(connection, table)
            for column in update_values:
                if column not in columns:
                    raise DatabaseError(f"unknown column {column!r} on table {table!r}")
            assignments: list[str] = []
            parameters: list[Any] = []
            for column, value in update_values.items():
                assignments.append(f"{_identifier(column, 'column')} = ?")
                parameters.append(value)
            if clear:
                assignments.extend(["claim_token = NULL", "claim_expires_at = NULL"])
                for column in (
                    "claim_version",
                    "claim_epoch",
                    "claim_worker",
                    "claimed_at",
                ):
                    if column in columns:
                        assignments.append(f"{column} = NULL")
            if "version" in columns:
                assignments.append("version = version + 1")
            if "claim_version" in columns and not clear:
                assignments.append("claim_version = claim_version + 1")
            if "updated_at" in columns:
                assignments.append("updated_at = ?")
                parameters.append(now_text)
            where = [f"{id_column} = ?", "claim_token = ?"]
            where_parameters: list[Any] = [row_id, token]
            if "claim_epoch" in columns:
                # Fence both structured ClaimToken callers and legacy callers
                # that still pass only the opaque token.  A token minted before
                # a leader existed must not become usable after a live epoch is
                # acquired, and a token from an expired epoch must not revive
                # after leadership changes.
                leader_live, active_epoch = self._leader_state(connection, now_text)
                if claim_epoch is not None:
                    if claim_epoch > 0 and (
                        not leader_live or active_epoch != claim_epoch
                    ):
                        return False
                    if claim_epoch == 0 and leader_live:
                        return False
                if leader_live:
                    where.append("COALESCE(claim_epoch, 0) = ?")
                    where_parameters.append(active_epoch)
                else:
                    where.append("COALESCE(claim_epoch, 0) = 0")
            if "claim_expires_at" in columns:
                where.append("claim_expires_at > ?")
                where_parameters.append(now_text)
            if claim_version is not None and "claim_version" in columns:
                where.append("claim_version = ?")
                where_parameters.append(claim_version)
            result = connection.execute(
                f"UPDATE {table} SET {', '.join(assignments)} WHERE {' AND '.join(where)}",
                (*parameters, *where_parameters),
            )
            return result.rowcount == 1

    @staticmethod
    def _default_completion_status(table: str) -> str:
        table_name = _identifier(table, "table name")
        if table_name == "event_inbox":
            return "handled"
        if table_name == "request_commands":
            return "succeeded"
        return "sent"

    def _ensure_management_tables(self) -> None:
        with self.connection() as connection:
            self._ensure_management_tables_on_connection(connection)

    @staticmethod
    def _ensure_management_tables_on_connection(
        connection: sqlite3.Connection,
    ) -> None:
        """Bootstrap only the migration ledger; schema changes stay versioned.

        This helper deliberately does not ``ALTER`` an existing management
        table.  A partial/foreign ledger must be repaired by an explicit
        ordered migration or rejected, never silently rewritten by every
        caller that happens to open the database.
        """

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
                rollback_compatible INTEGER NOT NULL DEFAULT 1
                    CHECK (rollback_compatible IN (0, 1))
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS migration_accounting (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                migration_version INTEGER NOT NULL,
                migration_name TEXT NOT NULL,
                source_name TEXT NOT NULL DEFAULT 'data',
                status TEXT NOT NULL DEFAULT 'planned'
                    CHECK (status IN ('planned', 'running', 'completed', 'failed', 'skipped')),
                source_rows INTEGER NOT NULL DEFAULT 0 CHECK (source_rows >= 0),
                migrated_rows INTEGER NOT NULL DEFAULT 0 CHECK (migrated_rows >= 0),
                skipped_rows INTEGER NOT NULL DEFAULT 0 CHECK (skipped_rows >= 0),
                failed_rows INTEGER NOT NULL DEFAULT 0 CHECK (failed_rows >= 0),
                started_at TEXT,
                completed_at TEXT,
                details_json TEXT,
                error_text TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE(migration_version, source_name)
            )
            """
        )

    def _read_migration_rows(
        self, connection: sqlite3.Connection
    ) -> list[MigrationRecord]:
        rows = connection.execute(
            """
            SELECT version, name, checksum, applied_at, duration_ms
            FROM schema_migrations ORDER BY version
            """
        ).fetchall()
        return [
            MigrationRecord(
                version=int(row[0]),
                name=str(row[1]),
                checksum=str(row[2]),
                applied_at=str(row[3]),
                duration_ms=int(row[4]),
            )
            for row in rows
        ]

    @staticmethod
    def _validate_applied_history(records: Sequence[MigrationRecord]) -> None:
        versions = [record.version for record in records]
        expected = list(range(1, len(records) + 1))
        if versions != expected:
            raise MigrationOrderError(
                f"applied migrations must be contiguous from 1 (got {versions!r})"
            )

    @staticmethod
    def _validate_user_version(
        connection: sqlite3.Connection,
        records: Sequence[MigrationRecord],
    ) -> None:
        """Reject a marker that claims more schema than the immutable ledger.

        SQLite's ``user_version`` is updated after each migration commit.  A
        process crash can leave it stale, so a marker below the ledger is
        repaired; a marker above it is evidence of an out-of-band schema
        change and must fail closed.
        """

        row = connection.execute("PRAGMA user_version").fetchone()
        if row is None or len(row) != 1:
            raise MigrationIntegrityError("SQLite user_version could not be read")
        marker = row[0]
        if isinstance(marker, bool) or not isinstance(marker, int) or marker < 0:
            raise MigrationIntegrityError("SQLite user_version is invalid")
        highest = records[-1].version if records else 0
        if marker > highest:
            raise MigrationIntegrityError(
                f"SQLite user_version {marker} is ahead of migration ledger {highest}"
            )

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
        table = _identifier(table, "table name")
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            # PRAGMA returns no rows for a missing table; a later operation can
            # produce a clearer message than an interpolated identifier.
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if exists is None:
                raise DatabaseError(f"SQLite table does not exist: {table!r}")
        return {str(row[1]) for row in rows}

    @staticmethod
    def _leader_state(
        connection: sqlite3.Connection,
        now_text: str,
    ) -> tuple[bool, int | None]:
        """Return the live media leader and its fencing epoch, if any."""

        row = connection.execute(
            "SELECT epoch, owner, claim_token, expires_at FROM leader_leases "
            "WHERE lease_name = 'media'"
        ).fetchone()
        if (
            row is None
            or row[1] is None
            or row[2] is None
            or row[3] is None
            or str(row[3]) <= now_text
        ):
            return False, None
        return True, int(row[0])

    def _record_failed_migration(
        self, migration: Migration, error: BaseException
    ) -> None:
        try:
            with self.connection() as connection:
                self._record_failed_migration_on_connection(
                    connection, migration, error
                )
        except sqlite3.Error:
            # Preserve the original migration exception if the accounting
            # table itself was damaged or the filesystem became unavailable.
            pass

    @staticmethod
    def _record_failed_migration_on_connection(
        connection: sqlite3.Connection,
        migration: Migration,
        error: BaseException,
    ) -> None:
        safe_error = _safe_error_text(error)
        now = utc_timestamp()
        connection.execute(
            """
            INSERT INTO migration_accounting
                (migration_version, migration_name, source_name, status,
                 source_rows, failed_rows, started_at, completed_at, error_text)
            VALUES (?, ?, 'schema', 'failed', 1, 1, ?, ?, ?)
            ON CONFLICT(migration_version, source_name)
            DO UPDATE SET status = 'failed', source_rows = source_rows + 1,
                          failed_rows = failed_rows + 1,
                          completed_at = excluded.completed_at,
                          error_text = excluded.error_text
            """,
            (migration.version, migration.name, now, now, safe_error),
        )

    @staticmethod
    def _schema_snapshot(
        connection: sqlite3.Connection,
    ) -> tuple[str, tuple[tuple[str, int], ...]]:
        schema_rows = connection.execute(
            """
            SELECT type, name, COALESCE(sql, '')
            FROM sqlite_master
            WHERE type IN ('table', 'index', 'trigger', 'view')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        schema_payload = "\n".join(
            f"{row[0]}\x1f{row[1]}\x1f{row[2]}" for row in schema_rows
        ).encode("utf-8")
        schema_checksum = hashlib.sha256(schema_payload).hexdigest()
        table_counts: list[tuple[str, int]] = []
        for row in schema_rows:
            if row[0] != "table":
                continue
            table_name = str(row[1])
            count = connection.execute(
                f"SELECT COUNT(*) FROM {_identifier(table_name, 'table name')}"
            ).fetchone()[0]
            table_counts.append((table_name, int(count)))
        return schema_checksum, tuple(table_counts)

    @classmethod
    def _verify_backup_connections(
        cls,
        source: sqlite3.Connection,
        target: sqlite3.Connection,
        destination: Path,
        pages_copied: int,
    ) -> BackupReport:
        quick_check = str(target.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_errors = tuple(
            tuple(row) for row in target.execute("PRAGMA foreign_key_check").fetchall()
        )
        source_schema, source_counts = cls._schema_snapshot(source)
        destination_schema, destination_counts = cls._schema_snapshot(target)
        source_data = cls._data_checksum(source, source_counts)
        destination_data = cls._data_checksum(target, destination_counts)
        verified = (
            quick_check.lower() == "ok"
            and integrity.lower() == "ok"
            and not foreign_key_errors
            and source_schema == destination_schema
            and source_counts == destination_counts
            and source_data == destination_data
        )
        return BackupReport(
            destination=destination,
            integrity_check=integrity,
            foreign_key_errors=foreign_key_errors,
            source_schema_checksum=source_schema,
            destination_schema_checksum=destination_schema,
            source_data_checksum=source_data,
            destination_data_checksum=destination_data,
            source_table_counts=source_counts,
            destination_table_counts=destination_counts,
            pages_copied=pages_copied,
            verified=verified,
            quick_check=quick_check,
        )

    @staticmethod
    def _data_checksum(
        connection: sqlite3.Connection,
        table_counts: Sequence[tuple[str, int]],
    ) -> str:
        """Hash SQLite-returned table values for backup content verification."""

        digest = hashlib.sha256()
        for table_name, _count in table_counts:
            digest.update(table_name.encode("utf-8"))
            digest.update(b"\0")
            columns = connection.execute(
                f"PRAGMA table_info({_identifier(table_name, 'table name')})"
            ).fetchall()
            column_names = [str(column[1]) for column in columns]
            selected = ", ".join(
                _identifier(column, "column") for column in column_names
            )
            try:
                rows = connection.execute(
                    f"SELECT {selected} FROM {_identifier(table_name, 'table name')} "
                    "ORDER BY rowid"
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such column: rowid" not in str(exc).lower():
                    raise
                rows = connection.execute(
                    f"SELECT {selected} FROM {_identifier(table_name, 'table name')}"
                ).fetchall()
            for row in rows:
                for value in row:
                    if isinstance(value, bytes):
                        encoded = b"bytes:" + value.hex().encode("ascii")
                    else:
                        encoded = repr((type(value).__name__, value)).encode("utf-8")
                    digest.update(len(encoded).to_bytes(8, "big"))
                    digest.update(encoded)
        return digest.hexdigest()


class SQLiteNonceReplayStore:
    """Database-backed implementation of the auth nonce replay protocol."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def consume(
        self,
        nonce: str,
        expires_at: int,
        *,
        now: float | None = None,
    ) -> bool:
        return self.database.consume_actor_nonce(nonce, expires_at, now=now)

    check_and_store = consume
    reserve = consume
    put_if_absent = consume
    consume_once = consume

    def cleanup(self, *, now: float | None = None, limit: int = 100) -> int:
        return self.database.cleanup_actor_nonces(now=now, limit=limit)


class SQLiteConfirmationTokenStore:
    """Hash-only, transactional implementation of the confirmation protocol."""

    def __init__(
        self,
        database: Database,
        *,
        ttl: int = DEFAULT_CONFIRMATION_TTL_SECONDS,
        policy_version: str = "1",
        clock: Any = time.time,
    ) -> None:
        if not isinstance(ttl, int) or isinstance(ttl, bool):
            raise TypeError("ttl must be an integer")
        if ttl <= 0 or ttl > DEFAULT_CONFIRMATION_TTL_SECONDS:
            raise ValueError(
                f"ttl must be between 1 and {DEFAULT_CONFIRMATION_TTL_SECONDS} seconds"
            )
        self.database = database
        self.ttl = ttl
        self.clock = clock
        self.policy_version = self._text("policy_version", policy_version)
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT current_version FROM confirmation_policy_state "
                    "WHERE policy_name = 'confirmation'"
                ).fetchone()
            if row is not None:
                self.policy_version = self._text("policy_version", str(row[0]))
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise

    @staticmethod
    def _now(value: float | None, clock: Any) -> int:
        current = clock() if value is None else value
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            raise ValueError("confirmation clock must return a number")
        current_float = float(current)
        if (
            not math.isfinite(current_float)
            or abs(current_float) > 9_007_199_254_740_991
        ):
            raise ValueError("confirmation clock must return a finite safe number")
        return int(current_float)

    @staticmethod
    def _token_value(token: str | Any) -> str:
        value = getattr(token, "value", token)
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
            from .auth import ConfirmationError

            raise ConfirmationError("confirmation token must be 256-bit base64url")
        return value

    @staticmethod
    def _token_hash(token: str | Any) -> str:
        value = SQLiteConfirmationTokenStore._token_value(token)
        return hashlib.sha256(value.encode("ascii")).hexdigest()

    @staticmethod
    def _preview_hash(preview: str | bytes) -> str:
        if isinstance(preview, str):
            try:
                value = preview.encode("utf-8", "strict")
            except UnicodeEncodeError as exc:
                from .auth import ConfirmationError

                raise ConfirmationError("preview must be valid UTF-8") from exc
        elif isinstance(preview, bytes):
            value = preview
        else:
            from .auth import ConfirmationError

            raise ConfirmationError("preview must be text or bytes")
        if len(value) > 64 * 1024:
            from .auth import ConfirmationError

            raise ConfirmationError("preview exceeds the confirmation bound")
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _text(name: str, value: str) -> str:
        if not isinstance(value, str) or not value:
            from .auth import ConfirmationError

            raise ConfirmationError(f"{name} is empty or too long")
        try:
            encoded = value.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            from .auth import ConfirmationError

            raise ConfirmationError(f"{name} must be valid UTF-8") from exc
        if len(encoded) > 512:
            from .auth import ConfirmationError

            raise ConfirmationError(f"{name} is empty or too long")
        return value

    @staticmethod
    def _positive_id(name: str, value: int) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            or value > 9_007_199_254_740_991
        ):
            from .auth import ConfirmationError

            raise ConfirmationError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _digest(name: str, value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            from .auth import ConfirmationError

            raise ConfirmationError(f"{name} must be a lowercase SHA-256 digest")
        return value

    @staticmethod
    def _reserve_assertion_nonce(
        connection: sqlite3.Connection,
        nonce: str,
        expires_at: int,
        now: int,
    ) -> None:
        from .auth import ConfirmationReplayError

        if not isinstance(nonce, str) or not nonce or len(nonce.encode("utf-8")) > 512:
            raise ValueError("assertion nonce must be bounded and non-empty")
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or abs(expires_at) > 9_007_199_254_740_991
        ):
            raise ValueError("assertion expiry must be a safe integer")
        if expires_at <= now:
            raise ConfirmationReplayError("assertion nonce has expired")
        nonce_hash = hashlib.sha256(nonce.encode("utf-8", "strict")).hexdigest()
        connection.execute(
            "DELETE FROM actor_nonces WHERE nonce_hash IN ("
            "SELECT nonce_hash FROM actor_nonces WHERE expires_at <= ? "
            "ORDER BY nonce_hash LIMIT 100)",
            (now,),
        )
        connection.execute(
            "DELETE FROM actor_nonces WHERE nonce_hash = ? AND expires_at <= ?",
            (nonce_hash, now),
        )
        result = connection.execute(
            "INSERT OR IGNORE INTO actor_nonces"
            " (nonce_hash, consumed_at, expires_at) VALUES (?, ?, ?)",
            (nonce_hash, now, expires_at),
        )
        if result.rowcount != 1:
            raise ConfirmationReplayError("assertion nonce has already been consumed")

    @staticmethod
    def _record(row: sqlite3.Row) -> Any:
        from .auth import ConfirmationRecord

        return ConfirmationRecord(
            token_hash=str(row["token_hash"]),
            actor_user_id=int(row["actor_user_id"]),
            actor_chat_id=int(row["actor_chat_id"]),
            tool=str(row["tool"]),
            argument_hash=str(row["argument_hash"]),
            target_identity=str(row["target_identity"]),
            state_fingerprint=str(row["state_fingerprint"]),
            preview_hash=str(row["preview_hash"]),
            policy_version=str(row["policy_version"]),
            nonce=str(row["nonce"]),
            issued_at=int(row["issued_at"]),
            expires_at=int(row["expires_at"]),
            state=str(row["state"]),
            bound_chat_id=(
                None if row["bound_chat_id"] is None else int(row["bound_chat_id"])
            ),
            bound_message_id=(
                None
                if row["bound_message_id"] is None
                else int(row["bound_message_id"])
            ),
            consumed_at=(
                None if row["consumed_at"] is None else int(row["consumed_at"])
            ),
        )

    @staticmethod
    def _raise_expired(
        connection: sqlite3.Connection, row: sqlite3.Row, now: int
    ) -> None:
        from .auth import ConfirmationExpired

        state = str(row["state"])
        if state == "expired" or (
            int(row["expires_at"]) <= now and state not in {"consumed", "revoked"}
        ):
            raise ConfirmationExpired("confirmation token has expired")

    def _mark_expired_if_needed(self, token_hash: str, now: int) -> bool:
        """Persist expiry in its own transaction before lifecycle work."""

        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT state, expires_at FROM confirmation_capabilities "
                "WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None or str(row[0]) in {"consumed", "revoked", "expired"}:
                return False
            if int(row[1]) > now:
                return False
            result = connection.execute(
                "UPDATE confirmation_capabilities SET state = 'expired', "
                "version = version + 1, updated_at = ? WHERE token_hash = ? "
                "AND state NOT IN ('consumed', 'revoked', 'expired')",
                (utc_timestamp(), token_hash),
            )
            return result.rowcount == 1

    def create(
        self,
        *,
        actor_user_id: int,
        actor_chat_id: int,
        tool: str,
        argument_hash: str,
        target_identity: str,
        state_fingerprint: str,
        preview: str | bytes,
        policy_version: str | None = None,
        now: float | None = None,
        nonce: str | None = None,
    ) -> Any:
        from .auth import ConfirmationToken

        actor_user_id = self._positive_id("actor_user_id", actor_user_id)
        actor_chat_id = self._positive_id("actor_chat_id", actor_chat_id)
        tool = self._text("tool", tool)
        argument_hash = self._digest("argument_hash", argument_hash)
        target_identity = self._text("target_identity", target_identity)
        state_fingerprint = self._text("state_fingerprint", state_fingerprint)
        requested_policy = (
            self.policy_version
            if policy_version is None
            else self._text("policy_version", policy_version)
        )
        record_nonce = (
            self._text("nonce", nonce)
            if nonce is not None
            else secrets.token_urlsafe(16)
        )
        preview_hash = self._preview_hash(preview)
        issued_at = self._now(now, self.clock)
        expires_at = issued_at + self.ttl

        for _attempt in range(3):
            value = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(value.encode("ascii")).hexdigest()
            try:
                with self.database.transaction() as connection:
                    state = connection.execute(
                        "SELECT current_version FROM confirmation_policy_state "
                        "WHERE policy_name = 'confirmation'"
                    ).fetchone()
                    if state is not None:
                        current_policy = self._text("policy_version", str(state[0]))
                        self.policy_version = current_policy
                        if requested_policy != current_policy:
                            from .auth import ConfirmationBindingError

                            raise ConfirmationBindingError(
                                "confirmation policy version is not current"
                            )
                    else:
                        if requested_policy != self.policy_version:
                            from .auth import ConfirmationBindingError

                            raise ConfirmationBindingError(
                                "confirmation policy version is not current"
                            )
                        connection.execute(
                            "INSERT INTO confirmation_policy_state"
                            " (policy_name, current_version, updated_at)"
                            " VALUES ('confirmation', ?, ?)",
                            (self.policy_version, utc_timestamp()),
                        )
                    connection.execute(
                        """
                        INSERT INTO confirmation_capabilities
                            (token_hash, actor_user_id, actor_chat_id, tool,
                             argument_hash, target_identity, state_fingerprint,
                             preview_hash, policy_version, nonce, issued_at,
                             expires_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            token_hash,
                            actor_user_id,
                            actor_chat_id,
                            tool,
                            argument_hash,
                            target_identity,
                            state_fingerprint,
                            preview_hash,
                            requested_policy,
                            record_nonce,
                            issued_at,
                            expires_at,
                        ),
                    )
                return ConfirmationToken(value, token_hash, issued_at, expires_at)
            except sqlite3.IntegrityError:
                if _attempt == 2:
                    raise
        raise DatabaseError("confirmation token was not stored")  # pragma: no cover

    issue = create
    mint = create

    def bind(
        self,
        token: str | Any,
        *,
        chat_id: int,
        message_id: int,
        preview: str | bytes,
        now: float | None = None,
    ) -> Any:
        from .auth import ConfirmationBindingError, ConfirmationReplayError

        chat_id = self._positive_id("chat_id", chat_id)
        message_id = self._positive_id("message_id", message_id)
        token_hash = self._token_hash(token)
        preview_hash = self._preview_hash(preview)
        current = self._now(now, self.clock)
        if self._mark_expired_if_needed(token_hash, current):
            from .auth import ConfirmationExpired

            raise ConfirmationExpired("confirmation token has expired")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM confirmation_capabilities WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                from .auth import ConfirmationError

                raise ConfirmationError("unknown confirmation token")
            self._raise_expired(connection, row, current)
            state = str(row["state"])
            if state == "consumed":
                raise ConfirmationReplayError("confirmation token was already consumed")
            if state == "armed":
                if (
                    row["bound_chat_id"] is not None
                    and row["bound_message_id"] is not None
                    and int(row["bound_chat_id"]) == chat_id
                    and int(row["bound_message_id"]) == message_id
                    and hmac.compare_digest(str(row["preview_hash"]), preview_hash)
                ):
                    return self._record(row)
                raise ConfirmationBindingError("confirmation token is already bound")
            if state != "pending_bind":
                raise ConfirmationBindingError(
                    "confirmation token is not awaiting bind"
                )
            if int(row["actor_chat_id"]) != chat_id:
                raise ConfirmationBindingError(
                    "confirmation chat does not match actor chat"
                )
            if not hmac.compare_digest(str(row["preview_hash"]), preview_hash):
                raise ConfirmationBindingError(
                    "preview text does not match exact server preview"
                )
            result = connection.execute(
                """
                UPDATE confirmation_capabilities
                SET state = 'armed', bound_chat_id = ?, bound_message_id = ?,
                    bound_at = ?, version = version + 1, updated_at = ?
                WHERE token_hash = ? AND state = 'pending_bind' AND version = ?
                """,
                (
                    chat_id,
                    message_id,
                    utc_timestamp(),
                    utc_timestamp(),
                    token_hash,
                    row["version"],
                ),
            )
            if result.rowcount != 1:
                raise ConfirmationBindingError("confirmation binding was superseded")
            bound = connection.execute(
                "SELECT * FROM confirmation_capabilities WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if bound is None:  # pragma: no cover - guarded by the UPDATE above
                raise DatabaseError("confirmation binding disappeared")
            return self._record(bound)

    bind_message = bind

    def consume(
        self,
        token: str | Any,
        *,
        actor_user_id: int,
        actor_chat_id: int,
        tool: str,
        argument_hash: str,
        target_identity: str,
        state_fingerprint: str,
        policy_version: str,
        chat_id: int,
        message_id: int,
        now: float | None = None,
        assertion_nonce: str | None = None,
        assertion_expires_at: int | None = None,
    ) -> Any:
        from .auth import ConfirmationBindingError, ConfirmationReplayError

        actor_user_id = self._positive_id("actor_user_id", actor_user_id)
        actor_chat_id = self._positive_id("actor_chat_id", actor_chat_id)
        tool = self._text("tool", tool)
        argument_hash = self._digest("argument_hash", argument_hash)
        target_identity = self._text("target_identity", target_identity)
        state_fingerprint = self._text("state_fingerprint", state_fingerprint)
        policy_version = self._text("policy_version", policy_version)
        chat_id = self._positive_id("chat_id", chat_id)
        message_id = self._positive_id("message_id", message_id)
        token_hash = self._token_hash(token)
        current = self._now(now, self.clock)
        if self._mark_expired_if_needed(token_hash, current):
            from .auth import ConfirmationExpired

            raise ConfirmationExpired("confirmation token has expired")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM confirmation_capabilities WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                from .auth import ConfirmationError

                raise ConfirmationError("unknown confirmation token")
            if (assertion_nonce is None) != (assertion_expires_at is None):
                raise ValueError(
                    "assertion_nonce and assertion_expires_at must be supplied together"
                )
            if assertion_nonce is not None:
                assert assertion_expires_at is not None
                self._reserve_assertion_nonce(
                    connection,
                    assertion_nonce,
                    assertion_expires_at,
                    current,
                )
            if str(row["state"]) == "consumed":
                raise ConfirmationReplayError("confirmation token was already consumed")
            self._raise_expired(connection, row, current)
            if str(row["state"]) != "armed":
                raise ConfirmationBindingError("confirmation token is not armed")
            if (
                actor_user_id != int(row["actor_user_id"])
                or actor_chat_id != int(row["actor_chat_id"])
                or tool != str(row["tool"])
                or not hmac.compare_digest(argument_hash, str(row["argument_hash"]))
                or target_identity != str(row["target_identity"])
                or state_fingerprint != str(row["state_fingerprint"])
                or policy_version != str(row["policy_version"])
            ):
                raise ConfirmationBindingError("confirmation binding changed")
            if row["bound_chat_id"] is None or chat_id != int(row["bound_chat_id"]):
                raise ConfirmationBindingError("confirmation message chat changed")
            if row["bound_message_id"] is None or message_id != int(
                row["bound_message_id"]
            ):
                raise ConfirmationBindingError("confirmation message changed")
            result = connection.execute(
                """
                UPDATE confirmation_capabilities
                SET state = 'consumed', consumed_at = ?, version = version + 1,
                    updated_at = ?
                WHERE token_hash = ? AND state = 'armed' AND version = ?
                """,
                (current, utc_timestamp(), token_hash, row["version"]),
            )
            if result.rowcount != 1:
                raise ConfirmationReplayError("confirmation token was already consumed")
            consumed = connection.execute(
                "SELECT * FROM confirmation_capabilities WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if consumed is None:  # pragma: no cover - guarded by the UPDATE above
                raise DatabaseError("confirmation consumption disappeared")
            return self._record(consumed)

    consume_token = consume

    def consume_with_assertion_nonce(
        self,
        token: str | Any,
        *,
        actor_user_id: int,
        actor_chat_id: int,
        tool: str,
        argument_hash: str,
        target_identity: str,
        state_fingerprint: str,
        policy_version: str,
        chat_id: int,
        message_id: int,
        assertion_nonce: str,
        assertion_expires_at: int,
        now: float | None = None,
    ) -> Any:
        """Consume a callback assertion nonce and capability in one transaction."""

        return self.consume(
            token,
            actor_user_id=actor_user_id,
            actor_chat_id=actor_chat_id,
            tool=tool,
            argument_hash=argument_hash,
            target_identity=target_identity,
            state_fingerprint=state_fingerprint,
            policy_version=policy_version,
            chat_id=chat_id,
            message_id=message_id,
            assertion_nonce=assertion_nonce,
            assertion_expires_at=assertion_expires_at,
            now=now,
        )

    def get(self, token: str | Any) -> Any:
        token_hash = self._token_hash(token)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM confirmation_capabilities WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        if row is None:
            from .auth import ConfirmationError

            raise ConfirmationError("unknown confirmation token")
        return self._record(row)

    def revoke(self, token: str | Any) -> None:
        token_hash = self._token_hash(token)
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE confirmation_capabilities
                SET state = 'revoked', version = version + 1, updated_at = ?
                WHERE token_hash = ? AND state IN ('pending_bind', 'armed')
                """,
                (utc_timestamp(), token_hash),
            )

    def revoke_policy(self, policy_version: str) -> int:
        policy_version = self._text("policy_version", policy_version)
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT current_version FROM confirmation_policy_state "
                "WHERE policy_name = 'confirmation'"
            ).fetchone()
            if current is not None and str(current[0]) == policy_version:
                self.policy_version = policy_version
            connection.execute(
                "INSERT INTO confirmation_policy_state"
                " (policy_name, current_version, updated_at) VALUES ('confirmation', ?, ?)"
                " ON CONFLICT(policy_name) DO UPDATE SET current_version = excluded.current_version,"
                " updated_at = excluded.updated_at",
                (policy_version, utc_timestamp()),
            )
            result = connection.execute(
                """
                UPDATE confirmation_capabilities
                SET state = 'revoked', version = version + 1, updated_at = ?
                WHERE state IN ('pending_bind', 'armed')
                """,
                (utc_timestamp(),),
            )
        self.policy_version = policy_version
        return result.rowcount

    rotate_policy = revoke_policy

    def cleanup(self, *, now: float | None = None, limit: int = 100) -> int:
        current = self._now(now, self.clock)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("confirmation cleanup limit must be positive")
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT token_hash FROM confirmation_capabilities "
                "WHERE expires_at <= ? OR state IN ('expired', 'revoked', 'consumed') "
                "ORDER BY token_hash LIMIT ?",
                (current, limit),
            ).fetchall()
            if not rows:
                return 0
            placeholders = ", ".join("?" for _ in rows)
            result = connection.execute(
                """
                DELETE FROM confirmation_capabilities WHERE token_hash IN (
                """
                + placeholders
                + ")",
                tuple(str(row[0]) for row in rows),
            )
            return result.rowcount


def open_database(
    path: str | Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    migrate: bool = True,
) -> Database:
    """Construct a database and optionally apply the checked-in migrations."""

    database = Database(path, busy_timeout_ms=busy_timeout_ms)
    if migrate:
        database.migrate()
    return database


def apply_migrations(
    connection: sqlite3.Connection,
    migrations: Iterable[Migration] = MIGRATIONS,
) -> MigrationReport:
    """Apply migrations to an existing sqlite3 connection.

    Most application code should use :class:`Database`, but this adapter is
    useful for tests and callers that own a connection pool.  The supplied
    connection is left open; transaction ownership remains here.
    """

    if connection.in_transaction:
        raise RuntimeError(
            "apply_migrations requires an autocommit connection; it owns each migration transaction"
        )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA synchronous = FULL")
    database_row = connection.execute("PRAGMA database_list").fetchone()
    database_path = "" if database_row is None else str(database_row[2])
    if database_path and database_path != ":memory:":
        journal_mode = str(
            connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        )
        if journal_mode.lower() != "wal":
            raise DatabaseError(
                f"SQLite refused WAL mode for migration adapter (got {journal_mode!r})"
            )
        adapter_path = Path(database_path)
        _secure_directory(adapter_path.parent)
        _secure_file(adapter_path)
        _secure_file(Path(f"{adapter_path}-wal"))
        _secure_file(Path(f"{adapter_path}-shm"))
    migrations_tuple = validate_migrations(migrations)
    Database._ensure_management_tables_on_connection(connection)
    applied: list[MigrationRecord] = []
    while True:
        next_migration: Migration | None = None
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = connection.execute(
                "SELECT version, name, checksum, applied_at, duration_ms "
                "FROM schema_migrations ORDER BY version"
            ).fetchall()
            records = [
                MigrationRecord(
                    int(row[0]),
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    int(row[4]),
                )
                for row in rows
            ]
            Database._validate_applied_history(records)
            Database._validate_user_version(connection, records)
            by_version = {
                migration.version: migration for migration in migrations_tuple
            }
            for record in records:
                migration = by_version.get(record.version)
                if (
                    migration is None
                    or migration.name != record.name
                    or migration.checksum != record.checksum
                ):
                    raise MigrationIntegrityError(
                        f"migration {record.version} does not match its checksum"
                    )
            highest = records[-1].version if records else 0
            next_migration = next(
                (
                    migration
                    for migration in migrations_tuple
                    if migration.version > highest
                ),
                None,
            )
            if next_migration is None:
                connection.execute("COMMIT")
                connection.execute(f"PRAGMA user_version = {highest}")
                break
            started = time.monotonic()
            started_at = utc_timestamp()
            connection.execute(
                """
                INSERT INTO migration_accounting
                    (migration_version, migration_name, source_name, status, started_at)
                VALUES (?, ?, 'schema', 'running', ?)
                ON CONFLICT(migration_version, source_name)
                DO UPDATE SET migration_name = excluded.migration_name,
                              status = excluded.status,
                              started_at = excluded.started_at,
                              completed_at = NULL,
                              source_rows = 0,
                              migrated_rows = 0,
                              skipped_rows = 0,
                              failed_rows = 0,
                              details_json = NULL,
                              error_text = NULL
                """,
                (next_migration.version, next_migration.name, started_at),
            )
            next_migration.run(connection)
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            applied_at = utc_timestamp()
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, started_at, completed_at, "
                "applied_at, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    next_migration.version,
                    next_migration.name,
                    next_migration.checksum,
                    started_at,
                    applied_at,
                    applied_at,
                    duration_ms,
                ),
            )
            connection.execute(
                """
                UPDATE migration_accounting
                SET status = 'completed', completed_at = ?,
                    details_json = ?, error_text = NULL
                WHERE migration_version = ? AND source_name = 'schema'
                """,
                (
                    applied_at,
                    json.dumps({"duration_ms": duration_ms}, sort_keys=True),
                    next_migration.version,
                ),
            )
            connection.execute("COMMIT")
            if database_path and database_path != ":memory:":
                _secure_file(Path(database_path))
                _secure_file(Path(f"{database_path}-wal"))
                _secure_file(Path(f"{database_path}-shm"))
            connection.execute(f"PRAGMA user_version = {next_migration.version}")
            applied.append(
                MigrationRecord(
                    next_migration.version,
                    next_migration.name,
                    next_migration.checksum,
                    applied_at,
                    duration_ms,
                )
            )
        except BaseException as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if next_migration is not None:
                try:
                    Database._record_failed_migration_on_connection(
                        connection, next_migration, exc
                    )
                except sqlite3.Error:
                    pass
            raise
    return MigrationReport(tuple(applied), migrations_tuple[-1].version)


run_migrations = apply_migrations


__all__ = [
    "BackupReport",
    "BackupInventoryRecord",
    "BackupVerificationError",
    "ClaimToken",
    "ClockRollbackError",
    "Database",
    "DatabaseError",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_CLAIM_LEASE_SECONDS",
    "MAX_CLAIM_LEASE_SECONDS",
    "DEFAULT_CONFIRMATION_TTL_SECONDS",
    "MigrationError",
    "MigrationIntegrityError",
    "MigrationOrderError",
    "MigrationRecord",
    "MigrationReport",
    "LeaderLease",
    "RETENTION_DAYS",
    "SQLiteConfirmationTokenStore",
    "SQLiteNonceReplayStore",
    "apply_migrations",
    "open_database",
    "run_migrations",
    "utc_now",
    "utc_timestamp",
]
