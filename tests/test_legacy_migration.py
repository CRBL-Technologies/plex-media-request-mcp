from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

import media_companion.legacy_migration as legacy_module
from media_companion.legacy_migration import (
    LegacyMigrationError,
    LegacyDisposition,
    classify_legacy_row,
    create_verified_legacy_backup,
    dry_run_legacy_migration,
    import_legacy_rows,
    reconcile_legacy_deletion_intents,
)


def _legacy_row(
    row_id: int,
    *,
    media_type: str = "movie",
    chat_id: int | None = -1001,
    tmdb_id: int | None = 42,
    tvdb_id: int | None = None,
    seasons: object = None,
    status: str = "requested",
    notified: str | None = None,
) -> dict[str, object]:
    return {
        "id": row_id,
        "media_type": media_type,
        "title": f"Fixture {row_id}",
        "year": 2024,
        "requested_by_user_id": 7,
        "requested_by_chat_id": chat_id,
        "requested_by_username": "fixture",
        "tmdb_id": tmdb_id,
        "tvdb_id": tvdb_id,
        "imdb_id": None,
        "season_numbers": seasons,
        "status": status,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "notified_available_at": notified,
    }


def test_classifier_accepts_movie_null_seasons_and_series_season_zero() -> None:
    movie = classify_legacy_row(_legacy_row(1))
    series = classify_legacy_row(
        _legacy_row(
            2, media_type="series", tmdb_id=None, tvdb_id=99, seasons="[2, 0, 2]"
        )
    )

    assert movie.disposition is LegacyDisposition.MIGRATED
    assert movie.seasons == ()
    assert series.disposition is LegacyDisposition.MIGRATED
    assert series.seasons == (0, 2)
    assert series.expansion_count == 2


def test_classifier_quarantines_ambiguous_values_and_limits_delete_candidates() -> None:
    malformed = classify_legacy_row(_legacy_row(1, seasons="not-json"))
    unknown = classify_legacy_row(_legacy_row(2, media_type="album"))
    missing_destination = classify_legacy_row(_legacy_row(3, chat_id=None))
    missing_identity = classify_legacy_row(_legacy_row(4, tmdb_id=None))

    assert malformed.disposition is LegacyDisposition.QUARANTINED
    assert unknown.disposition is LegacyDisposition.QUARANTINED
    assert missing_destination.disposition is LegacyDisposition.DELETE_CANDIDATE
    assert missing_destination.delete_candidate
    assert missing_identity.disposition is LegacyDisposition.DELETE_CANDIDATE
    assert missing_identity.delete_candidate


