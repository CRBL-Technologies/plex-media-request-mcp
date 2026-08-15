from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path

from hermes_media_extension.whitelist_helper import (
    AdminRemovalDenied,
    FingerprintMismatch,
    PolicyParseError,
    SymlinkRejected,
    add_allowed_user,
    extract_blocked_user_event,
    parse_allowed_users,
    parse_blocked_user_logs,
    remove_allowed_user,
    semantic_fingerprint,
)


SYNTHETIC_USER_1 = 900_000_001
SYNTHETIC_USER_2 = 900_000_002
SYNTHETIC_USER_3 = 900_000_003
SYNTHETIC_GROUP_CHAT_MAGNITUDE = 100_900_000_001
SYNTHETIC_GROUP_CHAT = -SYNTHETIC_GROUP_CHAT_MAGNITUDE


class WhitelistFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / ".env"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, content: bytes) -> None:
        self.path.write_bytes(content)

    def test_parser_reads_one_assignment_without_evaluating_shell(self) -> None:
        self.write(
            b"# keep this\nOTHER=$(touch SHOULD_NOT_EXIST)\n"
            b'TELEGRAM_ALLOWED_USERS="900000002,900000001"\n'
        )

        snapshot = parse_allowed_users(self.path)

        self.assertEqual(snapshot.user_ids, (SYNTHETIC_USER_1, SYNTHETIC_USER_2))
        self.assertEqual(
            snapshot.fingerprint,
            hashlib.sha256(b"900000001,900000002").hexdigest(),
        )
        self.assertFalse((Path.cwd() / "SHOULD_NOT_EXIST").exists())

    def test_parser_rejects_duplicate_wildcard_and_malformed_ids(self) -> None:
        for value in (
            b"1,1",
            b"1,*",
            b"1,-2",
            b"1,,2",
            b"1 2",
            b"0",
            b"01",
        ):
            with self.subTest(value=value):
                self.write(b"TELEGRAM_ALLOWED_USERS=" + value + b"\n")
                with self.assertRaises(PolicyParseError):
                    parse_allowed_users(self.path)

    def test_parser_rejects_duplicate_assignment_and_symlink(self) -> None:
        self.write(b"TELEGRAM_ALLOWED_USERS=1\nTELEGRAM_ALLOWED_USERS=2\n")
        with self.assertRaises(PolicyParseError):
            parse_allowed_users(self.path)

        target = Path(self.temp_dir.name) / "real.env"
        target.write_bytes(b"TELEGRAM_ALLOWED_USERS=1\n")
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(SymlinkRejected):
            parse_allowed_users(self.path)

    def test_add_and_remove_preserve_other_bytes_mode_and_owner(self) -> None:
        self.write(
            b"# heading\r\nOTHER=value\r\n"
            b'TELEGRAM_ALLOWED_USERS="900000002,900000001"\r\n'
            b"TAIL=kept\r\n"
        )
        os.chmod(self.path, 0o640)
        before_stat = self.path.stat()
        before = self.path.read_bytes()
        current = parse_allowed_users(self.path)

        added = add_allowed_user(
            self.path,
            SYNTHETIC_USER_3,
            expected_fingerprint=current.fingerprint,
        )
        self.assertTrue(added.changed)
        self.assertEqual(added.status, "added")
        self.assertEqual(
            parse_allowed_users(self.path).user_ids,
            (SYNTHETIC_USER_1, SYNTHETIC_USER_2, SYNTHETIC_USER_3),
        )
        updated = self.path.read_bytes()
        self.assertIn(b"# heading\r\nOTHER=value\r\n", updated)
        self.assertIn(b"TAIL=kept\r\n", updated)
        self.assertEqual(
            stat.S_IMODE(self.path.stat().st_mode), stat.S_IMODE(before_stat.st_mode)
        )
        self.assertEqual(self.path.stat().st_uid, before_stat.st_uid)
        self.assertEqual(self.path.stat().st_gid, before_stat.st_gid)

        no_op = add_allowed_user(
            self.path,
            SYNTHETIC_USER_3,
            expected_fingerprint=added.fingerprint,
        )
        self.assertFalse(no_op.changed)
        self.assertEqual(no_op.status, "already_present")

        removed = remove_allowed_user(
            self.path,
            SYNTHETIC_USER_3,
            expected_fingerprint=no_op.fingerprint,
        )
        self.assertTrue(removed.changed)
        self.assertEqual(
            parse_allowed_users(self.path).user_ids,
            (SYNTHETIC_USER_1, SYNTHETIC_USER_2),
        )
        self.assertNotEqual(before, self.path.read_bytes())

    def test_stale_fingerprint_and_admin_removal_are_rejected_without_write(
        self,
    ) -> None:
        self.write(b"TELEGRAM_ALLOWED_USERS=1,2\nOTHER=kept\n")
        before = self.path.read_bytes()
        with self.assertRaises(FingerprintMismatch):
            add_allowed_user(self.path, 3, expected_fingerprint="0" * 64)
        self.assertEqual(self.path.read_bytes(), before)

        with self.assertRaises(AdminRemovalDenied):
            remove_allowed_user(self.path, 1, admin_ids=(1,))
        self.assertEqual(self.path.read_bytes(), before)

    def test_semantic_fingerprint_is_order_independent(self) -> None:
        self.assertEqual(semantic_fingerprint((2, 1)), semantic_fingerprint((1, 2)))
        with self.assertRaises(ValueError):
            semantic_fingerprint((1, 1))


class BlockedLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_exact_line_parser_never_returns_raw_message(self) -> None:
        event = extract_blocked_user_event(
            "2026-08-15 12:00:00 INFO gateway.telegram: "
            "Blocked unauthorized user 900000001 in chat -100900000001"
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(
            (event.user_id, event.chat_id),
            (SYNTHETIC_USER_1, SYNTHETIC_GROUP_CHAT),
        )
        self.assertIsNone(event.source)
        self.assertIsNone(
            extract_blocked_user_event(
                "Blocked unauthorized user 900000001 in chat 2 extra message text"
            )
        )

    def test_rotations_are_bounded_and_return_only_typed_events(self) -> None:
        current = self.root / "gateway.log"
        rotated = self.root / "gateway.log.1"
        current.write_text(
            "unrelated message with private text\n"
            "Blocked unauthorized user 900000002 in chat 900000002\n",
            encoding="utf-8",
        )
        rotated.write_text(
            "Blocked unauthorized user 900000001 in chat 900000001\n",
            encoding="utf-8",
        )
        events = parse_blocked_user_logs(
            (current, rotated), max_bytes_per_file=256, max_records=5
        )
        self.assertEqual(
            [(event.user_id, event.chat_id) for event in events],
            [
                (SYNTHETIC_USER_2, SYNTHETIC_USER_2),
                (SYNTHETIC_USER_1, SYNTHETIC_USER_1),
            ],
        )
        self.assertTrue(all(not hasattr(event, "message") for event in events))
        self.assertEqual(parse_blocked_user_logs(current)[0].user_id, SYNTHETIC_USER_2)

    def test_rotated_log_symlink_is_rejected(self) -> None:
        target = self.root / "real.log"
        target.write_text("Blocked unauthorized user 1 in chat 1\n", encoding="utf-8")
        link = self.root / "gateway.log.1"
        link.symlink_to(target)
        with self.assertRaises(SymlinkRejected):
            parse_blocked_user_logs((link,))


if __name__ == "__main__":
    unittest.main()
