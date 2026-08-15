from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from media_companion.cleanup import (
    CleanupAction,
    plan_cleanup,
    select_quarantine_alerts,
    scrub_payload,
)
from media_companion.delivery import (
    ClaimConflictError,
    ClockGuard,
    DeliveryRecord,
    DeliveryState,
    DeliveryTransitionError,
    LeaseTooShortError,
    TelegramFailureClass,
    alert_due,
    assume_sent,
    begin_sending,
    claim_delivery,
    expire_claim,
    fail_before_transmission,
    mark_sent,
    mark_unknown,
    process_telegram_failure,
    record_alert,
    retry_failed_once,
    resend_once,
    renew_claim,
)
from media_companion.models import MediaType, NotificationClass, RequestMode
from media_companion.planner import (
    ActivationBaseline,
    ActivationError,
    ActivationDisposition,
    ActivationPass,
    CanonicalUnit,
    IncompleteScanError,
    PlanningError,
    Subscription,
    assemble_completed_seasons,
    build_obligations,
    classify_activation_item,
    evaluate_oracle,
    plan_notifications,
    plan_groups,
    render_group_chunks,
    render_group_text,
)

UTC = timezone.utc
BASE = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def episode(key: str, offset: int, number: int) -> CanonicalUnit:
    return CanonicalUnit(
        unit_id=key,
        media_type=MediaType.EPISODE,
        visible_in_plex_at=BASE + timedelta(seconds=offset),
        show_identity="show-1",
        season_number=1,
        episode_number=number,
        title=f"Episode {number}",
        server_uuid="server-1",
        library_uuid="library-1",
        snapshot_verified=True,
        playable=True,
    )


def test_activation_requires_strict_verified_added_at() -> None:
    historical = classify_activation_item(
        {"logical_key": "old", "added_at": BASE + timedelta(hours=2)},
        baseline_started_at=BASE,
        pass_one_membership=("old",),
    )
    assert historical.disposition is ActivationDisposition.HISTORICAL

    assert classify_activation_item(
        {"logical_key": "equal", "added_at": BASE}, baseline_started_at=BASE
    ).quarantined
    assert classify_activation_item(
        {
            "logical_key": "coarse",
            "added_at": BASE + timedelta(seconds=1),
            "coarse": True,
        },
        baseline_started_at=BASE,
    ).quarantined
    assert classify_activation_item(
        {"logical_key": "new", "added_at": BASE + timedelta(seconds=1)},
        baseline_started_at=BASE,
    ).is_new


def test_activation_pass_cannot_activate_from_incomplete_pagination() -> None:
    with pytest.raises(IncompleteScanError):
        ActivationPass(1, frozenset({"a"}), complete=False).require_valid()


def test_fixed_window_boundary_and_earliest_first_seen() -> None:
    first = episode("one", 0, 1)
    boundary = episode("boundary", 300, 2)
    obligations = [
        build_obligations([first], admin_destinations=[-100])[0],
        build_obligations([boundary], admin_destinations=[-100])[0],
    ]
    groups = plan_groups([boundary, first], obligations)
    assert len(groups) == 2
    assert groups[0].first_seen_at == first.visible_in_plex_at
    assert groups[0].due_at == first.visible_in_plex_at + timedelta(minutes=5)


def test_same_destination_admin_wins_but_retains_requester_detail() -> None:
    unit = episode("episode", 1, 4)
    subscription = Subscription(
        generation=1,
        destination=-100,
        active_at=BASE,
        user_id=42,
        unit_keys=frozenset({"episode"}),
        mode=RequestMode.AIRING_EPISODE,
    )
    obligations = build_obligations(
        [unit], subscriptions=[subscription], admin_destinations=[-100]
    )
    admin = next(
        row for row in obligations if row.notification_class is NotificationClass.ADMIN
    )
    requester = next(
        row
        for row in obligations
        if row.notification_class is NotificationClass.REQUESTER
    )
    assert admin.requester_detail
    assert requester.state == "suppressed"
    groups = plan_groups([unit], obligations)
    assert len(groups) == 1
    assert "S01E04" in render_group_text(groups[0], [unit])


