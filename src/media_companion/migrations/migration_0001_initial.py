"""Initial canonical Media Companion ledger schema.

The companion database is not an identity or access-policy registry.  Telegram
numeric user/chat IDs are stored directly on the request/subscription/audit
records that need them; allowlist membership remains Hermes-owned.  This
migration creates only durable ledger primitives and never reads the legacy
``media_requests`` table.
"""

from __future__ import annotations

from . import Migration


STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        checksum TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
        rollback_compatible INTEGER NOT NULL DEFAULT 1 CHECK (rollback_compatible IN (0, 1))
    )
    """,
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
    """,
    """
    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_key TEXT NOT NULL UNIQUE,
        requested_by_user_id INTEGER,
        requested_by_chat_id INTEGER,
        requested_by_username TEXT,
        actor_update_id INTEGER,
        media_type TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        tmdb_id INTEGER,
        tvdb_id INTEGER,
        imdb_id TEXT,
        external_provider TEXT,
        external_id TEXT,
        title TEXT NOT NULL,
        year INTEGER,
        seasons_json TEXT,
        mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'requested',
        provider_item_id TEXT,
        arr_id INTEGER,
        plex_baseline_json TEXT,
        idempotency_key TEXT UNIQUE,
        payload_json TEXT,
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS request_commands (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
        command_type TEXT NOT NULL,
        service TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        provider_id TEXT,
        season_number INTEGER,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        available_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        claim_token TEXT UNIQUE,
        claim_expires_at TEXT,
        claim_version INTEGER,
        claim_epoch INTEGER,
        claim_worker TEXT,
        claimed_at TEXT,
        external_id TEXT,
        last_error TEXT,
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER REFERENCES requests(id) ON DELETE SET NULL,
        user_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        destination TEXT NOT NULL DEFAULT 'telegram',
        notification_class TEXT NOT NULL DEFAULT 'requester',
        media_type TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        tmdb_id INTEGER,
        tvdb_id INTEGER,
        imdb_id TEXT,
        season_number INTEGER,
        seasons_json TEXT,
        mode TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0),
        baseline INTEGER NOT NULL DEFAULT 0 CHECK (baseline IN (0, 1)),
        status TEXT NOT NULL DEFAULT 'active',
        activated_at TEXT,
        disabled_at TEXT,
        fulfilled_at TEXT,
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(user_id, chat_id, provider_id, media_type, season_number, generation)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subscription_units (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        logical_unit_key TEXT NOT NULL,
        unit_type TEXT NOT NULL,
        provider_id TEXT,
        season_number INTEGER,
        episode_number INTEGER,
        expected INTEGER NOT NULL DEFAULT 1 CHECK (expected IN (0, 1)),
        status TEXT NOT NULL DEFAULT 'tracking',
        visible_in_plex_at TEXT,
        delivered_at TEXT,
        plex_item_id INTEGER,
        enumeration_version INTEGER,
        metadata_json TEXT,
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(subscription_id, logical_unit_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS episode_enumerations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        season_number INTEGER NOT NULL,
        version INTEGER NOT NULL,
        episodes_json TEXT NOT NULL,
        expected_count INTEGER,
        authoritative INTEGER NOT NULL DEFAULT 0 CHECK (authoritative IN (0, 1)),
        evidence_json TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(provider, provider_id, season_number, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plex_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        server_uuid TEXT NOT NULL,
        library_uuid TEXT NOT NULL,
        machine_identifier TEXT,
        rating_key TEXT NOT NULL,
        tombstone_generation INTEGER NOT NULL DEFAULT 0 CHECK (tombstone_generation >= 0),
        media_type TEXT NOT NULL,
        title TEXT NOT NULL,
        year INTEGER,
        show_title TEXT,
        season_number INTEGER,
        episode_number INTEGER,
        library_key TEXT,
        library_name TEXT,
        tmdb_id INTEGER,
        tvdb_id INTEGER,
        imdb_id TEXT,
        provider_guid_json TEXT,
        quality TEXT,
        plex_url TEXT,
        poster_hash TEXT,
        added_at TEXT,
        visible_in_plex_at TEXT,
        fingerprint TEXT,
        lifecycle_status TEXT NOT NULL DEFAULT 'active',
        payload_json TEXT,
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(server_uuid, library_uuid, rating_key, tombstone_generation)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plex_crosswalks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plex_item_id INTEGER NOT NULL REFERENCES plex_items(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        verified INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
        evidence_json TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(plex_item_id, provider, provider_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activation (
        activation_id TEXT PRIMARY KEY,
        baseline_started_at TEXT,
        baseline_completed_at TEXT,
        activated_at TEXT,
        baseline_membership_json TEXT,
        allowed_server_ids_json TEXT NOT NULL DEFAULT '[]',
        allowed_library_ids_json TEXT NOT NULL DEFAULT '[]',
        delivery_enabled INTEGER NOT NULL DEFAULT 0 CHECK (delivery_enabled IN (0, 1)),
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'baseline', 'active', 'blocked', 'complete')),
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activation_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        activation_id TEXT NOT NULL REFERENCES activation(activation_id) ON DELETE CASCADE,
        logical_key TEXT NOT NULL,
        server_uuid TEXT NOT NULL,
        library_uuid TEXT NOT NULL,
        rating_key TEXT NOT NULL,
        tombstone_generation INTEGER NOT NULL DEFAULT 0,
        pass_number INTEGER NOT NULL CHECK (pass_number IN (1, 2)),
        added_at TEXT,
        classification TEXT NOT NULL DEFAULT 'historical',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(activation_id, logical_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activation_cursors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        activation_id TEXT NOT NULL REFERENCES activation(activation_id) ON DELETE CASCADE,
        server_uuid TEXT NOT NULL,
        library_uuid TEXT NOT NULL,
        added_at_cursor TEXT,
        rating_key_cursor TEXT,
        scan_generation INTEGER NOT NULL DEFAULT 0,
        last_incremental_at TEXT,
        last_full_sweep_at TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(activation_id, server_uuid, library_uuid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_inbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL,
        event_type TEXT NOT NULL,
        server_uuid TEXT,
        library_uuid TEXT,
        rating_key TEXT,
        tombstone_generation INTEGER,
        payload_hash TEXT NOT NULL,
        sanitized_payload_json TEXT,
        status TEXT NOT NULL DEFAULT 'received',
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        available_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        claim_token TEXT UNIQUE,
        claim_expires_at TEXT,
        claim_version INTEGER,
        claim_epoch INTEGER,
        claim_worker TEXT,
        claimed_at TEXT,
        error_class TEXT,
        error_text TEXT,
        received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        processed_at TEXT,
        version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
)


MIGRATION = Migration(version=1, name="initial_ledger", statements=STATEMENTS)

__all__ = ["MIGRATION", "STATEMENTS"]
