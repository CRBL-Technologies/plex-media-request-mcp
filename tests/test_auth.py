"""Focused tests for the transport-independent Phase-3 auth primitives."""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
from collections.abc import Mapping

import pytest

from media_companion.auth import (
    ACTOR_ASSERTION_CLOCK_SKEW_SECONDS,
    ACTOR_ASSERTION_LIFETIME_SECONDS,
    JCS_MAX_SAFE_INTEGER,
    ActorAssertionSigner,
    ActorAssertionVerifier,
    CanonicalizationError,
    ConfirmationBindingError,
    ConfirmationExpired,
    ConfirmationReplayError,
    DuplicateHeaderError,
    DuplicateJsonKeyError,
    InMemoryConfirmationTokenStore,
    InMemoryNonceReplayStore,
    InvalidAssertion,
    ReplayError,
    canonical_argument_hash,
    canonical_json,
    confirmation_callback_data,
    hash_confirmation_token,
    parse_canonical_json,
    parse_confirmation_callback_data,
    require_single_header,
)


def test_canonical_json_is_key_order_and_unicode_composition_stable_only() -> None:
    assert canonical_json({"b": 2, "a": "café"}) == b'{"a":"caf\xc3\xa9","b":2}'
    assert canonical_json({"a": "é"}) != canonical_json({"a": "e\u0301"})
    # JCS orders UTF-16 code units rather than Python's Unicode code points.
    assert canonical_json({"\U00010000": 1, "\ue000": 2}) == (
        b'{"\xf0\x90\x80\x80":1,"\xee\x80\x80":2}'
    )


def test_canonical_json_rejects_unsafe_values_and_duplicate_input_keys() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json(float("nan"))
    with pytest.raises(CanonicalizationError):
        canonical_json(1 << 60)
    with pytest.raises(CanonicalizationError):
        canonical_json("\ud800")
    with pytest.raises(DuplicateJsonKeyError):
        parse_canonical_json('{"x":1,"x":2}')
    with pytest.raises(DuplicateJsonKeyError):
        parse_canonical_json('{"x":1,"\\u0078":2}')
    with pytest.raises(CanonicalizationError):
        parse_canonical_json(b"\xff")
    with pytest.raises(CanonicalizationError):
        parse_canonical_json(b'{"b": 1, "a": 2}')


def test_canonical_json_number_boundaries_follow_ecmascript_jcs_shape() -> None:
    assert canonical_json(JCS_MAX_SAFE_INTEGER) == b"9007199254740991"
    assert canonical_json(-JCS_MAX_SAFE_INTEGER) == b"-9007199254740991"
    with pytest.raises(CanonicalizationError):
        canonical_json(JCS_MAX_SAFE_INTEGER + 1)
    with pytest.raises(CanonicalizationError):
        canonical_json(-(JCS_MAX_SAFE_INTEGER + 1))
    # The bounded protocol accepts finite IEEE-754 values, including values
    # whose exact decimal rendering is outside the safe integer range.  Larger
    # exact integers must be represented as strings by cross-language callers.
    assert canonical_json(1.0) == b"1"
    assert canonical_json(-0.0) == b"0"
    assert canonical_json(1e-6) == b"0.000001"
    assert canonical_json(1e-7) == b"1e-7"
    assert canonical_json(1e20) == b"100000000000000000000"
    assert canonical_json(1e21) == b"1e+21"


def _issue(signer: ActorAssertionSigner, *, now: int = 1_000) -> str:
    return signer.issue(
        audience="media-companion",
        tool="request_movie",
        arguments={"title": "The Matrix", "requester_user_id": 42},
        user_id=42,
        chat_id=42,
        chat_type="private",
        role="user",
        update_id=7,
        update_type="message",
        message_id=8,
        now=now,
        nonce="fixed-nonce",
    )


def _signed_body(key: bytes, body: bytes) -> str:
    signature = hmac.new(key, body, hashlib.sha256).digest()

    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    return f"{encode(body)}.{encode(signature)}"


