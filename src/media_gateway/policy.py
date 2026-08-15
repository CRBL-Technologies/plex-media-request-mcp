"""Bounded editor for Hermes' Telegram user policy."""

from __future__ import annotations

import fcntl
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from .types import Role

_ASSIGNMENT = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>TELEGRAM_(?:ALLOWED|ADMIN)_USERS)"
    r"(?P<equals>\s*=\s*)(?P<value>.*?)(?P<newline>\r?\n?)$"
)
_ID = re.compile(r"[1-9][0-9]*")


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    allowed: frozenset[int]
    admins: frozenset[int]

    def role(self, user_id: int) -> Role:
        if user_id in self.admins:
            return Role.ADMIN
        if user_id in self.allowed:
            return Role.USER
        return Role.BLOCKED


def _ids(value: str) -> frozenset[int]:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    if not value:
        return frozenset()
    tokens = [token.strip() for token in value.split(",")]
    if any(_ID.fullmatch(token) is None for token in tokens):
        raise ValueError("Telegram policy contains an invalid user ID")
    return frozenset(int(token) for token in tokens)


def _read(path: Path) -> tuple[str, PolicySnapshot]:
    if path.is_symlink():
        raise ValueError("Telegram policy file cannot be a symlink")
    text = path.read_text(encoding="utf-8")
    if len(text.encode()) > 1024 * 1024:
        raise ValueError("Telegram policy file is unexpectedly large")
    values: dict[str, frozenset[int]] = {}
    for line in text.splitlines(keepends=True):
        match = _ASSIGNMENT.fullmatch(line)
        if not match:
            continue
        key = match.group("key")
        if key in values:
            raise ValueError(f"duplicate {key} assignment")
        values[key] = _ids(match.group("value"))
    allowed = values.get("TELEGRAM_ALLOWED_USERS", frozenset())
    admins = values.get("TELEGRAM_ADMIN_USERS", frozenset())
    return text, PolicySnapshot(allowed=allowed | admins, admins=admins)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class Policy:
    def __init__(self, path: Path):
        self.path = path

    def snapshot(self) -> PolicySnapshot:
        return _read(self.path)[1]

    def set_allowed(self, user_id: int, *, allowed: bool) -> PolicySnapshot:
        if user_id <= 0:
            raise ValueError("user ID must be positive")
        with _locked(self.path):
            text, snapshot = _read(self.path)
            if not allowed and user_id in snapshot.admins:
                raise ValueError("administrators cannot be removed from the allowlist")
            regular = set(snapshot.allowed - snapshot.admins)
            if allowed:
                regular.add(user_id)
            else:
                regular.discard(user_id)
            replacement = ",".join(str(item) for item in sorted(regular))
            lines = text.splitlines(keepends=True)
            found = False
            for index, line in enumerate(lines):
                match = _ASSIGNMENT.fullmatch(line)
                if not match or match.group("key") != "TELEGRAM_ALLOWED_USERS":
                    continue
                quote = ""
                old = match.group("value").strip()
                if len(old) >= 2 and old[0] == old[-1] and old[0] in "\"'":
                    quote = old[0]
                lines[index] = (
                    f"{match.group('prefix')}TELEGRAM_ALLOWED_USERS{match.group('equals')}"
                    f"{quote}{replacement}{quote}{match.group('newline')}"
                )
                found = True
                break
            if not found:
                if text and not text.endswith(("\n", "\r")):
                    lines.append("\n")
                lines.append(f"TELEGRAM_ALLOWED_USERS={replacement}\n")
            self._atomic_write("".join(lines))
            return _read(self.path)[1]

    def _atomic_write(self, text: str) -> None:
        current = self.path.stat()
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            os.fchmod(descriptor, current.st_mode & 0o777)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
