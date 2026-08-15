"""Delivery, authorization-lifecycle, worker, and accounting tables."""

from __future__ import annotations

from . import Migration


STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS notification_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_key TEXT NOT NULL UNIQUE,
        destination TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        notification_class TEXT NOT NULL,
        canonical_show_identity TEXT,
        season_number INTEGER,
        window_generation INTEGER NOT NULL DEFAULT 1,
        first_seen_at TEXT,
        due_at TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        payload_json TEXT,
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER REFERENCES notification_groups(id) ON DELETE SET NULL,
        destination TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        notification_class TEXT NOT NULL,
        event_key TEXT NOT NULL,
        subscription_generation INTEGER,
        idempotency_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        claim_token TEXT UNIQUE,
        claim_expires_at TEXT,
        claim_version INTEGER,
        claim_epoch INTEGER,
        claim_worker TEXT,
        claimed_at TEXT,
        possible_duplicate INTEGER NOT NULL DEFAULT 0 CHECK (possible_duplicate IN (0, 1)),
        resend_generation INTEGER NOT NULL DEFAULT 0 CHECK (resend_generation >= 0),
        recovery_of INTEGER REFERENCES deliveries(id) ON DELETE SET NULL,
        telegram_message_id INTEGER,
        error_class TEXT,
        error_text TEXT,
        last_retry_at TEXT,
        sent_at TEXT,
        abandoned_at TEXT,
        sending_started_at TEXT,
        send_deadline_at TEXT,
        retry_due_at TEXT,
        terminal_at TEXT,
        unknown_at TEXT,
        unknown_reason TEXT,
        last_error_class TEXT,
        alert_count INTEGER NOT NULL DEFAULT 0 CHECK (alert_count >= 0),
        last_alert_at TEXT,
        recovery_generation INTEGER NOT NULL DEFAULT 0 CHECK (recovery_generation >= 0),
        recovery_attempted INTEGER NOT NULL DEFAULT 0 CHECK (recovery_attempted IN (0, 1)),
        unknown_resolved INTEGER NOT NULL DEFAULT 0 CHECK (unknown_resolved IN (0, 1)),
        parent_delivery_id INTEGER REFERENCES deliveries(id) ON DELETE SET NULL,
        chunk_ordinal INTEGER NOT NULL DEFAULT 1 CHECK (chunk_ordinal > 0),
        chunk_count INTEGER NOT NULL DEFAULT 1 CHECK (chunk_count > 0),
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS delivery_memberships (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        delivery_id INTEGER NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
        subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        subscription_generation INTEGER NOT NULL,
        unit_id INTEGER REFERENCES subscription_units(id) ON DELETE SET NULL,
        eligibility TEXT NOT NULL DEFAULT 'eligible',
        status TEXT NOT NULL DEFAULT 'pending',
        outcome TEXT,
        fulfilled_at TEXT,
        disabled_at TEXT,
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(delivery_id, subscription_id, subscription_generation, unit_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS delivery_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        delivery_id INTEGER NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
        chunk_count INTEGER NOT NULL CHECK (chunk_count > 0),
        stable_key TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        claim_token TEXT UNIQUE,
        claim_expires_at TEXT,
        claim_version INTEGER,
        claim_epoch INTEGER,
        claim_worker TEXT,
        claimed_at TEXT,
        possible_duplicate INTEGER NOT NULL DEFAULT 0 CHECK (possible_duplicate IN (0, 1)),
        telegram_message_id INTEGER,
        error_class TEXT,
        error_text TEXT,
        sent_at TEXT,
        sending_started_at TEXT,
        send_deadline_at TEXT,
        unknown_at TEXT,
        unknown_reason TEXT,
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(delivery_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS actor_nonces (
        nonce_hash TEXT PRIMARY KEY,
        actor_hash TEXT,
        update_id INTEGER,
        tool_name TEXT,
        consumed_at INTEGER,
        expires_at INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS confirmation_capabilities (
        token_hash TEXT PRIMARY KEY,
        actor_user_id INTEGER NOT NULL,
        actor_chat_id INTEGER NOT NULL,
        tool TEXT NOT NULL,
        argument_hash TEXT NOT NULL,
        target_identity TEXT NOT NULL,
        state_fingerprint TEXT NOT NULL,
        preview_hash TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        nonce TEXT NOT NULL,
        issued_at INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending_bind'
            CHECK (state IN ('pending_bind', 'armed', 'consumed', 'expired', 'revoked')),
        expires_at INTEGER NOT NULL,
        bound_chat_id INTEGER,
        bound_message_id INTEGER,
        bound_at TEXT,
        consumed_at INTEGER,
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        scope TEXT NOT NULL,
        key TEXT NOT NULL,
        response_hash TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        expires_at TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        PRIMARY KEY(scope, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS leader_leases (
        lease_name TEXT PRIMARY KEY,
        epoch INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),
        owner TEXT,
        claim_token TEXT UNIQUE,
        claimed_at TEXT,
        expires_at TEXT,
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS claim_leases (
        resource_type TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        claim_token TEXT NOT NULL UNIQUE,
        owner TEXT,
        claimed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        expires_at TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        PRIMARY KEY(resource_type, resource_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_user_id INTEGER,
        actor_chat_id INTEGER,
        actor_type TEXT,
        action TEXT NOT NULL,
        outcome TEXT NOT NULL,
        resource_type TEXT,
        resource_id TEXT,
        request_id INTEGER REFERENCES requests(id) ON DELETE SET NULL,
        metadata_json TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quarantined_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        source_name TEXT,
        source_table TEXT,
        source_id TEXT,
        source_row_id TEXT,
        record_type TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        reason TEXT,
        disposition TEXT NOT NULL DEFAULT 'quarantined',
        detail_json TEXT,
        payload_json TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        resolved_at TEXT,
        resolved_by TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS migration_lineage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        migration_id TEXT NOT NULL,
        source_table TEXT NOT NULL,
        source_row_id TEXT NOT NULL,
        disposition TEXT NOT NULL
            CHECK (disposition IN ('migrated', 'equivalently_merged', 'terminally_archived',
                                   'deleted_after_approval', 'quarantined')),
        reason_code TEXT,
        target_table TEXT,
        target_row_id TEXT,
        expansion_count INTEGER NOT NULL DEFAULT 0 CHECK (expansion_count >= 0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(migration_id, source_table, source_row_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS migration_expansions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lineage_id INTEGER NOT NULL REFERENCES migration_lineage(id) ON DELETE CASCADE,
        target_table TEXT NOT NULL,
        target_row_id TEXT,
        season_number INTEGER,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
)


MIGRATION = Migration(
    version=2, name="delivery_and_operation_ledger", statements=STATEMENTS
)

__all__ = ["MIGRATION", "STATEMENTS"]