def _signed_claims(key: bytes, claims: Mapping[str, object]) -> str:
    return _signed_body(key, canonical_json(claims))


def test_actor_assertion_round_trip_binds_audience_tool_and_exact_arguments() -> None:
    key = b"actor-signing-key-for-tests"
    signer = ActorAssertionSigner(key, kid="k1")
    replay = InMemoryNonceReplayStore()
    verifier = ActorAssertionVerifier(
        keys={"k1": key},
        expected_audience="media-companion",
        nonce_store=replay,
    )
    token = _issue(signer)
    claims = verifier.verify_bound(
        token,
        expected_audience="media-companion",
        expected_tool="request_movie",
        arguments={"requester_user_id": 42, "title": "The Matrix"},
        now=1_000,
    )
    assert claims.user_id == 42
    assert claims["audience"] == "media-companion"
    assert claims["argument_hash"] == canonical_argument_hash(
        {"title": "The Matrix", "requester_user_id": 42}
    )
    with pytest.raises(InvalidAssertion):
        verifier.verify(
            token,
            expected_tool="request_movie",
            arguments={"requester_user_id": 42, "title": "Alien"},
            now=1_000,
        )
    with pytest.raises(ReplayError):
        verifier.verify(
            token,
            expected_tool="request_movie",
            arguments={"requester_user_id": 42, "title": "The Matrix"},
            now=1_000,
        )
    with pytest.raises(InvalidAssertion):
        verifier.verify(token, expected_tool="request_movie", now=1_000)


def test_actor_assertion_expiry_skew_and_key_rotation() -> None:
    old_key = b"old-actor-signing-key"
    new_key = b"new-actor-signing-key"
    signer = ActorAssertionSigner(old_key, kid="old")
    verifier = ActorAssertionVerifier(
        keys={"old": old_key, "new": new_key},
        expected_audience="media-companion",
        nonce_store=InMemoryNonceReplayStore(),
    )
    token = _issue(signer, now=2_000)
    verifier.verify(
        token,
        expected_tool="request_movie",
        arguments={"title": "The Matrix", "requester_user_id": 42},
        now=2_000
        + ACTOR_ASSERTION_LIFETIME_SECONDS
        + ACTOR_ASSERTION_CLOCK_SKEW_SECONDS
        - 1,
    )
    with pytest.raises(InvalidAssertion):
        verifier.verify(
            token,
            expected_tool="request_movie",
            arguments={"title": "The Matrix", "requester_user_id": 42},
            now=(
                2_000
                + ACTOR_ASSERTION_LIFETIME_SECONDS
                + ACTOR_ASSERTION_CLOCK_SKEW_SECONDS
            ),
        )
    rotated = ActorAssertionSigner(new_key, kid="new").issue(
        audience="media-companion",
        tool="request_movie",
        arguments={},
        user_id=42,
        chat_id=42,
        chat_type="private",
        role="user",
        update_id=8,
        update_type="message",
        now=2_000,
    )
    assert (
        verifier.verify(
            rotated,
            expected_tool="request_movie",
            arguments={},
            now=2_000,
        ).kid
        == "new"
    )
    verifier.remove_key("old")
    with pytest.raises(InvalidAssertion):
        verifier.verify(
            token,
            expected_tool="request_movie",
            arguments={"title": "The Matrix", "requester_user_id": 42},
            now=2_000,
        )


