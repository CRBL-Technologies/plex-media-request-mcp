"""Operational indexes for the canonical ledger."""

from __future__ import annotations

from . import Migration


STATEMENTS: tuple[str, ...] = (
    """
    CREATE INDEX IF NOT EXISTS idx_migration_accounting_version
        ON migration_accounting(migration_version, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_requests_status_updated
        ON requests(status, updated_at, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_request_commands_ready
        ON request_commands(status, available_at, claim_expires_at, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_subscriptions_destination
        ON subscriptions(chat_id, status, provider_id, season_number)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_subscription_units_visible
        ON subscription_units(status, visible_in_plex_at, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_episode_enumerations_provider
        ON episode_enumerations(provider, provider_id, season_number, version)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_plex_items_provider
        ON plex_items(tmdb_id, tvdb_id, imdb_id, media_type)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_plex_crosswalks_provider
        ON plex_crosswalks(provider, provider_id, verified)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_activation_members_identity
        ON activation_members(activation_id, server_uuid, library_uuid, rating_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_activation_cursors_freshness
        ON activation_cursors(activation_id, last_incremental_at, last_full_sweep_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_event_inbox_ready
        ON event_inbox(status, available_at, claim_expires_at, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notification_groups_due
        ON notification_groups(status, due_at, destination, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_deliveries_ready
        ON deliveries(status, claim_expires_at, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_deliveries_destination
        ON deliveries(chat_id, notification_class, event_key, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_delivery_memberships_subscription
        ON delivery_memberships(subscription_id, subscription_generation, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_delivery_chunks_ready
        ON delivery_chunks(status, claim_expires_at, delivery_id, ordinal)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_confirmation_expiry
        ON confirmation_capabilities(state, expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_actor_nonce_expiry
        ON actor_nonces(expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_quarantined_records_open
        ON quarantined_records(status, resolved_at, reason_code, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audit_resource
        ON audit_events(resource_type, resource_id, created_at)
    """,
)


MIGRATION = Migration(
    version=3, name="ledger_operational_indexes", statements=STATEMENTS
)

__all__ = ["MIGRATION", "STATEMENTS"]