def test_oracle_requires_one_accounted_state_and_complete_scan() -> None:
    obligation = build_obligations([episode("episode", 1, 1)], admin_destinations=[1])[
        0
    ]
    incomplete = evaluate_oracle([obligation], {}, scan_complete=False)
    assert not incomplete.ready
    complete = evaluate_oracle([obligation], {obligation.key: "ready"})
    assert complete.ready


def test_claim_lease_and_unknown_only_from_sending() -> None:
    record = DeliveryRecord("delivery", 1, NotificationClass.ADMIN, "key")
    claimed = claim_delivery(record, worker_id="worker", now=BASE, lease_seconds=60)
    with pytest.raises(DeliveryTransitionError):
        mark_unknown(claimed, claimed.claim, now=BASE, reason="timeout")

    sending = begin_sending(claimed, claimed.claim, now=BASE)
    unknown = mark_unknown(sending, sending.claim, now=BASE, reason="timeout")
    assert unknown.state is DeliveryState.UNKNOWN
    assert (
        assume_sent(unknown, confirmed=True, now=BASE).state
        is DeliveryState.ASSUMED_SENT
    )


def test_claim_lease_must_cover_send_deadline_and_stale_token_is_fenced() -> None:
    record = DeliveryRecord("delivery", 1, NotificationClass.ADMIN, "key")
    claimed = claim_delivery(record, worker_id="worker", now=BASE, lease_seconds=10)
    with pytest.raises(LeaseTooShortError):
        begin_sending(claimed, claimed.claim, now=BASE)
    renewed = claim_delivery(record, worker_id="worker", now=BASE, lease_seconds=60)
    with pytest.raises(ClaimConflictError):
        begin_sending(renewed, claimed.claim, now=BASE)


def test_retry_schedule_exhausts_then_failed_and_unknown_resend_is_explicit() -> None:
    record = DeliveryRecord("delivery", 1, NotificationClass.ADMIN, "key")
    current = BASE
    for expected_delay in (60, 300, 900, 3600, 21600, 86400):
        claimed = claim_delivery(
            record, worker_id="worker", now=current, lease_seconds=60
        )
        record = fail_before_transmission(claimed, claimed.claim, now=current)
        assert record.state is DeliveryState.RETRY_WAIT
        assert record.retry_due_at == current + timedelta(seconds=expected_delay)
        current = record.retry_due_at
    claimed = claim_delivery(record, worker_id="worker", now=current, lease_seconds=60)
    record = fail_before_transmission(claimed, claimed.claim, now=current)
    assert record.state is DeliveryState.FAILED

    # The resend test uses a record transitioned through the real sending path.
    base = claim_delivery(
        DeliveryRecord("unknown", 1, NotificationClass.ADMIN, "unknown"),
        worker_id="worker",
        now=BASE,
        lease_seconds=60,
    )
    sending = begin_sending(base, base.claim, now=BASE)
    unknown = mark_unknown(sending, sending.claim, now=BASE, reason="timeout")
    recovery = resend_once(unknown, confirmed=True, now=BASE)
    assert recovery.previous.state is DeliveryState.SUPERSEDED
    assert recovery.retry.state is DeliveryState.PENDING
    assert recovery.retry.possible_duplicate
    resend_claim = claim_delivery(
        recovery.retry, worker_id="worker", now=BASE, lease_seconds=60
    )
    assert (
        fail_before_transmission(resend_claim, resend_claim.claim, now=BASE).state
        is DeliveryState.FAILED
    )