def test_actor_verifier_requires_replay_store_and_bound_context() -> None:
    key = b"actor-signing-key-for-tests"
    signer = ActorAssertionSigner(key, kid="k1")
    with pytest.raises(TypeError):
        ActorAssertionVerifier(  # type: ignore[call-arg]
            keys={"k1": key}, expected_audience="media-companion"
        )
    verifier = ActorAssertionVerifier(
        keys={"k1": key},
        expected_audience="media-companion",
        nonce_store=InMemoryNonceReplayStore(),
    )
    token = _issue(signer)
    with pytest.raises(InvalidAssertion):
        verifier.verify(token, expected_tool="request_movie", now=1_000)
    with pytest.raises(InvalidAssertion):
        verifier.verify(
            token,
            expected_tool="request_movie",
            arguments={"title": "The Matrix", "requester_user_id": 42},
            expected_audience="",
            now=1_000,
        )
    with pytest.raises(InvalidAssertion):
        verifier.verify(
            token,
            expected_tool="request_movie",
            arguments={"title": "The Matrix", "requester_user_id": 42},
            now=1_000,
            consume_nonce=False,
        )


def test_actor_wire_claims_require_version_and_canonical_optional_fields() -> None:
    key = b"actor-signing-key-for-tests"
    signer = ActorAssertionSigner(key, kid="k1")
    verifier = ActorAssertionVerifier(
        keys={"k1": key},
        expected_audience="media-companion",
        nonce_store=InMemoryNonceReplayStore(),
    )
    token = _issue(signer)
    body_text, _ = token.split(".", 1)
    body = base64.urlsafe_b64decode(body_text + "===")
    parsed = parse_canonical_json(body)
    assert isinstance(parsed, dict)

    missing_version = dict(parsed)
    del missing_version["v"]
    with pytest.raises(InvalidAssertion):
        verifier.verify(
            _signed_claims(key, missing_version),
            expected_tool="request_movie",
            arguments={"title": "The Matrix", "requester_user_id": 42},
            now=1_000,
        )

    boolean_version = dict(parsed)
    boolean_version["v"] = True
    with pytest.raises(InvalidAssertion):
        verifier.verify(
            _signed_claims(key, boolean_version),
            expected_tool="request_movie",
            arguments={"title": "The Matrix", "requester_user_id": 42},
            now=1_000,
        )

    null_optional = dict(parsed)
    null_optional["session_id"] = None
    with pytest.raises(InvalidAssertion):
        verifier.verify(
            _signed_claims(key, null_optional),
            expected_tool="request_movie",
            arguments={"title": "The Matrix", "requester_user_id": 42},
            now=1_000,
        )

    fractional_numeric_claim = dict(parsed)
    fractional_numeric_claim["user_id"] = 42.5
    with pytest.raises(InvalidAssertion):
        verifier.verify(
            _signed_claims(key, fractional_numeric_claim),
            expected_tool="request_movie",
            arguments={"title": "The Matrix", "requester_user_id": 42},
            now=1_000,
        )

    with pytest.raises(InvalidAssertion):
        verifier.verify(
            _signed_body(key, body + b" "),
            expected_tool="request_movie",
            arguments={"title": "The Matrix", "requester_user_id": 42},
            now=1_000,
        )


