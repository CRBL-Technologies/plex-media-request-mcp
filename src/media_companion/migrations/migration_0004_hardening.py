"""Integrity, fencing, retention, and operational hardening for the ledger.

This migration is additive.  The first three migrations are already part of
the deployed ledger checksum, so new constraints and operational metadata are
introduced here instead of rewriting those payloads.
"""

from __future__ import annotations

from . import Migration


STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS request_candidates (
        handle_hash TEXT PRIMARY KEY,
        actor_user_id INTEGER NOT NULL,
        actor_chat_id INTEGER NOT NULL,
        actor_update_id INTEGER,
        media_type TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        title TEXT NOT NULL,
        year INTEGER,
        query_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_operations (
        operation_key TEXT PRIMARY KEY,
        service TEXT NOT NULL,
        provider TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        season_number INTEGER,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'succeeded', 'unknown')),
        external_id TEXT,
        owner_request_id INTEGER REFERENCES requests(id) ON DELETE SET NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_request_candidates_expiry
        ON request_candidates(expires_at, actor_user_id, actor_chat_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_provider_operations_status
        ON provider_operations(status, expires_at, service, provider, provider_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_requests_actor_update
        ON requests(requested_by_user_id, requested_by_chat_id, actor_update_id)
        WHERE actor_update_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_requests_actor_update
        ON requests(requested_by_user_id, requested_by_chat_id, actor_update_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS clock_state (
        clock_name TEXT PRIMARY KEY,
        last_seen_epoch_ms INTEGER,
        tolerance_ms INTEGER NOT NULL DEFAULT 30000 CHECK (tolerance_ms >= 0),
        blocked INTEGER NOT NULL DEFAULT 0 CHECK (blocked IN (0, 1)),
        blocked_at TEXT,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL UNIQUE,
        sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
        source_database TEXT NOT NULL,
        verified INTEGER NOT NULL CHECK (verified IN (0, 1)),
        rollback_snapshot INTEGER NOT NULL DEFAULT 0 CHECK (rollback_snapshot IN (0, 1)),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        expired_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS retention_dedupe (
        dedupe_key TEXT PRIMARY KEY,
        record_type TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        source_hash TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS confirmation_policy_state (
        policy_name TEXT PRIMARY KEY,
        current_version TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS legacy_source_mappings (
        source_name TEXT NOT NULL,
        source_table TEXT NOT NULL,
        source_row_id TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL,
        disposition TEXT NOT NULL CHECK (disposition IN
            ('migrated', 'equivalently_merged', 'terminally_archived', 'delete_candidate',
             'deleted_after_approval', 'quarantined')),
        reason TEXT NOT NULL,
        target_request_id INTEGER,
        derived_item_count INTEGER NOT NULL DEFAULT 0 CHECK (derived_item_count >= 0),
        details_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(source_name, source_table, source_row_id)
    )
    """,
    "ALTER TABLE claim_leases ADD COLUMN claim_epoch INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE deliveries ADD COLUMN obligation_key TEXT",
    "ALTER TABLE migration_lineage ADD COLUMN source_fingerprint TEXT NOT NULL DEFAULT ''",
    """
    CREATE TRIGGER IF NOT EXISTS migration_lineage_fingerprint_insert_guard
    BEFORE INSERT ON migration_lineage
    WHEN length(NEW.source_fingerprint) <> 64
         OR NEW.source_fingerprint GLOB '*[^0-9a-f]*'
    BEGIN
        SELECT RAISE(ABORT, 'migration lineage fingerprint must be SHA-256');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS legacy_mapping_fingerprint_insert_guard
    BEFORE INSERT ON legacy_source_mappings
    WHEN length(NEW.source_fingerprint) <> 64
         OR NEW.source_fingerprint GLOB '*[^0-9a-f]*'
    BEGIN
        SELECT RAISE(ABORT, 'legacy source fingerprint must be SHA-256');
    END
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_movie_generation
        ON subscriptions(user_id, chat_id, destination, notification_class,
                         provider_id, media_type, generation)
    WHERE season_number IS NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_season_generation
        ON subscriptions(user_id, chat_id, destination, notification_class,
                         provider_id, media_type, season_number, generation)
        WHERE season_number IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_memberships_without_unit
        ON delivery_memberships(delivery_id, subscription_id, subscription_generation)
        WHERE unit_id IS NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_groups_open_generation
        ON notification_groups(
            destination, chat_id, notification_class,
            COALESCE(canonical_show_identity, ''), COALESCE(season_number, -1)
        )
        WHERE status IN ('open', 'ready')
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_deliveries_obligation_key
        ON deliveries(obligation_key)
        WHERE obligation_key IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_chunks_stable_key
        ON delivery_chunks(stable_key)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_migration_expansion_identity
        ON migration_expansions(
            lineage_id, target_table, COALESCE(target_row_id, ''),
            COALESCE(season_number, -1)
        )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_deliveries_ready_retry
        ON deliveries(status, retry_due_at, claim_expires_at, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_delivery_chunks_ready_retry
        ON delivery_chunks(status, claim_expires_at, delivery_id, ordinal)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_plex_items_tmdb
        ON plex_items(tmdb_id, media_type)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_plex_items_tvdb
        ON plex_items(tvdb_id, media_type)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_plex_items_imdb
        ON plex_items(imdb_id, media_type)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audit_request
        ON audit_events(request_id, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_backup_inventory_expiry
        ON backup_inventory(expires_at, expired_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_legacy_source_mappings_disposition
        ON legacy_source_mappings(source_name, disposition, updated_at)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_inbox_status_insert_guard
    BEFORE INSERT ON event_inbox
    WHEN NEW.status NOT IN
        ('received', 'queued', 'observed', 'ready', 'claimed', 'processing', 'retry_wait',
         'handled', 'ignored', 'failed', 'quarantined', 'blocked')
    BEGIN
        SELECT RAISE(ABORT, 'invalid event_inbox status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_inbox_status_update_guard
    BEFORE UPDATE OF status ON event_inbox
    WHEN NEW.status NOT IN
        ('received', 'queued', 'observed', 'ready', 'claimed', 'processing', 'retry_wait',
         'handled', 'ignored', 'failed', 'quarantined', 'blocked')
    BEGIN
        SELECT RAISE(ABORT, 'invalid event_inbox status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS delivery_status_insert_guard
    BEFORE INSERT ON deliveries
    WHEN NEW.status NOT IN
        ('pending', 'ready', 'claimed', 'sending', 'retry_wait', 'sent',
         'assumed_sent', 'failed', 'unknown', 'delivery_blocked', 'canceled',
         'cancelled', 'superseded', 'abandoned')
    BEGIN
        SELECT RAISE(ABORT, 'invalid delivery status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS delivery_status_update_guard
    BEFORE UPDATE OF status ON deliveries
    WHEN NEW.status NOT IN
        ('pending', 'ready', 'claimed', 'sending', 'retry_wait', 'sent',
         'assumed_sent', 'failed', 'unknown', 'delivery_blocked', 'canceled',
         'cancelled', 'superseded', 'abandoned')
    BEGIN
        SELECT RAISE(ABORT, 'invalid delivery status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS delivery_chunk_status_insert_guard
    BEFORE INSERT ON delivery_chunks
    WHEN NEW.status NOT IN
        ('pending', 'ready', 'claimed', 'sending', 'sent', 'retry_wait',
         'assumed_sent', 'failed', 'unknown', 'canceled', 'cancelled',
         'superseded', 'abandoned', 'delivery_blocked')
    BEGIN
        SELECT RAISE(ABORT, 'invalid delivery chunk status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS delivery_chunk_status_update_guard
    BEFORE UPDATE OF status ON delivery_chunks
    WHEN NEW.status NOT IN
        ('pending', 'ready', 'claimed', 'sending', 'sent', 'retry_wait',
         'assumed_sent', 'failed', 'unknown', 'canceled', 'cancelled',
         'superseded', 'abandoned', 'delivery_blocked')
    BEGIN
        SELECT RAISE(ABORT, 'invalid delivery chunk status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS delivery_chunk_range_insert_guard
    BEFORE INSERT ON delivery_chunks
    WHEN NEW.ordinal <= 0 OR NEW.chunk_count <= 0 OR NEW.ordinal > NEW.chunk_count
    BEGIN
        SELECT RAISE(ABORT, 'delivery chunk ordinal is outside its count');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS delivery_chunk_range_update_guard
    BEFORE UPDATE OF ordinal, chunk_count ON delivery_chunks
    WHEN NEW.ordinal <= 0 OR NEW.chunk_count <= 0 OR NEW.ordinal > NEW.chunk_count
    BEGIN
        SELECT RAISE(ABORT, 'delivery chunk ordinal is outside its count');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS request_status_insert_guard
    BEFORE INSERT ON requests
    WHEN NEW.status NOT IN
        ('requested', 'accepted', 'downloading', 'imported_to_arr', 'pending',
         'processing', 'notifying',
         'visible_in_plex', 'blocked', 'failed', 'fulfilled', 'canceled',
         'cancelled', 'quarantined', 'delivered')
    BEGIN
        SELECT RAISE(ABORT, 'invalid request status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS request_status_update_guard
    BEFORE UPDATE OF status ON requests
    WHEN NEW.status NOT IN
        ('requested', 'accepted', 'downloading', 'imported_to_arr', 'pending',
         'processing', 'notifying',
         'visible_in_plex', 'blocked', 'failed', 'fulfilled', 'canceled',
         'cancelled', 'quarantined', 'delivered')
    BEGIN
        SELECT RAISE(ABORT, 'invalid request status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS subscription_status_insert_guard
    BEFORE INSERT ON subscriptions
    WHEN NEW.status NOT IN
        ('pending', 'tracking', 'active', 'fulfilled', 'disabled', 'failed',
         'blocked', 'canceled', 'cancelled', 'quarantined')
    BEGIN
        SELECT RAISE(ABORT, 'invalid subscription status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS subscription_status_update_guard
    BEFORE UPDATE OF status ON subscriptions
    WHEN NEW.status NOT IN
        ('pending', 'tracking', 'active', 'fulfilled', 'disabled', 'failed',
         'blocked', 'canceled', 'cancelled', 'quarantined')
    BEGIN
        SELECT RAISE(ABORT, 'invalid subscription status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS subscription_unit_status_insert_guard
    BEFORE INSERT ON subscription_units
    WHEN NEW.status NOT IN
        ('tracking', 'available', 'delivered', 'disabled', 'failed', 'blocked',
         'canceled', 'cancelled', 'unresolved', 'quarantined')
    BEGIN
        SELECT RAISE(ABORT, 'invalid subscription unit status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS subscription_unit_status_update_guard
    BEFORE UPDATE OF status ON subscription_units
    WHEN NEW.status NOT IN
        ('tracking', 'available', 'delivered', 'disabled', 'failed', 'blocked',
         'canceled', 'cancelled', 'unresolved', 'quarantined')
    BEGIN
        SELECT RAISE(ABORT, 'invalid subscription unit status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS request_status_transition_guard
    BEFORE UPDATE OF status ON requests
    WHEN NOT (
        NEW.status = OLD.status OR
        (OLD.status = 'requested' AND NEW.status IN ('accepted', 'visible_in_plex', 'blocked', 'failed', 'canceled', 'cancelled')) OR
        (OLD.status = 'accepted' AND NEW.status IN ('downloading', 'blocked', 'failed', 'canceled', 'cancelled')) OR
        (OLD.status = 'downloading' AND NEW.status IN ('imported_to_arr', 'blocked', 'failed', 'canceled', 'cancelled')) OR
        (OLD.status = 'imported_to_arr' AND NEW.status IN ('visible_in_plex', 'blocked', 'failed', 'canceled', 'cancelled')) OR
        (OLD.status = 'visible_in_plex' AND NEW.status IN ('fulfilled', 'delivered', 'blocked', 'failed')) OR
        (OLD.status IN ('blocked', 'failed') AND NEW.status IN ('accepted', 'canceled', 'cancelled', 'quarantined'))
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid request status transition');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS subscription_status_transition_guard
    BEFORE UPDATE OF status ON subscriptions
    WHEN NOT (
        NEW.status = OLD.status OR
        (OLD.status = 'pending' AND NEW.status IN ('tracking', 'active', 'fulfilled', 'disabled', 'failed', 'blocked', 'canceled', 'cancelled', 'quarantined')) OR
        (OLD.status = 'tracking' AND NEW.status IN ('active', 'fulfilled', 'disabled', 'failed', 'blocked', 'canceled', 'cancelled', 'quarantined')) OR
        (OLD.status = 'active' AND NEW.status IN ('fulfilled', 'disabled', 'failed', 'blocked', 'canceled', 'cancelled', 'quarantined')) OR
        (OLD.status IN ('failed', 'blocked', 'disabled') AND NEW.status IN ('tracking', 'active', 'disabled', 'canceled', 'cancelled', 'quarantined'))
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid subscription status transition');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS subscription_unit_status_transition_guard
    BEFORE UPDATE OF status ON subscription_units
    WHEN NOT (
        NEW.status = OLD.status OR
        (OLD.status IN ('tracking', 'unresolved') AND NEW.status IN ('available', 'disabled', 'failed', 'blocked', 'canceled', 'cancelled', 'quarantined')) OR
        (OLD.status = 'available' AND NEW.status IN ('delivered', 'tracking', 'disabled', 'failed', 'blocked', 'canceled', 'cancelled')) OR
        (OLD.status IN ('failed', 'blocked') AND NEW.status IN ('tracking', 'disabled', 'canceled', 'cancelled', 'quarantined'))
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid subscription unit status transition');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS request_command_status_guard
    BEFORE INSERT ON request_commands
    WHEN NEW.status NOT IN
        ('pending', 'ready', 'claimed', 'processing', 'running', 'retry_wait', 'succeeded',
         'failed', 'blocked', 'unknown', 'canceled', 'cancelled')
    BEGIN
        SELECT RAISE(ABORT, 'invalid request command status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS request_command_status_update_guard
    BEFORE UPDATE OF status ON request_commands
    WHEN NEW.status NOT IN
        ('pending', 'ready', 'claimed', 'processing', 'running', 'retry_wait', 'succeeded',
         'failed', 'blocked', 'unknown', 'canceled', 'cancelled')
    BEGIN
        SELECT RAISE(ABORT, 'invalid request command status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS request_command_status_transition_guard
    BEFORE UPDATE OF status ON request_commands
    WHEN NOT (
        NEW.status = OLD.status OR
        (OLD.status = 'pending' AND NEW.status IN ('ready', 'claimed', 'retry_wait', 'canceled', 'cancelled')) OR
        (OLD.status = 'ready' AND NEW.status IN ('claimed', 'retry_wait', 'canceled', 'cancelled')) OR
        (OLD.status = 'claimed' AND NEW.status IN ('processing', 'running', 'succeeded', 'retry_wait', 'failed', 'unknown', 'blocked', 'canceled', 'cancelled')) OR
        (OLD.status IN ('processing', 'running') AND NEW.status IN ('claimed', 'succeeded', 'retry_wait', 'failed', 'unknown', 'blocked', 'canceled', 'cancelled')) OR
        (OLD.status = 'retry_wait' AND NEW.status IN ('pending', 'ready', 'claimed', 'failed', 'canceled', 'cancelled')) OR
        (OLD.status = 'unknown' AND NEW.status IN ('succeeded', 'pending', 'failed', 'canceled', 'cancelled')) OR
        (OLD.status = 'failed' AND NEW.status IN ('pending', 'ready', 'claimed', 'canceled', 'cancelled'))
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid request command status transition');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS notification_group_status_insert_guard
    BEFORE INSERT ON notification_groups
    WHEN NEW.status NOT IN
        ('open', 'ready', 'claimed', 'sending', 'sent', 'assumed_sent', 'retry_wait',
         'failed', 'unknown', 'blocked', 'closed', 'canceled', 'cancelled',
         'superseded', 'abandoned')
    BEGIN
        SELECT RAISE(ABORT, 'invalid notification group status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS notification_group_status_update_guard
    BEFORE UPDATE OF status ON notification_groups
    WHEN NEW.status NOT IN
        ('open', 'ready', 'claimed', 'sending', 'sent', 'assumed_sent', 'retry_wait',
         'failed', 'unknown', 'blocked', 'closed', 'canceled', 'cancelled',
         'superseded', 'abandoned')
    BEGIN
        SELECT RAISE(ABORT, 'invalid notification group status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS notification_group_status_transition_guard
    BEFORE UPDATE OF status ON notification_groups
    WHEN NOT (
        NEW.status = OLD.status OR
        (OLD.status = 'open' AND NEW.status IN ('ready', 'claimed', 'canceled', 'cancelled', 'superseded', 'blocked')) OR
        (OLD.status = 'ready' AND NEW.status IN ('claimed', 'canceled', 'cancelled', 'superseded', 'blocked')) OR
        (OLD.status = 'claimed' AND NEW.status IN ('sending', 'retry_wait', 'failed', 'unknown', 'abandoned', 'canceled', 'cancelled')) OR
        (OLD.status = 'sending' AND NEW.status IN ('sent', 'unknown', 'failed')) OR
        (OLD.status = 'retry_wait' AND NEW.status IN ('open', 'ready', 'claimed', 'failed', 'abandoned')) OR
        (OLD.status = 'failed' AND NEW.status IN ('open', 'ready', 'abandoned')) OR
        (OLD.status = 'unknown' AND NEW.status IN ('sent', 'assumed_sent', 'open', 'superseded')) OR
        (OLD.status IN ('sent', 'assumed_sent') AND NEW.status = 'closed')
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid notification group status transition');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS delivery_membership_status_guard
    BEFORE INSERT ON delivery_memberships
    WHEN NEW.status NOT IN
        ('pending', 'eligible', 'fulfilled', 'disabled', 'failed', 'blocked',
         'canceled', 'cancelled', 'superseded', 'quarantined')
    BEGIN
        SELECT RAISE(ABORT, 'invalid delivery membership status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS delivery_membership_status_update_guard
    BEFORE UPDATE OF status ON delivery_memberships
    WHEN NEW.status NOT IN
        ('pending', 'eligible', 'fulfilled', 'disabled', 'failed', 'blocked',
         'canceled', 'cancelled', 'superseded', 'quarantined')
    BEGIN
        SELECT RAISE(ABORT, 'invalid delivery membership status');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS delivery_membership_status_transition_guard
    BEFORE UPDATE OF status ON delivery_memberships
    WHEN NOT (
        NEW.status = OLD.status OR
        (OLD.status IN ('pending', 'eligible') AND NEW.status IN ('fulfilled', 'disabled', 'failed', 'blocked', 'canceled', 'cancelled', 'superseded', 'quarantined')) OR
        (OLD.status IN ('failed', 'blocked') AND NEW.status IN ('pending', 'eligible', 'disabled', 'canceled', 'cancelled', 'superseded', 'quarantined'))
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid delivery membership status transition');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS notification_group_scope_guard
    BEFORE INSERT ON notification_groups
    WHEN NEW.window_generation <= 0 OR (NEW.season_number IS NOT NULL AND NEW.season_number < 0)
    BEGIN
        SELECT RAISE(ABORT, 'invalid notification group scope');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS notification_group_scope_update_guard
    BEFORE UPDATE OF season_number, window_generation ON notification_groups
    WHEN NEW.window_generation <= 0 OR (NEW.season_number IS NOT NULL AND NEW.season_number < 0)
    BEGIN
        SELECT RAISE(ABORT, 'invalid notification group scope');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS delivery_obligation_guard
    BEFORE INSERT ON deliveries
    WHEN NEW.obligation_key IS NOT NULL AND trim(NEW.obligation_key) = ''
    BEGIN
        SELECT RAISE(ABORT, 'delivery obligation key must not be blank');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS delivery_obligation_update_guard
    BEFORE UPDATE OF obligation_key ON deliveries
    WHEN NEW.obligation_key IS NOT NULL AND trim(NEW.obligation_key) = ''
    BEGIN
        SELECT RAISE(ABORT, 'delivery obligation key must not be blank');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS request_scope_guard
    BEFORE INSERT ON request_commands
    WHEN NEW.season_number IS NOT NULL AND NEW.season_number < 0
    BEGIN
        SELECT RAISE(ABORT, 'request command season is negative');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS request_scope_update_guard
    BEFORE UPDATE OF season_number ON request_commands
    WHEN NEW.season_number IS NOT NULL AND NEW.season_number < 0
    BEGIN
        SELECT RAISE(ABORT, 'request command season is negative');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS subscription_scope_guard
    BEFORE INSERT ON subscriptions
    WHEN NEW.season_number IS NOT NULL AND NEW.season_number < 0
    BEGIN
        SELECT RAISE(ABORT, 'subscription season is negative');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS subscription_scope_update_guard
    BEFORE UPDATE OF season_number ON subscriptions
    WHEN NEW.season_number IS NOT NULL AND NEW.season_number < 0
    BEGIN
        SELECT RAISE(ABORT, 'subscription season is negative');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS delivery_parent_chunk_range_guard
    BEFORE INSERT ON deliveries
    WHEN NEW.chunk_ordinal > NEW.chunk_count
    BEGIN
        SELECT RAISE(ABORT, 'delivery chunk ordinal is outside its count');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS delivery_parent_chunk_range_update_guard
    BEFORE UPDATE OF chunk_ordinal, chunk_count ON deliveries
    WHEN NEW.chunk_ordinal > NEW.chunk_count
    BEGIN
        SELECT RAISE(ABORT, 'delivery chunk ordinal is outside its count');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS migration_expansion_scope_guard
    BEFORE INSERT ON migration_expansions
    WHEN NEW.season_number IS NOT NULL AND NEW.season_number < 0
    BEGIN
        SELECT RAISE(ABORT, 'migration expansion season is negative');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS migration_expansion_scope_update_guard
    BEFORE UPDATE OF season_number ON migration_expansions
    WHEN NEW.season_number IS NOT NULL AND NEW.season_number < 0
    BEGIN
        SELECT RAISE(ABORT, 'migration expansion season is negative');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS migration_accounting_conservation_insert_guard
    BEFORE INSERT ON migration_accounting
    WHEN NEW.status IN ('completed', 'failed', 'skipped')
         AND NEW.source_rows <> NEW.migrated_rows + NEW.skipped_rows + NEW.failed_rows
    BEGIN
        SELECT RAISE(ABORT, 'migration accounting does not conserve source rows');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS migration_accounting_conservation_update_guard
    BEFORE UPDATE OF status, source_rows, migrated_rows, skipped_rows, failed_rows
        ON migration_accounting
    WHEN NEW.status IN ('completed', 'failed', 'skipped')
         AND NEW.source_rows <> NEW.migrated_rows + NEW.skipped_rows + NEW.failed_rows
    BEGIN
        SELECT RAISE(ABORT, 'migration accounting does not conserve source rows');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS migration_accounting_completed_immutable_guard
    BEFORE UPDATE ON migration_accounting
    WHEN OLD.status = 'completed' AND (
        NEW.migration_version <> OLD.migration_version OR
        NEW.migration_name <> OLD.migration_name OR
        NEW.source_name <> OLD.source_name OR
        NEW.status <> OLD.status OR
        NEW.source_rows <> OLD.source_rows OR
        NEW.migrated_rows <> OLD.migrated_rows OR
        NEW.skipped_rows <> OLD.skipped_rows OR
        NEW.failed_rows <> OLD.failed_rows OR
        COALESCE(NEW.started_at, '') <> COALESCE(OLD.started_at, '') OR
        COALESCE(NEW.completed_at, '') <> COALESCE(OLD.completed_at, '') OR
        COALESCE(NEW.details_json, '') <> COALESCE(OLD.details_json, '') OR
        COALESCE(NEW.error_text, '') <> COALESCE(OLD.error_text, '') OR
        NEW.created_at <> OLD.created_at
    )
    BEGIN
        SELECT RAISE(ABORT, 'completed migration accounting is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS migration_lineage_immutable_guard
    BEFORE UPDATE ON migration_lineage
    WHEN NEW.migration_id <> OLD.migration_id OR NEW.source_table <> OLD.source_table OR
         NEW.source_row_id <> OLD.source_row_id OR NEW.disposition <> OLD.disposition OR
         COALESCE(NEW.reason_code, '') <> COALESCE(OLD.reason_code, '') OR
         COALESCE(NEW.target_table, '') <> COALESCE(OLD.target_table, '') OR
         COALESCE(NEW.target_row_id, '') <> COALESCE(OLD.target_row_id, '') OR
         NEW.expansion_count <> OLD.expansion_count OR
         NEW.source_fingerprint <> OLD.source_fingerprint
    BEGIN
        SELECT RAISE(ABORT, 'migration lineage is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS migration_lineage_delete_guard
    BEFORE DELETE ON migration_lineage
    BEGIN
        SELECT RAISE(ABORT, 'migration lineage cannot be deleted');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS migration_expansion_immutable_guard
    BEFORE UPDATE ON migration_expansions
    WHEN NEW.lineage_id <> OLD.lineage_id OR
         NEW.target_table <> OLD.target_table OR
         COALESCE(NEW.target_row_id, '') <> COALESCE(OLD.target_row_id, '') OR
         COALESCE(NEW.season_number, -1) <> COALESCE(OLD.season_number, -1) OR
         NEW.created_at <> OLD.created_at
    BEGIN
        SELECT RAISE(ABORT, 'migration expansion is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS migration_expansion_delete_guard
    BEFORE DELETE ON migration_expansions
    BEGIN
        SELECT RAISE(ABORT, 'migration expansion cannot be deleted');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS migration_accounting_completed_delete_guard
    BEFORE DELETE ON migration_accounting
    WHEN OLD.status = 'completed'
    BEGIN
        SELECT RAISE(ABORT, 'completed migration accounting cannot be deleted');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS legacy_mapping_identity_guard
    BEFORE UPDATE ON legacy_source_mappings
    WHEN NEW.source_name <> OLD.source_name OR NEW.source_table <> OLD.source_table OR
         NEW.source_row_id <> OLD.source_row_id OR
         NEW.source_fingerprint <> OLD.source_fingerprint
    BEGIN
        SELECT RAISE(ABORT, 'legacy source mapping identity is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS delivery_status_transition_guard
    BEFORE UPDATE OF status ON deliveries
    WHEN NOT (
        NEW.status = OLD.status OR
        (OLD.status = 'pending' AND NEW.status IN ('ready', 'claimed', 'canceled', 'cancelled', 'superseded', 'delivery_blocked')) OR
        (OLD.status = 'ready' AND NEW.status IN ('claimed', 'canceled', 'cancelled', 'superseded', 'delivery_blocked')) OR
        (OLD.status = 'claimed' AND NEW.status IN ('pending', 'sending', 'retry_wait', 'failed', 'unknown', 'abandoned', 'canceled', 'cancelled', 'delivery_blocked')) OR
        (OLD.status = 'sending' AND NEW.status IN ('sent', 'unknown', 'failed')) OR
        (OLD.status = 'retry_wait' AND NEW.status IN ('pending', 'claimed', 'failed', 'abandoned')) OR
        (OLD.status = 'failed' AND NEW.status IN ('pending', 'abandoned', 'delivery_blocked')) OR
        (OLD.status = 'unknown' AND NEW.status IN ('assumed_sent', 'pending', 'superseded'))
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid delivery status transition');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS delivery_chunk_status_transition_guard
    BEFORE UPDATE OF status ON delivery_chunks
    WHEN NOT (
        NEW.status = OLD.status OR
        (OLD.status = 'pending' AND NEW.status IN ('ready', 'claimed', 'canceled', 'cancelled', 'superseded', 'delivery_blocked')) OR
        (OLD.status = 'ready' AND NEW.status IN ('claimed', 'canceled', 'cancelled', 'superseded', 'delivery_blocked')) OR
        (OLD.status = 'claimed' AND NEW.status IN ('pending', 'sending', 'retry_wait', 'failed', 'unknown', 'abandoned', 'canceled', 'cancelled', 'delivery_blocked')) OR
        (OLD.status = 'sending' AND NEW.status IN ('sent', 'unknown', 'failed')) OR
        (OLD.status = 'retry_wait' AND NEW.status IN ('pending', 'claimed', 'failed', 'abandoned', 'delivery_blocked')) OR
        (OLD.status = 'failed' AND NEW.status IN ('pending', 'abandoned', 'delivery_blocked')) OR
        (OLD.status = 'unknown' AND NEW.status IN ('assumed_sent', 'pending', 'superseded'))
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid delivery chunk status transition');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS event_status_transition_guard
    BEFORE UPDATE OF status ON event_inbox
    WHEN NOT (
        NEW.status = OLD.status OR
        (OLD.status IN ('received', 'queued', 'retry_wait') AND NEW.status IN ('queued', 'observed', 'ready', 'claimed', 'ignored', 'failed', 'quarantined', 'blocked')) OR
        (OLD.status = 'observed' AND NEW.status IN ('ready', 'queued', 'ignored', 'quarantined', 'blocked')) OR
        (OLD.status = 'ready' AND NEW.status IN ('claimed', 'processing', 'ignored', 'quarantined', 'blocked')) OR
        (OLD.status = 'claimed' AND NEW.status IN ('processing', 'handled', 'queued', 'failed', 'quarantined', 'blocked')) OR
        (OLD.status = 'processing' AND NEW.status IN ('claimed', 'handled', 'queued', 'failed', 'quarantined', 'blocked'))
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid event status transition');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS activation_singleton_guard
    BEFORE INSERT ON activation
    WHEN NEW.status IN ('baseline', 'active') AND EXISTS (
        SELECT 1 FROM activation WHERE status IN ('baseline', 'active')
    )
    BEGIN
        SELECT RAISE(ABORT, 'only one activation may be baseline or active');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS activation_singleton_update_guard
    BEFORE UPDATE OF status ON activation
    WHEN NEW.status IN ('baseline', 'active') AND EXISTS (
        SELECT 1 FROM activation
        WHERE status IN ('baseline', 'active') AND activation_id <> NEW.activation_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'only one activation may be baseline or active');
    END
    """,
)


MIGRATION = Migration(version=4, name="ledger_hardening", statements=STATEMENTS)

__all__ = ["MIGRATION", "STATEMENTS"]
