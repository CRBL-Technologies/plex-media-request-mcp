"""Versioned SQLite schema migrations for :mod:`media_companion`.

Migrations are deliberately represented as data (an ordered list of SQL
statements) instead of being discovered from the current database schema.  It
keeps upgrades deterministic and gives the database layer a stable payload to
hash before it is applied.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
import marshal
from typing import Callable, Iterable

import sqlite3


ApplyMigration = Callable[[sqlite3.Connection], None]


def _callback_digest(callback: ApplyMigration) -> str:
    """Hash the executable payload of a migration callback."""

    code = getattr(callback, "__code__", None)
    if code is not None:
        payload = marshal.dumps(code)
    else:
        try:
            payload = inspect.getsource(callback).encode("utf-8", "strict")
        except (OSError, TypeError, UnicodeError) as exc:
            raise ValueError(
                "migration callbacks must expose inspectable executable code"
            ) from exc
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable, ordered schema migration.

    ``statements`` should contain complete SQLite statements.  A callable
    ``apply`` is supported for migrations that need a small amount of Python
    orchestration, while the default remains a straightforward statement
    runner.  The checksum covers the canonical SQL payload and, for callback
    migrations, the callback's executable-code digest.  Version and name are
    recorded separately in the migration ledger.
    """

    version: int
    name: str
    statements: tuple[str, ...]
    apply: ApplyMigration | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise ValueError("migration versions must be positive")
        if (
            not isinstance(self.name, str)
            or not self.name
            or self.name.strip() != self.name
        ):
            raise ValueError("migration names must be non-empty and trimmed")
        if self.apply is not None and not callable(self.apply):
            raise ValueError("migration apply must be callable")
        if not self.statements and self.apply is None:
            raise ValueError("migration must contain SQL or an apply callback")

    @property
    def sql(self) -> str:
        """Return the canonical SQL payload used for accounting/checksums."""

        return "\n\n".join(
            _normalize_statement(statement) for statement in self.statements
        )

    @property
    def id(self) -> int:
        """Compatibility alias for callers that call versions migration IDs."""

        return self.version

    @property
    def checksum(self) -> str:
        """Return the SHA-256 checksum of this migration's immutable payload."""

        payload = self.sql.encode("utf-8")
        if self.apply is not None:
            payload += b"\n\n-- migration-callback-sha256:"
            payload += _callback_digest(self.apply).encode("ascii")
        return hashlib.sha256(payload).hexdigest()

    def run(self, connection: sqlite3.Connection) -> None:
        """Apply this migration to ``connection``.

        Statements are executed individually so the caller's transaction is
        preserved.  ``executescript`` implicitly commits in sqlite3, which
        would make a partially applied migration possible after a failure.
        """

        if self.apply is not None:
            # A callback must not take ownership of the runner's transaction.
            before = connection.in_transaction
            transaction_control: list[str] = []

            def trace(statement: str) -> None:
                command = (
                    statement.strip().split(None, 1)[0].upper()
                    if statement.strip()
                    else ""
                )
                if command in {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"}:
                    transaction_control.append(command)

            def authorize(
                action: int,
                _arg1: str | None,
                _arg2: str | None,
                _database: str | None,
                _source: str | None,
            ) -> int:
                if action in {
                    sqlite3.SQLITE_TRANSACTION,
                    sqlite3.SQLITE_SAVEPOINT,
                }:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_trace_callback(trace)
            connection.set_authorizer(authorize)
            callback_error: BaseException | None = None
            try:
                self.apply(connection)
            except BaseException as exc:
                callback_error = exc
            finally:
                connection.set_trace_callback(None)
                connection.set_authorizer(None)
            if callback_error is not None:
                if transaction_control or (
                    isinstance(callback_error, sqlite3.DatabaseError)
                    and "not authorized" in str(callback_error).lower()
                ):
                    raise RuntimeError(
                        "migration callback attempted to control the runner transaction"
                    ) from callback_error
                raise callback_error
            if (
                not before
                or not connection.in_transaction
                or any(
                    command in {"COMMIT", "ROLLBACK"} for command in transaction_control
                )
            ):
                raise RuntimeError(
                    "migration callback must preserve the caller transaction"
                )
            return
        for statement in self.statements:
            connection.execute(statement)

    up = run


def _normalize_statement(statement: str) -> str:
    if not isinstance(statement, str):
        raise ValueError("migration statements must be strings")
    normalized = statement.strip()
    if not normalized:
        raise ValueError("migration statements cannot be blank")
    return normalized


def validate_migrations(migrations: Iterable[Migration]) -> tuple[Migration, ...]:
    """Validate migrations in the exact order supplied by the registry.

    Validation is intentionally strict: duplicate versions/names and gaps in
    the sequence are all deployment mistakes, not runtime migration choices.
    """

    ordered = tuple(migrations)
    if not ordered:
        raise ValueError("at least one migration is required")

    versions = [migration.version for migration in ordered]
    if versions != sorted(versions):
        raise ValueError("migrations must be supplied in numeric order")
    if len(set(versions)) != len(versions):
        raise ValueError("migration versions must be unique")
    names = [migration.name for migration in ordered]
    if len(set(names)) != len(names):
        raise ValueError("migration names must be unique")
    expected = list(range(1, len(ordered) + 1))
    if versions != expected:
        raise ValueError(
            f"migration versions must be contiguous starting at 1 (got {versions!r})"
        )
    return ordered


# Keep imports at the bottom: migration modules are data-only and this avoids
# exposing a partially initialized ``Migration`` class during import.
from .migration_0001_initial import MIGRATION as MIGRATION_0001  # noqa: E402
from .migration_0002_operational import MIGRATION as MIGRATION_0002  # noqa: E402
from .migration_0003_ledger import MIGRATION as MIGRATION_0003  # noqa: E402
from .migration_0004_hardening import MIGRATION as MIGRATION_0004  # noqa: E402

MIGRATIONS: tuple[Migration, ...] = validate_migrations(
    (MIGRATION_0001, MIGRATION_0002, MIGRATION_0003, MIGRATION_0004)
)


def all_migrations() -> tuple[Migration, ...]:
    """Return the checked-in migration registry in application order."""

    return MIGRATIONS


get_migrations = all_migrations

__all__ = [
    "ApplyMigration",
    "Migration",
    "MIGRATIONS",
    "MIGRATION_0001",
    "MIGRATION_0002",
    "MIGRATION_0003",
    "MIGRATION_0004",
    "all_migrations",
    "get_migrations",
    "validate_migrations",
]
