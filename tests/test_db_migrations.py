from __future__ import annotations

import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest

from media_companion.db import (
    Database,
    ClockRollbackError,
    MigrationIntegrityError,
    Migration,
    MigrationOrderError,
    SQLiteConfirmationTokenStore,
    SQLiteNonceReplayStore,
    apply_migrations,
)
from media_companion.migrations import MIGRATIONS


class DatabaseMigrationTests(unittest.TestCase):
    def test_migrations_are_ordered_checksummed_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "companion.sqlite3")

            first = database.migrate()
            second = database.migrate()

            self.assertEqual([record.version for record in first.applied], [1, 2, 3, 4])
            self.assertEqual(second.applied, ())
            self.assertEqual(first.current_version, 4)
            self.assertEqual(
                [migration.version for migration in MIGRATIONS], [1, 2, 3, 4]
            )
            for migration in MIGRATIONS:
                self.assertEqual(
                    migration.checksum,
                    hashlib.sha256(migration.sql.encode()).hexdigest(),
                )

            with database.connection() as connection:
                rows = connection.execute(
                    "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
                ).fetchall()
                accounting = connection.execute(
                    """
                    SELECT migration_version, status, source_name
                    FROM migration_accounting
                    WHERE source_name = 'schema'
                    ORDER BY migration_version
                    """
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in rows],
                    [
                        (migration.version, migration.name, migration.checksum)
                        for migration in MIGRATIONS
                    ],
                )
                self.assertEqual(
                    [tuple(row) for row in accounting],
                    [
                        (1, "completed", "schema"),
                        (2, "completed", "schema"),
                        (3, "completed", "schema"),
                        (4, "completed", "schema"),
                    ],
                )

    def test_sqlite_connection_uses_wal_busy_timeout_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(
                Path(directory) / "companion.sqlite3", busy_timeout_ms=2_345
            )
            database.migrate()

            with database.connection() as connection:
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                    "wal",
                )
                self.assertEqual(
                    connection.execute("PRAGMA busy_timeout").fetchone()[0], 2_345
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertTrue(
                {
                    "requests",
                    "request_commands",
                    "subscriptions",
                    "subscription_units",
                    "episode_enumerations",
                    "plex_items",
                    "plex_crosswalks",
                    "activation",
                    "activation_members",
                    "activation_cursors",
                    "event_inbox",
                    "notification_groups",
                    "deliveries",
                    "delivery_memberships",
                    "delivery_chunks",
                    "actor_nonces",
                    "confirmation_capabilities",
                    "idempotency_keys",
                    "leader_leases",
                    "claim_leases",
                    "audit_events",
                    "quarantined_records",
                    "migration_lineage",
                    "migration_expansions",
                }
                <= tables
            )
            self.assertFalse(
                {
                    "users",
                    "identities",
                    "chat_bindings",
                    "pairing_codes",
                    "media_requests",
                    "request_items",
                    "outbox",
                    "notifications",
                    "webhook_events",
                }
                & tables
            )

    def test_migration_history_rejects_tampering_and_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "companion.sqlite3"
            database = Database(path)
            database.migrate()

            with database.connection() as connection:
                connection.execute(
                    "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1"
                )
            with self.assertRaises(MigrationIntegrityError):
                database.migrate()

            path2 = Path(directory) / "gapped.sqlite3"
            database2 = Database(path2)
            database2.migrate()
            with database2.connection() as connection:
                connection.execute("DELETE FROM schema_migrations WHERE version = 1")
            with self.assertRaises(MigrationOrderError):
                database2.migrate()

    def test_transactions_roll_back_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "companion.sqlite3")
            database.migrate()

            with self.assertRaises(RuntimeError):
                with database.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO deliveries
                            (destination, chat_id, notification_class, event_key,
                             idempotency_key)
                        VALUES ('telegram', 123, 'requester', 'test', 'test-rollback')
                        """
                    )
                    raise RuntimeError("rollback")

            with database.connection() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0],
                    0,
                )

    def test_native_backup_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "companion.sqlite3")
            database.migrate()
            with database.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO deliveries
                        (destination, chat_id, notification_class, event_key,
                         idempotency_key)
                    VALUES ('telegram', 123, 'requester', 'test', 'test-backup')
                    """
                )

            report = database.backup(root / "backup.sqlite3")
            self.assertTrue(report.verified)
            self.assertEqual(report.integrity_check.lower(), "ok")
            self.assertEqual(
                report.source_table_counts, report.destination_table_counts
            )
            with sqlite3.connect(root / "backup.sqlite3") as backup:
                self.assertEqual(
                    backup.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 1
                )

    def test_claim_token_and_compare_and_swap_are_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "companion.sqlite3")
            database.migrate()
            with database.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO event_inbox
                        (event_key, source, event_type, payload_hash)
                    VALUES ('test-claim', 'test', 'test', 'hash')
                    """
                )
                row_id = connection.execute("SELECT id FROM event_inbox").fetchone()[0]

            first = database.claim_event(row_id, lease_seconds=30)
            self.assertIsNotNone(first)
            assert first is not None
            self.assertIsNone(database.claim_event(row_id, lease_seconds=30))
            self.assertFalse(
                database.complete_claim("event_inbox", row_id, "wrong-token")
            )
            self.assertTrue(database.complete_claim("event_inbox", row_id, first.token))

            with database.connection() as connection:
                row = connection.execute(
                    "SELECT status, claim_token, version FROM event_inbox WHERE id = ?",
                    (row_id,),
                ).fetchone()
                self.assertEqual(tuple(row), ("handled", None, 2))
                version = int(row[2])
            self.assertTrue(
                database.compare_and_swap(
                    "event_inbox",
                    row_id,
                    expected_version=version,
                    updates={"error_text": "none"},
                )
            )
            self.assertFalse(
                database.compare_and_swap(
                    "event_inbox",
                    row_id,
                    expected_version=version,
                    updates={"error_text": "stale"},
                )
            )

    def test_durable_nonce_and_confirmation_protocols_are_atomic(self) -> None:
        from media_companion.auth import ConfirmationReplayError

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "companion.sqlite3")
            database.migrate()

            nonce_store = database.nonce_replay_store()
            self.assertIsInstance(nonce_store, SQLiteNonceReplayStore)
            self.assertTrue(nonce_store.consume("nonce-1", 200, now=100))
            self.assertFalse(nonce_store.consume("nonce-1", 200, now=100))
            self.assertFalse(nonce_store.consume("expired", 100, now=100))
            with database.connection() as connection:
                nonce_row = connection.execute(
                    "SELECT nonce_hash, expires_at FROM actor_nonces"
                ).fetchone()
                self.assertEqual(
                    tuple(nonce_row), (hashlib.sha256(b"nonce-1").hexdigest(), 200)
                )
                self.assertNotIn("nonce-1", str(tuple(nonce_row)))
            self.assertEqual(nonce_store.cleanup(now=201), 1)

            confirmation_store = database.confirmation_store(
                ttl=300, policy_version="policy-v1"
            )
            self.assertIsInstance(confirmation_store, SQLiteConfirmationTokenStore)
            argument_hash = hashlib.sha256(b"arguments").hexdigest()
            token = confirmation_store.create(
                actor_user_id=7,
                actor_chat_id=8,
                tool="delete_media",
                argument_hash=argument_hash,
                target_identity="tmdb:123",
                state_fingerprint="state-v1",
                preview="Delete tmdb:123?",
                policy_version="policy-v1",
                now=100,
                nonce="confirmation-nonce",
            )
            with database.connection() as connection:
                stored_token = connection.execute(
                    "SELECT token_hash, state FROM confirmation_capabilities"
                ).fetchone()
                self.assertEqual(stored_token[0], token.token_hash)
                self.assertNotIn(str(token), str(tuple(stored_token)))
                self.assertEqual(stored_token[1], "pending_bind")

            bound = confirmation_store.bind(
                token,
                chat_id=8,
                message_id=99,
                preview="Delete tmdb:123?",
                now=101,
            )
            self.assertEqual(bound.state, "armed")
            self.assertEqual(
                confirmation_store.bind(
                    token,
                    chat_id=8,
                    message_id=99,
                    preview="Delete tmdb:123?",
                    now=101,
                ),
                bound,
            )
            consumed = confirmation_store.consume(
                token,
                actor_user_id=7,
                actor_chat_id=8,
                tool="delete_media",
                argument_hash=argument_hash,
                target_identity="tmdb:123",
                state_fingerprint="state-v1",
                policy_version="policy-v1",
                chat_id=8,
                message_id=99,
                now=102,
            )
            self.assertEqual(consumed.state, "consumed")
            with self.assertRaises(ConfirmationReplayError):
                confirmation_store.consume(
                    token,
                    actor_user_id=7,
                    actor_chat_id=8,
                    tool="delete_media",
                    argument_hash=argument_hash,
                    target_identity="tmdb:123",
                    state_fingerprint="state-v1",
                    policy_version="policy-v1",
                    chat_id=8,
                    message_id=99,
                    now=102,
                )

    def test_migration_lineage_records_one_disposition_and_expansions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "companion.sqlite3")
            database.migrate()
            lineage_id = database.record_migration_lineage(
                "dry-run-1",
                "legacy_media_requests",
                42,
                "migrated",
                target_table="subscriptions",
                target_row_id=7,
                expansions=(
                    {
                        "target_table": "subscription_units",
                        "target_row_id": 10,
                        "season_number": 1,
                    },
                    {
                        "target_table": "subscription_units",
                        "target_row_id": 11,
                        "season_number": 2,
                    },
                ),
            )
            self.assertGreater(lineage_id, 0)
            self.assertEqual(
                database.migration_disposition_counts("dry-run-1"), {"migrated": 1}
            )
            with database.connection() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT expansion_count FROM migration_lineage WHERE id = ?",
                        (lineage_id,),
                    ).fetchone()[0],
                    2,
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO migration_lineage("
                        "migration_id, source_table, source_row_id, disposition, source_fingerprint) "
                        "VALUES ('bad', 'legacy', '1', 'quarantined', 'not-a-fingerprint')"
                    )

    def test_synthetic_legacy_shape_is_not_data_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE media_requests (
                        id INTEGER PRIMARY KEY,
                        media_type TEXT NOT NULL,
                        title TEXT,
                        season_numbers TEXT,
                        requested_by_chat_id INTEGER,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    INSERT INTO media_requests
                        (id, media_type, title, season_numbers,
                         requested_by_chat_id, status, created_at, updated_at)
                    VALUES (7, 'series', 'Synthetic', '[1,2]', 123, 'requested',
                            '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
                    """
                )

            database = Database(path)
            database.migrate()
            with database.connection() as connection:
                row = connection.execute(
                    "SELECT id, media_type, title, season_numbers, requested_by_chat_id "
                    "FROM media_requests"
                ).fetchone()
                self.assertEqual(tuple(row), (7, "series", "Synthetic", "[1,2]", 123))
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0
                )

    def test_apply_migrations_is_per_migration_and_callback_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = sqlite3.connect(
                Path(directory) / "adapter.sqlite3", isolation_level=None
            )

            def apply_one(handle: sqlite3.Connection) -> None:
                handle.execute("CREATE TABLE callback_table (id INTEGER PRIMARY KEY)")

            report = apply_migrations(
                connection,
                (Migration(1, "callback", (), apply_one),),
            )
            self.assertEqual(report.applied_versions, (1,))
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM callback_table").fetchone()[0],
                0,
            )

            def commits(handle: sqlite3.Connection) -> None:
                handle.execute("CREATE TABLE callback_commit (id INTEGER PRIMARY KEY)")
                handle.commit()

            with self.assertRaises(RuntimeError):
                bad_connection = sqlite3.connect(
                    Path(directory) / "bad.sqlite3", isolation_level=None
                )
                try:
                    apply_migrations(
                        bad_connection,
                        (Migration(1, "bad_callback", (), apply=commits),),
                    )
                finally:
                    bad_connection.close()

            with sqlite3.connect(Path(directory) / "bad.sqlite3") as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'callback_table'"
                    ).fetchone()
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'callback_commit'"
                    ).fetchone()
                )

    def test_failed_migration_accounting_is_conserved_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failed.sqlite3"

            def fail(handle: sqlite3.Connection) -> None:
                handle.execute("CREATE TABLE partial_failure (id INTEGER)")
                raise RuntimeError("secret token=/tmp/private")

            database = Database(
                path,
                migrations=(
                    Migration(1, "initial", ("CREATE TABLE first (id INTEGER)",)),
                    Migration(2, "failing", (), apply=fail),
                ),
            )
            with self.assertRaises(RuntimeError):
                database.migrate()
            with database.connection() as connection:
                row = connection.execute(
                    "SELECT source_rows, migrated_rows, skipped_rows, failed_rows, error_text "
                    "FROM migration_accounting WHERE migration_version = 2"
                ).fetchone()
                self.assertEqual(tuple(row[:4]), (1, 0, 0, 1))
                self.assertNotIn("secret", str(row[4]))
                self.assertNotIn("/tmp/private", str(row[4]))
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'partial_failure'"
                    ).fetchone()
                )

    def test_canonical_validation_rechecks_every_applied_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "companion.sqlite3")
            database.migrate()
            with database.connection() as connection:
                connection.execute(
                    "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 4"
                )
            with self.assertRaises(MigrationIntegrityError):
                database.validate_canonical_schema()

    def test_generic_claim_epoch_fences_renew_release_and_cas(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "companion.sqlite3")
            database.migrate()
            leader = database.acquire_leader("worker-a", lease_seconds=30, now=base)
            self.assertIsNotNone(leader)
            assert leader is not None
            claim = database.claim_resource(
                "provider", "42", leader_epoch=leader.epoch, now=base
            )
            self.assertIsNotNone(claim)
            assert claim is not None
            self.assertTrue(database.release_leader(leader))
            replacement = database.acquire_leader(
                "worker-b", lease_seconds=30, now=base + timedelta(seconds=1)
            )
            self.assertIsNotNone(replacement)
            self.assertIsNone(
                database.renew_resource(
                    "provider", "42", claim, now=base + timedelta(seconds=2)
                )
            )
            self.assertFalse(
                database.release_resource(
                    "provider", "42", claim, now=base + timedelta(seconds=2)
                )
            )
            self.assertFalse(
                database.compare_and_swap(
                    "claim_leases",
                    "42",
                    claim,
                    {"owner": "stale"},
                    id_column="resource_id",
                    now=base + timedelta(seconds=2),
                )
            )

    def test_legacy_string_claims_are_fenced_by_live_leader_epoch(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "companion.sqlite3")
            database.migrate()
            with database.connection() as connection:
                connection.execute(
                    "INSERT INTO deliveries(destination, chat_id, notification_class, "
                    "event_key, idempotency_key) VALUES ('telegram', 1, 'requester', 'e', 'i')"
                )
                row_id = int(
                    connection.execute("SELECT id FROM deliveries").fetchone()[0]
                )
            claim = database.claim_delivery(row_id, now=base)
            self.assertIsNotNone(claim)
            assert claim is not None
            leader = database.acquire_leader("worker-a", lease_seconds=30, now=base)
            self.assertIsNotNone(leader)
            self.assertIsNone(
                database.renew_claim(
                    "deliveries", row_id, claim.token, now=base + timedelta(seconds=1)
                )
            )
            self.assertFalse(
                database.release_claim(
                    "deliveries", row_id, claim.token, now=base + timedelta(seconds=1)
                )
            )
            self.assertFalse(
                database.complete_claim(
                    "deliveries",
                    row_id,
                    claim.token,
                    status="sent",
                    now=base + timedelta(seconds=1),
                )
            )

    def test_leader_epoch_fences_claims_and_clock_rollbacks(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "companion.sqlite3")
            database.migrate()
            with database.connection() as connection:
                connection.execute(
                    "INSERT INTO deliveries(destination, chat_id, notification_class, "
                    "event_key, idempotency_key) VALUES ('telegram', 1, 'requester', 'e', 'i')"
                )
                row_id = int(
                    connection.execute("SELECT id FROM deliveries").fetchone()[0]
                )
            leader = database.acquire_leader("worker-a", lease_seconds=30, now=base)
            self.assertIsNotNone(leader)
            assert leader is not None
            self.assertIsNone(
                database.claim_delivery(row_id, leader_epoch=leader.epoch + 1, now=base)
            )
            claim = database.claim_delivery(row_id, leader_epoch=leader.epoch, now=base)
            self.assertIsNotNone(claim)
            assert claim is not None
            renewed = database.renew_claim(
                "deliveries",
                row_id,
                claim,
                lease_seconds=30,
                now=base + timedelta(seconds=1),
            )
            self.assertIsNotNone(renewed)
            self.assertFalse(
                database.complete_claim(
                    "deliveries", row_id, claim, status="sent", now=base
                )
            )
            self.assertTrue(database.release_leader(leader))
            replacement = database.acquire_leader(
                "worker-b", lease_seconds=30, now=base + timedelta(seconds=2)
            )
            self.assertIsNotNone(replacement)
            assert replacement is not None
            self.assertGreater(replacement.epoch, leader.epoch)
            self.assertFalse(
                database.complete_claim(
                    "deliveries",
                    row_id,
                    renewed,
                    status="sent",
                    now=base + timedelta(seconds=2),
                )
            )

            database.observe_clock(base + timedelta(seconds=3))
            with self.assertRaises(ClockRollbackError):
                database.observe_clock(base - timedelta(seconds=31))
            with database.connection() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT blocked FROM clock_state WHERE clock_name = 'database'"
                    ).fetchone()[0],
                    1,
                )
            database.clear_clock_rollback(base + timedelta(seconds=4))

    def test_null_safe_constraints_and_status_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "companion.sqlite3")
            database.migrate()
            with database.connection() as connection:
                values = (
                    1,
                    2,
                    "telegram",
                    "requester",
                    "movie",
                    "100",
                    "movie",
                )
                connection.execute(
                    "INSERT INTO subscriptions(user_id, chat_id, destination, notification_class, "
                    "media_type, provider_id, mode) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO subscriptions(user_id, chat_id, destination, notification_class, "
                        "media_type, provider_id, mode) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        values,
                    )
                connection.execute(
                    "INSERT INTO notification_groups(group_key, destination, chat_id, "
                    "notification_class, canonical_show_identity) VALUES ('g1', 'telegram', 2, 'requester', NULL)"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO notification_groups(group_key, destination, chat_id, "
                        "notification_class, canonical_show_identity) VALUES ('g2', 'telegram', 2, 'requester', NULL)"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO delivery_chunks(delivery_id, ordinal, chunk_count, stable_key, payload_json) "
                        "VALUES (999, 2, 1, 'bad', '{}')"
                    )
                connection.execute(
                    "INSERT INTO requests(request_key, media_type, provider_id, title, mode) "
                    "VALUES ('r', 'movie', '1', 'Movie', 'movie')"
                )
                request_id = int(
                    connection.execute("SELECT id FROM requests").fetchone()[0]
                )
                connection.execute(
                    "UPDATE requests SET status = 'accepted' WHERE id = ?",
                    (request_id,),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE requests SET status = 'visible_in_plex' WHERE id = ?",
                        (request_id,),
                    )

    def test_confirmation_nonce_is_atomic_and_policy_rotation_persists(self) -> None:
        from media_companion.auth import (
            ConfirmationBindingError,
            ConfirmationReplayError,
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "companion.sqlite3")
            database.migrate()
            store = database.confirmation_store(policy_version="v1")
            argument_hash = hashlib.sha256(b"arguments").hexdigest()
            token = store.create(
                actor_user_id=7,
                actor_chat_id=8,
                tool="delete_media",
                argument_hash=argument_hash,
                target_identity="tmdb:123",
                state_fingerprint="state-v1",
                preview="Delete tmdb:123?",
                policy_version="v1",
                now=100,
            )
            store.bind(
                token, chat_id=8, message_id=99, preview="Delete tmdb:123?", now=101
            )
            with self.assertRaises(ConfirmationBindingError):
                store.consume(
                    token,
                    actor_user_id=7,
                    actor_chat_id=8,
                    tool="delete_media",
                    argument_hash=argument_hash,
                    target_identity="changed",
                    state_fingerprint="state-v1",
                    policy_version="v1",
                    chat_id=8,
                    message_id=99,
                    assertion_nonce="callback-nonce",
                    assertion_expires_at=200,
                    now=102,
                )
            # The failed capability check rolled the nonce reservation back.
            consumed = store.consume(
                token,
                actor_user_id=7,
                actor_chat_id=8,
                tool="delete_media",
                argument_hash=argument_hash,
                target_identity="tmdb:123",
                state_fingerprint="state-v1",
                policy_version="v1",
                chat_id=8,
                message_id=99,
                assertion_nonce="callback-nonce",
                assertion_expires_at=200,
                now=102,
            )
            self.assertEqual(consumed.state, "consumed")
            with self.assertRaises(ConfirmationReplayError):
                store.consume(
                    token,
                    actor_user_id=7,
                    actor_chat_id=8,
                    tool="delete_media",
                    argument_hash=argument_hash,
                    target_identity="tmdb:123",
                    state_fingerprint="state-v1",
                    policy_version="v1",
                    chat_id=8,
                    message_id=99,
                    assertion_nonce="callback-nonce",
                    assertion_expires_at=200,
                    now=102,
                )
            store.revoke_policy("v2")
            restarted = database.confirmation_store(policy_version="v1")
            self.assertEqual(restarted.policy_version, "v2")

    def test_scrub_backup_inventory_and_cutover_contract(self) -> None:
        from stat import S_IMODE

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "companion.sqlite3")
            database.migrate()
            report = database.backup(root / "rollback.sqlite3", rollback_snapshot=True)
            self.assertTrue(report.verified)
            self.assertEqual(S_IMODE((root / "rollback.sqlite3").stat().st_mode), 0o600)
            contract = database.legacy_cutover_contract()
            self.assertEqual(contract["target_schema_version"], 4)
            self.assertTrue(contract["backup_required"])
            old = "2020-01-01T00:00:00Z"
            with database.connection() as connection:
                connection.execute(
                    "INSERT INTO requests(request_key, media_type, provider_id, title, mode, status, "
                    "requested_by_user_id, requested_by_chat_id, requested_by_username, payload_json, updated_at) "
                    "VALUES ('old', 'movie', '1', 'Old', 'movie', 'fulfilled', 7, 8, 'name', 'raw', ?)",
                    (old,),
                )
                connection.execute(
                    "INSERT INTO deliveries(destination, chat_id, notification_class, event_key, "
                    "idempotency_key, status, error_text, updated_at) VALUES ('telegram', 8, 'requester', "
                    "'old-event', 'old-idem', 'sent', 'raw error', ?)",
                    (old,),
                )
            counts = database.scrub_terminal_personal_data(
                now=datetime(2026, 8, 15, tzinfo=timezone.utc), limit=10
            )
            self.assertGreaterEqual(counts["requests"], 1)
            with database.connection() as connection:
                self.assertEqual(
                    tuple(
                        connection.execute(
                            "SELECT requested_by_user_id, requested_by_chat_id, payload_json FROM requests "
                            "WHERE request_key = 'old'"
                        ).fetchone()
                    ),
                    (None, None, None),
                )
                self.assertEqual(
                    tuple(
                        connection.execute(
                            "SELECT chat_id, error_text FROM deliveries WHERE idempotency_key = 'old-idem'"
                        ).fetchone()
                    ),
                    (0, None),
                )
                connection.execute(
                    "INSERT INTO actor_nonces(nonce_hash, consumed_at, expires_at) "
                    "VALUES (?, 0, 0)",
                    (hashlib.sha256(b"expired").hexdigest(),),
                )
                connection.execute(
                    "INSERT INTO request_candidates("
                    "handle_hash, actor_user_id, actor_chat_id, media_type, provider_id, "
                    "title, query_hash, payload_json, issued_at, expires_at) "
                    "VALUES ('candidate-hash', 7, 8, 'movie', '1', 'Old', 'query-hash', '{}', ?, ?)",
                    (old, old),
                )
            bounded = database.scrub_terminal_personal_data(
                now=datetime(2026, 8, 15, tzinfo=timezone.utc), limit=1
            )
            self.assertEqual(bounded["actor_nonces"], 1)