def test_nonce_store_is_atomic_and_expires_entries() -> None:
    store = InMemoryNonceReplayStore()
    accepted: list[bool] = []

    def consume() -> None:
        accepted.append(store.consume("nonce", 20, now=10))

    threads = [threading.Thread(target=consume) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(accepted) == 1
    assert store.consume("nonce", 20, now=20) is False
    assert store.consume("nonce", 21, now=20) is True
    assert store.cleanup(now=21) == 1
    with pytest.raises(TypeError):
        store.consume("unsafe", JCS_MAX_SAFE_INTEGER + 1, now=10)


def test_duplicate_actor_headers_fail_closed_case_insensitively() -> None:
    assert require_single_header({"x-crbl-actor": "signed"}) == "signed"
    with pytest.raises(DuplicateHeaderError):
        require_single_header([("X-CRBL-Actor", "one"), ("x-crbl-actor", "two")])
    with pytest.raises(DuplicateHeaderError):
        require_single_header({"X-CRBL-Actor": "one,two"})


def test_confirmation_token_is_opaque_hash_only_and_one_time() -> None:
    now = 5_000
    store = InMemoryConfirmationTokenStore(clock=lambda: now)
    arguments = {"movie_id": 123}
    args_hash = canonical_argument_hash(arguments)
    issued = store.create(
        actor_user_id=42,
        actor_chat_id=42,
        tool="radarr_add_movie",
        argument_hash=args_hash,
        target_identity="tmdb:123",
        state_fingerprint="version:7",
        preview="Confirm <movie>?",
        policy_version="1",
        now=now,
    )
    assert len(issued.value) == 43
    assert issued.token_hash == hash_confirmation_token(issued.value)
    assert all(issued.value not in repr(record) for record in store.records)
    assert confirmation_callback_data(issued).startswith("crblc:")
    assert (
        parse_confirmation_callback_data(confirmation_callback_data(issued))
        == issued.value
    )

    bound = store.bind(
        issued,
        chat_id=42,
        message_id=900,
        preview="Confirm <movie>?",
        now=now,
    )
    assert bound.state == "armed"
    consumed = store.consume(
        issued,
        actor_user_id=42,
        actor_chat_id=42,
        tool="radarr_add_movie",
        argument_hash=args_hash,
        target_identity="tmdb:123",
        state_fingerprint="version:7",
        policy_version="1",
        chat_id=42,
        message_id=900,
        now=now,
    )
    assert consumed.state == "consumed"
    with pytest.raises(ConfirmationReplayError):
        store.consume(
            issued,
            actor_user_id=42,
            actor_chat_id=42,
            tool="radarr_add_movie",
            argument_hash=args_hash,
            target_identity="tmdb:123",
            state_fingerprint="version:7",
            policy_version="1",
            chat_id=42,
            message_id=900,
            now=now,
        )


def test_confirmation_binding_drift_and_expiry_are_denied() -> None:
    store = InMemoryConfirmationTokenStore()
    issued = store.create(
        actor_user_id=1,
        actor_chat_id=1,
        tool="repair_blocked_imports",
        argument_hash=hashlib.sha256(b"args").hexdigest(),
        target_identity="arr:4",
        state_fingerprint="v1",
        preview="Repair?",
        policy_version="1",
        now=10,
    )
    with pytest.raises(ConfirmationBindingError):
        store.bind(issued, chat_id=1, message_id=2, preview="Repair!", now=10)
    store.bind(issued, chat_id=1, message_id=2, preview="Repair?", now=10)
    with pytest.raises(TypeError):
        store.consume(  # type: ignore[call-arg]
            issued,
            actor_user_id=1,
            actor_chat_id=1,
            tool="repair_blocked_imports",
            argument_hash=hashlib.sha256(b"args").hexdigest(),
            target_identity="arr:4",
            state_fingerprint="v1",
            policy_version="1",
            now=10,
        )
    with pytest.raises(ConfirmationBindingError):
        store.consume(
            issued,
            actor_user_id=1,
            actor_chat_id=1,
            tool="repair_blocked_imports",
            argument_hash=hashlib.sha256(b"args").hexdigest(),
            target_identity="arr:4",
            state_fingerprint="v2",
            policy_version="1",
            chat_id=1,
            message_id=2,
            now=10,
        )
    with pytest.raises(ConfirmationBindingError):
        store.consume(
            issued,
            actor_user_id=1,
            actor_chat_id=1,
            tool="repair_blocked_imports",
            argument_hash=hashlib.sha256(b"args").hexdigest(),
            target_identity="arr:4",
            state_fingerprint="v1",
            policy_version="1",
            chat_id=1,
            message_id=3,
            now=10,
        )
    expiring = InMemoryConfirmationTokenStore()
    token = expiring.create(
        actor_user_id=1,
        actor_chat_id=1,
        tool="repair_blocked_imports",
        argument_hash=hashlib.sha256(b"args").hexdigest(),
        target_identity="arr:4",
        state_fingerprint="v1",
        preview="Repair?",
        policy_version="1",
        now=10,
    )
    with pytest.raises(ConfirmationExpired):
        expiring.bind(token, chat_id=1, message_id=2, preview="Repair?", now=310)