def test_expired_claimed_work_is_reclaimable_but_unknown_is_not() -> None:
    record = DeliveryRecord("delivery", 1, NotificationClass.ADMIN, "key")
    claimed = claim_delivery(record, worker_id="worker", now=BASE, lease_seconds=1)
    reclaimed = expire_claim(claimed, now=BASE + timedelta(seconds=2))
    assert reclaimed.state is DeliveryState.PENDING
    sending_base = claim_delivery(
        record, worker_id="worker", now=BASE, lease_seconds=60
    )
    sending = begin_sending(sending_base, sending_base.claim, now=BASE)
    unknown = expire_claim(sending, now=BASE + timedelta(seconds=80))
    assert unknown.state is DeliveryState.UNKNOWN


def test_cleanup_excludes_unknown_and_scrubs_terminal_payload() -> None:
    old = BASE - timedelta(days=61)
    plan = plan_cleanup(
        [
            {"id": "sent", "kind": "delivery", "status": "sent", "terminal_at": old},
            {
                "id": "unknown",
                "kind": "delivery",
                "status": "unknown",
                "terminal_at": old,
            },
        ],
        now=BASE,
    )
    assert len(plan.candidates) == 1
    assert plan.candidates[0].action is CleanupAction.SCRUB
    scrubbed = scrub_payload(
        {
            "chat_id": 42,
            "username": "alice",
            "logical_dedupe_key": "stable",
            "nested": {"message_id": 1},
        }
    )
    assert scrubbed == {"logical_dedupe_key": "stable", "nested": {}}


def test_planner_readiness_requires_fresh_scan_evidence() -> None:
    unit = episode("ready", 1, 1)
    pending = plan_notifications([unit], admin_destinations=[1])
    assert not pending.accounting.ready
    assert pending.accounting.reason == "incremental scan incomplete"
    complete = plan_notifications(
        [unit],
        admin_destinations=[1],
        scan_complete=True,
        full_sweep_complete=True,
        scans_fresh=True,
    )
    assert complete.accounting.ready


def test_activation_requires_fresh_ordered_passes_and_accounting() -> None:
    baseline = ActivationBaseline("activation", BASE)
    pass_one = ActivationPass(
        1, frozenset({"old"}), complete=True, fresh_at=BASE + timedelta(seconds=1)
    )
    pass_two = ActivationPass(
        2,
        frozenset({"old", "new"}),
        complete=True,
        fresh_at=BASE + timedelta(seconds=2),
    )
    activated = (
        baseline.record_pass(pass_one)
        .record_pass(pass_two)
        .activate(completed_at=BASE + timedelta(seconds=3))
    )
    with pytest.raises(ActivationError):
        activated.enable_delivery(
            accounting=evaluate_oracle([], {}, scan_complete=False),
            scan_complete=True,
            full_sweep_complete=True,
            scans_fresh=True,
        )
    accounting = evaluate_oracle([], {})
    enabled = activated.enable_delivery(
        accounting=accounting,
        scan_complete=True,
        full_sweep_complete=True,
        scans_fresh=True,
    )
    assert enabled.delivery_enabled
    with pytest.raises(IncompleteScanError):
        ActivationPass(1, frozenset({"old"}), complete=True).require_valid()


def test_canonical_identity_and_snapshot_validation_are_fail_closed() -> None:
    with pytest.raises(PlanningError):
        CanonicalUnit.from_record(
            {
                "unit_id": "x",
                "media_type": "episode",
                "visible_in_plex_at": BASE,
                "show_identity": "show",
                "season_number": 1,
                "episode_number": 1,
                "server_uuid": "server",
                "library_uuid": "library",
            }
        )
    with pytest.raises(PlanningError):
        CanonicalUnit(
            unit_id="series",
            media_type=MediaType.SERIES,
            visible_in_plex_at=BASE,
            server_uuid="server",
            library_uuid="library",
            snapshot_verified=True,
            playable=True,
        )
    assert classify_activation_item(
        {"added_at": BASE + timedelta(seconds=1)}, baseline_started_at=BASE
    ).quarantined