def test_terminal_rows_archive_without_reenqueuing_invalid_history(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite3"
    _create_target(target)
    row = _legacy_row(
        1,
        chat_id=None,
        tmdb_id=None,
        seasons="not-json",
        status="available",
        notified="2026-01-02T00:00:00Z",
    )

    report = import_legacy_rows([row], target)

    assert report.disposition_counts == {"terminally_archived": 1}
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        assert (
            connection.execute("SELECT disposition FROM migration_lineage").fetchone()[
                0
            ]
            == "terminally_archived"
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM quarantined_records").fetchone()[0]
            == 0
        )


def test_dry_run_is_aggregate_redacted_and_does_not_write_source(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE media_requests (
                id INTEGER PRIMARY KEY, media_type TEXT, title TEXT,
                requested_by_chat_id INTEGER, tmdb_id INTEGER, tvdb_id INTEGER,
                imdb_id TEXT, season_numbers TEXT, status TEXT,
                created_at TEXT, updated_at TEXT, notified_available_at TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO media_requests VALUES (1, 'movie', 'secret title', -1, 42, NULL, NULL, NULL, 'requested', 'a', 'b', NULL)"
        )

    report = dry_run_legacy_migration(path)
    artifact = report.to_redacted_artifact()
    assert report.source_rows == 1
    assert artifact["residual"] == 0
    assert "secret title" not in json.dumps(artifact)
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM media_requests").fetchone()[0] == 1
        )


def _create_target(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, request_key TEXT NOT NULL UNIQUE,
                user_id INTEGER, chat_id INTEGER, username TEXT,
                requested_by_user_id INTEGER, requested_by_chat_id INTEGER,
                requested_by_username TEXT, media_type TEXT NOT NULL,
                provider_id TEXT NOT NULL, tmdb_id INTEGER, tvdb_id INTEGER,
                imdb_id TEXT, title TEXT NOT NULL, year INTEGER, seasons_json TEXT,
                mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'requested',
                provider_item_id TEXT, arr_id INTEGER, payload_json TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY, completed_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES (3, '2026-01-01T00:00:00Z');
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER,
                user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                destination TEXT NOT NULL, notification_class TEXT NOT NULL,
                media_type TEXT NOT NULL, provider_id TEXT NOT NULL,
                tmdb_id INTEGER, tvdb_id INTEGER, imdb_id TEXT,
                season_number INTEGER, mode TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 1, baseline INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active', created_at TEXT, updated_at TEXT,
                UNIQUE(user_id, chat_id, provider_id, media_type, season_number, generation)
            );
            CREATE TABLE subscription_units (
                id INTEGER PRIMARY KEY AUTOINCREMENT, subscription_id INTEGER NOT NULL,
                logical_unit_key TEXT NOT NULL, unit_type TEXT NOT NULL,
                provider_id TEXT, season_number INTEGER, episode_number INTEGER,
                expected INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'tracking',
                metadata_json TEXT, created_at TEXT, updated_at TEXT,
                UNIQUE(subscription_id, logical_unit_key)
            );
            CREATE TABLE migration_lineage (
                id INTEGER PRIMARY KEY AUTOINCREMENT, migration_id TEXT NOT NULL,
                source_table TEXT NOT NULL, source_row_id TEXT NOT NULL,
                disposition TEXT NOT NULL, reason_code TEXT, target_table TEXT,
                target_row_id TEXT, expansion_count INTEGER NOT NULL DEFAULT 0,
                source_fingerprint TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                UNIQUE(migration_id, source_table, source_row_id)
            );
            CREATE TABLE migration_expansions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, lineage_id INTEGER NOT NULL,
                target_table TEXT NOT NULL, target_row_id TEXT, season_number INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE quarantined_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
                source_id TEXT, record_type TEXT NOT NULL, reason_code TEXT NOT NULL,
                disposition TEXT NOT NULL, detail_json TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE legacy_source_mappings (
                source_name TEXT NOT NULL, source_table TEXT NOT NULL,
                source_row_id TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
                disposition TEXT NOT NULL CHECK (disposition IN
                    ('migrated', 'equivalently_merged', 'terminally_archived',
                     'deleted_after_approval', 'quarantined')), reason TEXT NOT NULL,
                target_request_id INTEGER, derived_item_count INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(source_name, source_table, source_row_id)
            );
            """
        )


def test_import_expands_series_and_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    _create_target(target)
    rows = [
        _legacy_row(1),
        _legacy_row(2, media_type="series", tmdb_id=None, tvdb_id=77, seasons="[0, 3]"),
    ]

    first = import_legacy_rows(rows, target)
    second = import_legacy_rows(rows, target)

    assert first.disposition_counts == {"migrated": 2}
    assert first.derived_expansion_count == 3
    assert second.disposition_counts == {"migrated": 2}
    assert second.derived_expansion_count == 3
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2
        assert (
            connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 3
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM subscription_units").fetchone()[0]
            == 3
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM migration_lineage").fetchone()[0]
            == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM migration_expansions").fetchone()[
                0
            ]
            == 3
        )


def test_equivalent_rows_reuse_canonical_units(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    _create_target(target)
    rows = [_legacy_row(1), _legacy_row(2)]

    report = import_legacy_rows(rows, target)

    assert report.disposition_counts == {
        "equivalently_merged": 1,
        "migrated": 1,
    }
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM subscription_units").fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM legacy_source_mappings"
            ).fetchone()[0]
            == 2
        )


def test_source_delete_requires_explicit_approval(tmp_path: Path) -> None:
    source = tmp_path / "legacy.sqlite3"
    target = tmp_path / "target.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE media_requests (id INTEGER PRIMARY KEY, media_type TEXT, requested_by_chat_id INTEGER, tmdb_id INTEGER, season_numbers TEXT, status TEXT, created_at TEXT, updated_at TEXT, notified_available_at TEXT)"
        )
        connection.execute(
            "INSERT INTO media_requests VALUES (1, 'movie', NULL, 42, NULL, 'requested', 'a', 'b', NULL)"
        )
    _create_target(target)

    preview = import_legacy_rows(source, target)
    assert preview.outcomes[0].disposition is LegacyDisposition.DELETE_CANDIDATE
    with sqlite3.connect(source) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM media_requests").fetchone()[0] == 1
        )

    with pytest.raises(LegacyMigrationError):
        import_legacy_rows(source, target, approve_deletes=True)

    backup = create_verified_legacy_backup(source, tmp_path / "legacy.rollback.sqlite3")
    approval = dry_run_legacy_migration(source).approve(
        backup, approved_delete_reasons={"missing_destination"}
    )
    applied = import_legacy_rows(source, target, approval=approval)
    assert applied.outcomes[0].disposition is LegacyDisposition.DELETED_AFTER_APPROVAL
    assert applied.deleted_source_rows == 1
    with sqlite3.connect(source) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM media_requests").fetchone()[0] == 0
        )


def test_verified_backup_and_approval_abort_on_source_change(tmp_path: Path) -> None:
    source = tmp_path / "legacy.sqlite3"
    target = tmp_path / "target.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE media_requests (id INTEGER PRIMARY KEY, media_type TEXT, "
            "title TEXT, requested_by_user_id INTEGER, requested_by_chat_id INTEGER, "
            "tmdb_id INTEGER, season_numbers TEXT, status TEXT, created_at TEXT, "
            "updated_at TEXT, notified_available_at TEXT, radarr_movie_id INTEGER)"
        )
        connection.execute(
            "INSERT INTO media_requests VALUES "
            "(1, 'movie', 'unchanged', 7, -1, 42, NULL, 'requested', 'a', 'b', NULL, 9001)"
        )
    _create_target(target)
    preview = dry_run_legacy_migration(source)
    backup = create_verified_legacy_backup(source, tmp_path / "rollback.sqlite3")
    approval = preview.approve(backup)
    assert backup.verified and backup.immutable
    assert backup.artifact_hash
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE media_requests SET title = 'changed' WHERE id = 1")
    with pytest.raises(LegacyMigrationError, match="approval|source identity|snapshot"):
        import_legacy_rows(source, target, approval=approval)
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


def test_malformed_row_gets_deterministic_surrogate_accounting_id(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite3"
    _create_target(target)
    row = _legacy_row(1)
    row.pop("id")
    first = import_legacy_rows([row], target)
    second = import_legacy_rows([row], target)
    assert first.disposition_counts == {"quarantined": 1}
    assert first.outcomes[0].source_row_id is not None
    assert first.outcomes[0].source_row_id.startswith("surrogate:")
    assert second.disposition_counts == {"quarantined": 1}
    with sqlite3.connect(target) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM legacy_source_mappings"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM migration_lineage").fetchone()[0]
            == 1
        )


def test_notifying_and_arr_state_are_preserved(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    _create_target(target)
    row = _legacy_row(1, status="notifying")
    row["radarr_movie_id"] = 1234
    report = import_legacy_rows([row], target)
    assert report.disposition_counts == {"migrated": 1}
    with sqlite3.connect(target) as connection:
        request = connection.execute(
            "SELECT status, provider_item_id, arr_id, payload_json, "
            "requested_by_user_id, requested_by_chat_id FROM requests"
        ).fetchone()
    assert request[0] == "notifying"
    assert request[1] == "1234"
    assert request[2] == 1234
    assert json.loads(request[3])["legacy_status"] == "notifying"
    assert request[4] == 7 and request[5] == -1001


def test_arr_state_without_canonical_identity_is_not_deleted(tmp_path: Path) -> None:
    row = _legacy_row(1, tmdb_id=None)
    row["radarr_movie_id"] = 1234
    classification = classify_legacy_row(row)
    assert classification.disposition is LegacyDisposition.QUARANTINED
    assert classification.reason == "provider_state_without_canonical_identity"


def test_missing_user_is_quarantined_instead_of_using_chat_id(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    _create_target(target)
    row = _legacy_row(1)
    row["requested_by_user_id"] = None
    report = import_legacy_rows([row], target)
    assert report.disposition_counts == {"quarantined": 1}
    assert report.reason_counts == {"missing_requester_user_id": 1}
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        assert (
            connection.execute("SELECT source_id FROM quarantined_records").fetchone()[
                0
            ]
            == "1"
        )


def test_alias_identity_conflict_is_quarantined(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    _create_target(target)
    row = _legacy_row(1)
    row["tmdbId"] = 43
    report = import_legacy_rows([row], target)
    assert report.disposition_counts == {"quarantined": 1}
    assert report.reason_counts == {"identity_conflict": 1}


def test_invalid_year_is_quarantined_not_coerced(tmp_path: Path) -> None:
    row = _legacy_row(1)
    row["year"] = "2024"
    classification = classify_legacy_row(row)
    assert classification.disposition is LegacyDisposition.QUARANTINED
    assert classification.reason == "invalid_year"


def test_text_is_bounded_and_control_characters_are_removed(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    _create_target(target)
    row = _legacy_row(1)
    row["title"] = "\x00\x01" + "x" * 2000
    row["requested_by_username"] = "user\n\x00" + "y" * 1000
    import_legacy_rows([row], target)
    with sqlite3.connect(target) as connection:
        title, username = connection.execute(
            "SELECT title, requested_by_username FROM requests"
        ).fetchone()
    assert len(title.encode()) <= 512 and "\x00" not in title and "\x01" not in title
    assert (
        len(username.encode()) <= 256
        and "\x00" not in username
        and "\n" not in username
    )


def test_source_race_aborts_before_target_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "legacy.sqlite3"
    target = tmp_path / "target.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE media_requests (id INTEGER PRIMARY KEY, media_type TEXT, "
            "title TEXT, requested_by_user_id INTEGER, requested_by_chat_id INTEGER, "
            "tmdb_id INTEGER, season_numbers TEXT, status TEXT, created_at TEXT, updated_at TEXT)"
        )
        connection.execute(
            "INSERT INTO media_requests VALUES (1, 'movie', 'old', 7, -1, 42, NULL, 'requested', 'a', 'b')"
        )
    _create_target(target)
    original_fence = legacy_module._source_fence

    @contextmanager
    def mutate_then_fence(
        source_rows: object, target: object, target_path: Path | None
    ):
        with sqlite3.connect(source) as writer:
            writer.execute("UPDATE media_requests SET title = 'race' WHERE id = 1")
        with original_fence(source_rows, target, target_path):
            yield

    monkeypatch.setattr(legacy_module, "_source_fence", mutate_then_fence)
    with pytest.raises(LegacyMigrationError, match="snapshot|source changed"):
        import_legacy_rows(source, target)
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


def test_prepared_deletion_is_reconciled_after_target_finalize_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "legacy.sqlite3"
    target = tmp_path / "target.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE media_requests (id INTEGER PRIMARY KEY, media_type TEXT, "
            "requested_by_chat_id INTEGER, tmdb_id INTEGER, season_numbers TEXT, "
            "status TEXT, created_at TEXT, updated_at TEXT, notified_available_at TEXT)"
        )
        connection.execute(
            "INSERT INTO media_requests VALUES (1, 'movie', NULL, 42, NULL, 'requested', 'a', 'b', NULL)"
        )
    _create_target(target)
    preview = dry_run_legacy_migration(source)
    backup = create_verified_legacy_backup(source, tmp_path / "rollback.sqlite3")
    approval = preview.approve(backup, approved_delete_reasons={"missing_destination"})
    original = legacy_module._record_mapping

    def fail_finalization(*args: object, **kwargs: object) -> None:
        disposition = args[2] if len(args) > 2 else None
        if disposition is LegacyDisposition.DELETED_AFTER_APPROVAL:
            raise RuntimeError("simulated crash")
        original(*args, **kwargs)

    monkeypatch.setattr(legacy_module, "_record_mapping", fail_finalization)
    with pytest.raises(RuntimeError, match="simulated crash"):
        import_legacy_rows(source, target, approval=approval)
    with sqlite3.connect(source) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM media_requests").fetchone()[0] == 0
        )
    monkeypatch.setattr(legacy_module, "_record_mapping", original)
    reconciled = reconcile_legacy_deletion_intents(source, target, approval=approval)
    assert reconciled.disposition_counts == {"deleted_after_approval": 1}
    with sqlite3.connect(target) as connection:
        assert (
            connection.execute(
                "SELECT disposition FROM legacy_source_mappings"
            ).fetchone()[0]
            == "deleted_after_approval"
        )


def test_same_database_delete_is_atomic(tmp_path: Path) -> None:
    target = tmp_path / "combined.sqlite3"
    _create_target(target)
    with sqlite3.connect(target) as connection:
        connection.execute(
            "CREATE TABLE media_requests (id INTEGER PRIMARY KEY, media_type TEXT, "
            "requested_by_chat_id INTEGER, tmdb_id INTEGER, season_numbers TEXT, "
            "status TEXT, created_at TEXT, updated_at TEXT, notified_available_at TEXT)"
        )
        connection.execute(
            "INSERT INTO media_requests VALUES (1, 'movie', NULL, 42, NULL, 'requested', 'a', 'b', NULL)"
        )
    preview = dry_run_legacy_migration(target)
    backup = create_verified_legacy_backup(
        target, tmp_path / "combined.rollback.sqlite3"
    )
    approval = preview.approve(backup, approved_delete_reasons={"missing_destination"})
    report = import_legacy_rows(target, target, approval=approval)
    assert report.disposition_counts == {"deleted_after_approval": 1}
    with sqlite3.connect(target) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM media_requests").fetchone()[0] == 0
        )
        assert (
            connection.execute(
                "SELECT disposition FROM legacy_source_mappings"
            ).fetchone()[0]
            == "deleted_after_approval"
        )


def test_existing_lineage_is_not_overwritten_when_source_id_is_reused(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite3"
    _create_target(target)
    first = _legacy_row(1)
    import_legacy_rows([first], target)
    changed = dict(first)
    changed["title"] = "reused-id"
    with pytest.raises(LegacyMigrationError, match="changed after disposition"):
        import_legacy_rows([changed], target)
    with sqlite3.connect(target) as connection:
        stored = connection.execute(
            "SELECT source_fingerprint, disposition FROM legacy_source_mappings"
        ).fetchone()
    assert stored[0] != legacy_module._fingerprint(changed)
    assert stored[1] == "migrated"
