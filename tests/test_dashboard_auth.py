from __future__ import annotations

import unittest

from media_dashboard.auth import (
    InvalidRequestOrigin,
    SessionStore,
    hash_password,
    validate_request_origin,
    verify_password,
)


class DashboardPasswordTests(unittest.TestCase):
    def test_scrypt_round_trip_and_wrong_password(self) -> None:
        encoded = hash_password("correct horse", salt=b"0123456789abcdef")
        self.assertTrue(verify_password("correct horse", encoded))
        self.assertFalse(verify_password("wrong", encoded))

    def test_malformed_or_weaker_hash_fails_closed(self) -> None:
        self.assertFalse(verify_password("password", "garbage"))
        encoded = hash_password("password", salt=b"0123456789abcdef")
        self.assertFalse(
            verify_password("password", encoded.replace("16384", "8192", 1))
        )
        self.assertFalse(
            verify_password("password", encoded.replace("16384", "016384", 1))
        )


class DashboardOriginTests(unittest.TestCase):
    def test_exact_lan_and_tailscale_origins(self) -> None:
        allowed = ("http://10.43.7.109:18082", "https://nas.tail.example")
        validate_request_origin(
            host="10.43.7.109:18082",
            origin="http://10.43.7.109:18082",
            allowed_origins=allowed,
            require_origin=True,
        )
        validate_request_origin(
            host="nas.tail.example",
            origin=None,
            allowed_origins=allowed,
            require_origin=False,
        )

    def test_unlisted_host_or_origin_is_rejected(self) -> None:
        allowed = ("http://nas.local:18082",)
        for host, origin in (
            ("evil.example", "http://nas.local:18082"),
            ("nas.local:18082", "https://evil.example"),
            ("nas.local:18082, evil.example", None),
        ):
            with self.subTest(host=host, origin=origin):
                with self.assertRaises(InvalidRequestOrigin):
                    validate_request_origin(
                        host=host,
                        origin=origin,
                        allowed_origins=allowed,
                        require_origin=origin is not None,
                    )

    def test_origin_must_match_the_requested_host(self) -> None:
        with self.assertRaises(InvalidRequestOrigin):
            validate_request_origin(
                host="nas.local:18082",
                origin="http://other.local:18082",
                allowed_origins=(
                    "http://nas.local:18082",
                    "http://other.local:18082",
                ),
                require_origin=True,
            )


class DashboardSessionTests(unittest.TestCase):
    def test_session_requires_matching_csrf_for_mutation(self) -> None:
        now = [100.0]
        store = SessionStore(
            clock=lambda: now[0], idle_seconds=30, absolute_seconds=100
        )
        secrets, _ = store.create()
        self.assertIsNotNone(store.validate(secrets.token))
        self.assertIsNone(
            store.validate(secrets.token, csrf_token="wrong", require_csrf=True)
        )
        self.assertIsNotNone(
            store.validate(
                secrets.token,
                csrf_token=secrets.csrf_token,
                require_csrf=True,
            )
        )

    def test_idle_absolute_and_clock_rollback_expire(self) -> None:
        for timestamp in (131.0, 201.0, 99.0):
            with self.subTest(timestamp=timestamp):
                now = [100.0]
                store = SessionStore(
                    clock=lambda: now[0], idle_seconds=30, absolute_seconds=100
                )
                secrets, _ = store.create()
                now[0] = timestamp
                self.assertIsNone(store.validate(secrets.token))

    def test_revoke_is_idempotent(self) -> None:
        store = SessionStore()
        secrets, _ = store.create()
        self.assertTrue(store.revoke(secrets.token))
        self.assertFalse(store.revoke(secrets.token))


if __name__ == "__main__":
    unittest.main()