def test_shared_chat_membership_identity_survives_same_generation() -> None:
    unit = episode("shared", 1, 1)
    subscriptions = [
        Subscription(
            generation=1,
            destination=-100,
            active_at=BASE,
            user_id=101,
            unit_keys=frozenset({"shared"}),
            mode=RequestMode.AIRING_EPISODE,
        ),
        Subscription(
            generation=1,
            destination=-100,
            active_at=BASE,
            user_id=202,
            unit_keys=frozenset({"shared"}),
            mode=RequestMode.AIRING_EPISODE,
        ),
    ]
    obligations = build_obligations([unit], subscriptions=subscriptions)
    assert len({row.key for row in obligations}) == 2
    assert {
        str(row.accounting_generation).split(":scope:", 1)[0] for row in obligations
    } == {
        "user:101:destination:-100",
        "user:202:destination:-100",
    }


def test_duplicate_versions_collapse_by_identity_but_not_tombstone_generation() -> None:
    base = episode("rating-1080", 1, 1)
    high_quality = replace(
        base,
        unit_id="rating-4k",
        logical_identity="tmdb:123",
        provider_identity="tmdb:123",
        quality="4K",
        library_priority=1,
        resolution=2160,
    )
    low_quality = replace(
        base,
        logical_identity="tmdb:123",
        provider_identity="tmdb:123",
        quality="1080p",
        library_priority=0,
        resolution=1080,
    )
    assert (
        len(build_obligations([high_quality, low_quality], admin_destinations=[1])) == 1
    )
    new_generation = replace(high_quality, tombstone_generation=1)
    assert (
        len(build_obligations([low_quality, new_generation], admin_destinations=[1]))
        == 2
    )


def test_season_completion_requires_expected_playable_set_and_assembly() -> None:
    first = episode("s1e1", 1, 1)
    second = episode("s1e2", 2, 2)
    complete_subscription = Subscription(
        generation=1,
        destination=9,
        active_at=BASE,
        user_id=1,
        unit_keys=frozenset({"s1e1", "s1e2"}),
        required_unit_keys=frozenset({"s1e1", "s1e2"}),
        enumeration_complete=True,
        season_ended=True,
        mode=RequestMode.SEASON_COMPLETION,
    )
    incomplete_subscription = Subscription(
        generation=1,
        destination=9,
        active_at=BASE,
        user_id=2,
        unit_keys=frozenset({"s1e1", "s1e2"}),
        required_unit_keys=frozenset({"s1e1", "s1e2", "missing"}),
        enumeration_complete=True,
        season_ended=True,
        mode=RequestMode.SEASON_COMPLETION,
    )
    complete_rows = build_obligations(
        [first, second], subscriptions=[complete_subscription]
    )
    assert len(complete_rows) == 2
    incomplete_rows = build_obligations(
        [first, second], subscriptions=[incomplete_subscription]
    )
    assert incomplete_rows == ()

    season_two = CanonicalUnit(
        unit_id="s2e1",
        media_type=MediaType.EPISODE,
        visible_in_plex_at=first.visible_in_plex_at,
        show_identity="show-1",
        season_number=2,
        episode_number=1,
        title="Episode 1",
        server_uuid="server-1",
        library_uuid="library-1",
        snapshot_verified=True,
        playable=True,
    )
    second_subscription = replace(
        complete_subscription,
        user_id=1,
        unit_keys=frozenset({"s2e1"}),
        required_unit_keys=frozenset({"s2e1"}),
    )
    season_one_group = plan_groups([first, second], complete_rows)[0].ready()
    season_two_rows = build_obligations(
        [season_two], subscriptions=[second_subscription]
    )
    season_two_group = plan_groups([season_two], season_two_rows)[0].ready()
    assembled = assemble_completed_seasons([season_one_group, season_two_group])
    assert len(assembled) == 1
    assert assembled[0].season_number is None
    assert assembled[0].source_group_keys
    assert set(assembled[0].unit_keys) == {"s1e1", "s1e2", "s2e1"}

    open_group = replace(season_two_group, state="open")
    passthrough = assemble_completed_seasons([season_one_group, open_group])
    assert {row.idempotency_key for row in passthrough} == {
        season_one_group.idempotency_key,
        open_group.idempotency_key,
    }


def test_renderer_escapes_urls_and_chunks_at_unit_boundaries() -> None:
    units = [
        CanonicalUnit(
            unit_id=f"render-{number}",
            media_type=MediaType.EPISODE,
            visible_in_plex_at=BASE + timedelta(seconds=number),
            show_identity="<Show>&\nInjected",
            season_number=1,
            episode_number=number,
            title="<b>unsafe</b> " + ("x" * 20),
            quality="4K & HDR",
            plex_url="https://plex.example/library/metadata/1?token=secret",
            server_uuid="server-1",
            library_uuid="library-1",
            snapshot_verified=True,
            playable=True,
        )
        for number in range(1, 5)
    ]
    obligations = build_obligations(units, admin_destinations=[1])
    group = plan_groups(units, obligations)[0]
    chunks = render_group_chunks(group, units, max_bytes=150)
    assert len(chunks) > 1
    assert all(len(chunk.encode("utf-8")) <= 150 for chunk in chunks)
    assert all("&lt;Show&gt;" in chunk for chunk in chunks)
    assert all("token=secret" not in chunk for chunk in chunks)
    assert all("S01E" in chunk for chunk in chunks)
    with pytest.raises(PlanningError):
        render_group_text(group, units, max_bytes=150)


def test_claims_are_durable_version_fenced_and_bare_tokens_rejected() -> None:
    record = DeliveryRecord("versioned", 1, NotificationClass.ADMIN, "versioned")
    first = claim_delivery(record, worker_id="worker", now=BASE, lease_seconds=60)
    renewed = renew_claim(first, first.claim, now=BASE, lease_seconds=60)
    with pytest.raises(ClaimConflictError):
        begin_sending(renewed, first.claim, now=BASE)
    with pytest.raises(ClaimConflictError):
        begin_sending(renewed, renewed.claim, now=BASE, leader_epoch=99)
    with pytest.raises(ClaimConflictError):
        begin_sending(renewed, renewed.claim_token or "", now=BASE)
    assert (
        begin_sending(renewed, renewed.claim, now=BASE).state is DeliveryState.SENDING
    )


def test_clock_rollback_blocks_new_claims_until_operator_clears_guard() -> None:
    from media_companion.planner import ClockRollbackError

    clock = ClockGuard(last_seen=BASE)
    with pytest.raises(ClockRollbackError):
        claim_delivery(
            DeliveryRecord("clock", 1, NotificationClass.ADMIN, "clock"),
            worker_id="worker",
            now=BASE - timedelta(minutes=1),
            clock=clock,
        )
    with pytest.raises(ClockRollbackError):
        claim_delivery(
            DeliveryRecord("clock-2", 1, NotificationClass.ADMIN, "clock-2"),
            worker_id="worker",
            now=BASE,
            clock=clock,
        )
    clock.clear(BASE)
    assert (
        claim_delivery(
            DeliveryRecord("clock-3", 1, NotificationClass.ADMIN, "clock-3"),
            worker_id="worker",
            now=BASE,
            clock=clock,
        ).state
        is DeliveryState.CLAIMED
    )


def test_failure_classes_follow_safe_transitions_without_disabling_other_chats() -> (
    None
):
    record = DeliveryRecord("failure", 1, NotificationClass.ADMIN, "failure")
    claimed = claim_delivery(record, worker_id="worker", now=BASE, lease_seconds=60)
    retry = process_telegram_failure(
        claimed,
        claimed.claim,
        failure_class=TelegramFailureClass.RATE_LIMITED,
        retry_after_seconds=90,
        now=BASE,
    )
    assert retry.state is DeliveryState.RETRY_WAIT
    sending_base = claim_delivery(
        retry, worker_id="worker", now=BASE + timedelta(seconds=90)
    )
    sending = begin_sending(
        sending_base, sending_base.claim, now=BASE + timedelta(seconds=90)
    )
    failed = process_telegram_failure(
        sending,
        sending.claim,
        failure_class=TelegramFailureClass.APPLICATION,
        error="400 https://plex.example/?token=secret",
        now=BASE + timedelta(seconds=91),
    )
    assert failed.state is DeliveryState.FAILED
    assert "token=secret" not in (failed.last_error or "")
    blocked_base = claim_delivery(
        DeliveryRecord("blocked", 1, NotificationClass.ADMIN, "blocked"),
        worker_id="worker",
        now=BASE,
        lease_seconds=60,
    )
    blocked = process_telegram_failure(
        blocked_base,
        blocked_base.claim,
        failure_class=TelegramFailureClass.DESTINATION_BLOCKED,
        now=BASE,
    )
    assert blocked.state is DeliveryState.DELIVERY_BLOCKED


def test_send_deadline_and_expiry_use_deadline_not_claim_lease() -> None:
    record = DeliveryRecord("deadline", 1, NotificationClass.ADMIN, "deadline")
    claimed = claim_delivery(record, worker_id="worker", now=BASE, lease_seconds=60)
    sending = begin_sending(claimed, claimed.claim, now=BASE)
    with pytest.raises(DeliveryTransitionError):
        mark_sent(sending, sending.claim, now=BASE + timedelta(seconds=31))
    assert (
        expire_claim(sending, now=BASE + timedelta(seconds=20)).state
        is DeliveryState.SENDING
    )
    assert (
        expire_claim(sending, now=BASE + timedelta(seconds=31)).state
        is DeliveryState.UNKNOWN
    )


def test_failure_alerts_repeat_after_recovery_generation() -> None:
    failed = DeliveryRecord(
        "alert",
        1,
        NotificationClass.ADMIN,
        "alert",
        state=DeliveryState.FAILED,
        terminal_at=BASE,
    )
    assert alert_due(failed, now=BASE)
    failed = record_alert(failed, now=BASE)
    assert not alert_due(failed, now=BASE + timedelta(hours=1))
    assert alert_due(failed, now=BASE + timedelta(days=1))
    failed = record_alert(failed, now=BASE + timedelta(days=1))
    assert alert_due(failed, now=BASE + timedelta(days=7))
    failed = record_alert(failed, now=BASE + timedelta(days=7))
    pending = retry_failed_once(failed, confirmed=True, now=BASE + timedelta(days=8))
    assert pending.alert_count == 0
    assert pending.last_alert_at is None
    recovery_claim = claim_delivery(
        pending, worker_id="worker", now=BASE + timedelta(days=8), lease_seconds=60
    )
    recovery_failed = fail_before_transmission(
        recovery_claim, recovery_claim.claim, now=BASE + timedelta(days=8)
    )
    assert recovery_failed.state is DeliveryState.FAILED


def test_cleanup_resolves_quarantine_repeat_alerts_and_scrubs_identity_fields() -> None:
    old = BASE - timedelta(days=61)
    unresolved = {
        "id": "q1",
        "kind": "quarantine",
        "status": "quarantined",
        "quarantined_at": old,
    }
    assert plan_cleanup([unresolved], now=BASE).candidates == ()
    assert len(select_quarantine_alerts([unresolved], now=BASE)) == 1
    assert (
        select_quarantine_alerts(
            [{**unresolved, "last_alert_at": BASE - timedelta(days=1)}], now=BASE
        )
        == ()
    )
    resolved = {
        **unresolved,
        "resolved": True,
        "resolved_at": old + timedelta(days=1),
    }
    assert len(plan_cleanup([resolved], now=BASE).candidates) == 1
    scrubbed = scrub_payload(
        {
            "actorUserId": 42,
            "chatId": -100,
            "error": "token=secret https://private.example/a",
            "dedupe_key": "should-not-survive-by-default",
            "logical_dedupe_key": "stable",
            "nested": {"resolvedBy": "operator", "title": "safe"},
        }
    )
    assert scrubbed == {"logical_dedupe_key": "stable", "nested": {"title": "safe"}}
