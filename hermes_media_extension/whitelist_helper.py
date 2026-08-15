"""Safe, narrow helpers for Hermes' Telegram allowlist and gateway log.

This module intentionally has no shell, network, or logging dependencies.  It
parses the one ``TELEGRAM_ALLOWED_USERS`` assignment itself, and changes only
that assignment after taking a stable sibling lock.  Log helpers return typed
IDs and never return a log line, message text, or an unparsed payload.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
import os
import re
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


ALLOWLIST_VARIABLE = "TELEGRAM_ALLOWED_USERS"

# These bounds are deliberately finite.  A Hermes .env is tiny, while a live
# gateway log can be large and can be rotated while it is being inspected.
MAX_POLICY_FILE_BYTES = 1024 * 1024
DEFAULT_MAX_LOG_BYTES = 1024 * 1024
DEFAULT_MAX_LOG_LINE_BYTES = 16 * 1024
DEFAULT_MAX_LOG_RECORDS = 10_000

# Linux ``flock`` arbitration is process-scoped in ways that can let two
# threads in one process pass through a lock/unlock pair unexpectedly.  Keep a
# local guard as well; the file lock still protects separate processes.
_MUTATION_LOCK = threading.RLock()


class PolicyFileError(Exception):
    """Base error for unreadable or unsafe Hermes policy files."""


class PolicyParseError(PolicyFileError, ValueError):
    """The policy file does not contain one valid allowlist assignment."""


class SymlinkRejected(PolicyFileError):
    """A policy or log path (or one of its parent components) is a symlink."""


class FingerprintMismatch(PolicyFileError, ValueError):
    """The caller's optimistic-concurrency fingerprint is stale."""


class AdminRemovalDenied(PolicyFileError, ValueError):
    """A mutation attempted to remove a configured administrator."""


class LogInputError(PolicyFileError, ValueError):
    """A rotated log is missing, unsafe, or outside parser bounds."""


# Descriptive aliases used by callers that call this an allowlist rather than
# a policy file.  They retain one exception hierarchy and do not expose input
# values.
AllowlistError = PolicyFileError
AllowlistParseError = PolicyParseError
StaleFingerprintError = FingerprintMismatch


@dataclass(frozen=True, slots=True)
class AllowlistSnapshot:
    """The semantic view of one valid ``TELEGRAM_ALLOWED_USERS`` assignment.

    ``user_ids`` is a sorted tuple of positive Python integers.  The bytes of
    the source file are intentionally not included in this object.
    """

    user_ids: tuple[int, ...]
    fingerprint: str

    @property
    def ids(self) -> tuple[int, ...]:
        """Compatibility spelling for callers that use ``ids``."""

        return self.user_ids

    @property
    def semantic_fingerprint(self) -> str:
        return self.fingerprint


@dataclass(frozen=True, slots=True)
class MutationResult:
    """Safe outcome of an allowlist add/remove operation."""

    operation: str
    user_id: int
    changed: bool
    status: str
    snapshot: AllowlistSnapshot

    @property
    def fingerprint(self) -> str:
        return self.snapshot.fingerprint

    @property
    def user_ids(self) -> tuple[int, ...]:
        return self.snapshot.user_ids

    @property
    def ids(self) -> tuple[int, ...]:
        return self.snapshot.user_ids


@dataclass(frozen=True, slots=True)
class BlockedUserEvent:
    """A sanitized unauthorized-user event extracted from a gateway log.

    Only numeric IDs and the source rotation's basename are exposed.  No raw
    line or user message is retained.  ``source`` is optional so callers that
    do not want filesystem metadata can omit it with ``include_source=False``.
    """

    user_id: int
    chat_id: int
    source: str | None = None

    @property
    def id(self) -> int:
        """Alias for the unauthorized sender's numeric ID."""

        return self.user_id

    @property
    def sender_id(self) -> int:
        return self.user_id


@dataclass(frozen=True, slots=True)
class _Assignment:
    """Byte offsets and quoting details for the one policy assignment."""

    line_start: int
    value_start: int
    value_end: int
    line_end: int
    quote: int | None
    newline: bytes


@dataclass(frozen=True, slots=True)
class _PolicyRead:
    data: bytes
    file_stat: os.stat_result
    assignment: _Assignment
    snapshot: AllowlistSnapshot


_ASSIGNMENT_RE = re.compile(
    rb"^[ \t]*(?:export[ \t]+)?TELEGRAM_ALLOWED_USERS[ \t]*=[ \t]*(.*?)[ \t]*(\r?\n|$)$"
)
_ASSIGNMENT_KEY_RE = re.compile(rb"^[ \t]*(?:export[ \t]+)?TELEGRAM_ALLOWED_USERS\b")
_ID_RE = re.compile(r"[1-9][0-9]*\Z")
# Hermes' normal file formatter ends the line with ``logger.name: message``;
# plain lines are accepted too for fixtures and alternate handlers.  The
# prefix is bounded, and only the exact message suffix is parsed.
_BLOCKED_LINE_RE = re.compile(
    rb"^(?:(?:[^\r\n]{0,1024}\b(?:gateway|hermes_plugins)"
    rb"[^\r\n]{0,256}: ))?"
    rb"Blocked unauthorized user ([1-9][0-9]*) in chat (-?[1-9][0-9]*)[ \t]*$"
)


def _path(path: str | os.PathLike[str]) -> Path:
    value = Path(path)
    if not value.is_absolute():
        value = Path.cwd() / value
    return value


def _reject_symlink_components(path: Path) -> None:
    """Reject symlinks before any open/rename operation.

    ``O_NOFOLLOW`` protects the final component on POSIX; checking every
    existing component also prevents a symlinked directory from redirecting a
    caller to a different policy or log tree.  Windows has no ``O_NOFOLLOW``
    equivalent, but the lstat walk still provides the same useful check.
    """

    absolute = path.absolute()
    current = Path(absolute.anchor or os.sep)
    for component in absolute.parts[1:]:
        current /= component
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            # The remaining components cannot exist if this one does not;
            # the eventual open/create operation will report the real error.
            break
        except OSError as exc:
            raise PolicyFileError("unable to inspect policy path") from exc
        if stat.S_ISLNK(mode):
            raise SymlinkRejected("symlink paths are not accepted")


def _open_regular(path: Path, flags: int, mode: int = 0o600) -> int:
    _reject_symlink_components(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | nofollow, mode)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SymlinkRejected("symlink paths are not accepted") from exc
        if isinstance(exc, FileNotFoundError):
            raise
        raise PolicyFileError("unable to open policy path") from exc
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode):
            raise PolicyFileError("policy path is not a regular file")
        return fd
    except Exception:
        os.close(fd)
        raise


def _canonical_id(value: object, *, allow_negative: bool = False) -> int:
    if isinstance(value, bool):
        raise ValueError("numeric ID required")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        token = value.strip()
        pattern = r"-?[1-9][0-9]*\Z" if allow_negative else r"[1-9][0-9]*\Z"
        if re.fullmatch(pattern, token) is None:
            raise ValueError("numeric ID required")
        result = int(token)
    else:
        raise ValueError("numeric ID required")
    if result <= 0 and not (allow_negative and result < 0):
        raise ValueError("numeric ID must be positive")
    return result


def _parse_id_value(raw: bytes) -> tuple[tuple[int, ...], int | None]:
    """Parse assignment bytes and return canonical IDs plus quote style."""

    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise PolicyParseError("allowlist contains non-ASCII data") from exc

    stripped = text.strip()
    quote: int | None = None
    if stripped.startswith(("'", '"')):
        quote = ord(stripped[0])
        if len(stripped) < 2 or stripped[-1] != stripped[0]:
            raise PolicyParseError("malformed allowlist value")
        stripped = stripped[1:-1]
        if stripped.startswith(("'", '"')) or stripped.endswith(("'", '"')):
            raise PolicyParseError("malformed allowlist value")
    elif stripped.endswith(("'", '"')):
        raise PolicyParseError("malformed allowlist value")

    # Empty is a valid fail-closed allowlist (and is needed when removing the
    # last regular user).  No shell expansions, comments, or escaped values
    # are interpreted here.
    if not stripped:
        return (), quote
    if any(character in stripped for character in "*?[];$`\\\n\r"):
        raise PolicyParseError("malformed allowlist value")

    tokens = stripped.split(",")
    ids: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        token = token.strip(" \t")
        if not token or _ID_RE.fullmatch(token) is None:
            raise PolicyParseError("malformed allowlist ID")
        try:
            value = int(token)
        except ValueError as exc:
            raise PolicyParseError("malformed allowlist ID") from exc
        if value <= 0:
            raise PolicyParseError("allowlist IDs must be positive")
        if value in seen:
            raise PolicyParseError("duplicate allowlist ID")
        seen.add(value)
        ids.append(value)
    return tuple(sorted(ids)), quote


def _assignment_and_snapshot(data: bytes) -> tuple[_Assignment, AllowlistSnapshot]:
    if len(data) > MAX_POLICY_FILE_BYTES:
        raise PolicyParseError("policy file exceeds configured bound")

    matches: list[_Assignment] = []
    offset = 0
    for line in data.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        match = _ASSIGNMENT_RE.fullmatch(line)
        if match is None:
            # Do not silently ignore a second/conflicting spelling such as
            # ``TELEGRAM_ALLOWED_USERS+=...`` when one valid assignment is
            # also present.  The file must have exactly one unambiguous key.
            if _ASSIGNMENT_KEY_RE.match(line) is not None:
                raise PolicyParseError("malformed TELEGRAM_ALLOWED_USERS assignment")
            continue
        value = match.group(1)
        newline = match.group(2)
        # The regex's non-greedy value includes the optional spaces before the
        # line ending.  Keep those spaces as part of the assignment value; a
        # replacement may normalize them, but never touches other lines.
        value_end = line_start + match.start(1) + len(value)
        value_start = line_start + match.start(1)
        # Match group 1 can contain trailing spaces consumed by the regex's
        # backtracking.  Strip those spaces from the replacement span while
        # retaining them in the unchanged line around it.
        while value_end > value_start and data[value_end - 1 : value_end] in (
            b" ",
            b"\t",
        ):
            value_end -= 1
        line_end = line_start + len(line)
        matches.append(
            _Assignment(
                line_start=line_start,
                value_start=value_start,
                value_end=value_end,
                line_end=line_end,
                quote=None,
                newline=newline,
            )
        )

    if len(matches) != 1:
        raise PolicyParseError(
            "policy must contain one TELEGRAM_ALLOWED_USERS assignment"
        )

    assignment = matches[0]
    raw_value = data[assignment.value_start : assignment.value_end]
    ids, quote = _parse_id_value(raw_value)
    assignment = _Assignment(
        line_start=assignment.line_start,
        value_start=assignment.value_start,
        value_end=assignment.value_end,
        line_end=assignment.line_end,
        quote=quote,
        newline=assignment.newline,
    )
    snapshot = AllowlistSnapshot(ids, semantic_fingerprint(ids))
    return assignment, snapshot


def _read_policy(path: Path) -> _PolicyRead:
    fd = _open_regular(path, os.O_RDONLY)
    try:
        file_stat = os.fstat(fd)
        chunks: list[bytes] = []
        remaining = MAX_POLICY_FILE_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    except OSError as exc:
        raise PolicyFileError("unable to read policy file") from exc
    finally:
        os.close(fd)
    assignment, snapshot = _assignment_and_snapshot(data)
    return _PolicyRead(data, file_stat, assignment, snapshot)


def semantic_fingerprint(user_ids: Iterable[object]) -> str:
    """Return a stable SHA-256 fingerprint of the semantic ID set.

    Ordering and input whitespace do not affect the result.  Invalid values,
    duplicates, wildcards, and non-positive IDs are rejected rather than being
    silently folded into a potentially surprising policy.
    """

    canonical: list[int] = []
    seen: set[int] = set()
    try:
        iterator = iter(user_ids)
    except TypeError as exc:
        raise ValueError("allowlist IDs must be iterable") from exc
    for value in iterator:
        try:
            number = _canonical_id(value)
        except ValueError as exc:
            raise ValueError("allowlist IDs must be positive integers") from exc
        if number in seen:
            raise ValueError("duplicate allowlist ID")
        seen.add(number)
        canonical.append(number)
    payload = ",".join(str(value) for value in sorted(canonical)).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


allowlist_fingerprint = semantic_fingerprint


def parse_allowed_users(path: str | os.PathLike[str]) -> AllowlistSnapshot:
    """Read and validate one Hermes ``.env`` allowlist assignment."""

    return _read_policy(_path(path)).snapshot


def _snapshot_from_ids(ids: Iterable[int]) -> AllowlistSnapshot:
    canonical = tuple(sorted(ids))
    return AllowlistSnapshot(canonical, semantic_fingerprint(canonical))


def _validate_expected(current: str, expected: str | None) -> None:
    if expected is None:
        return
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise FingerprintMismatch("allowlist fingerprint is stale")
    if not hmac.compare_digest(current, expected):
        raise FingerprintMismatch("allowlist fingerprint is stale")


def _lock_path(policy_path: Path) -> Path:
    return policy_path.with_name(f".{policy_path.name}.lock")


def _acquire_lock(policy_path: Path) -> tuple[int, Path]:
    lock_path = _lock_path(policy_path)
    # The lock is deliberately stable across atomic replacement of the target
    # inode.  It is a sibling rather than a lock on the target itself so a
    # second process cannot observe a newly renamed inode and bypass it.
    _reject_symlink_components(lock_path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | nofollow, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SymlinkRejected("symlink paths are not accepted") from exc
        raise PolicyFileError("unable to open policy lock") from exc
    try:
        lock_stat = os.fstat(fd)
        if not stat.S_ISREG(lock_stat.st_mode):
            raise PolicyFileError("policy lock is not a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
    except Exception:
        os.close(fd)
        raise
    return fd, lock_path


def _render_assignment(policy: _PolicyRead, ids: tuple[int, ...]) -> bytes:
    assignment = policy.assignment
    rendered = ",".join(str(value) for value in ids).encode("ascii")
    if assignment.quote is not None:
        quote = bytes((assignment.quote,))
        rendered = quote + rendered + quote
    return (
        policy.data[: assignment.value_start]
        + rendered
        + policy.data[assignment.value_end :]
    )


def _write_atomic(policy_path: Path, policy: _PolicyRead, replacement: bytes) -> None:
    parent = policy_path.parent
    _reject_symlink_components(parent)
    temp_fd: int | None = None
    temp_name: str | None = None
    original_stat = policy.file_stat
    try:
        temp_fd, temp_name = tempfile.mkstemp(
            prefix=f".{policy_path.name}.", suffix=".tmp", dir=parent
        )
        # mkstemp uses O_EXCL, and the parent was checked above.  Preserve the
        # exact target permission bits and owner/group before any bytes land.
        os.fchmod(temp_fd, stat.S_IMODE(original_stat.st_mode))
        try:
            os.fchown(temp_fd, original_stat.st_uid, original_stat.st_gid)
        except OSError as exc:
            raise PolicyFileError("unable to preserve policy ownership") from exc
        view = memoryview(replacement)
        while view:
            written = os.write(temp_fd, view)
            if written <= 0:
                raise PolicyFileError("unable to write policy file")
            view = view[written:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None

        # Do not replace a path that changed behind the lock.  The lock covers
        # cooperating writers; this identity check protects against a manual
        # edit or replacement racing the helper.
        current_stat = os.lstat(policy_path)
        if (
            stat.S_ISLNK(current_stat.st_mode)
            or current_stat.st_dev != original_stat.st_dev
            or current_stat.st_ino != original_stat.st_ino
            or current_stat.st_size != original_stat.st_size
            or current_stat.st_mtime_ns != original_stat.st_mtime_ns
            or current_stat.st_ctime_ns != original_stat.st_ctime_ns
        ):
            raise PolicyFileError("policy file changed during update")
        os.replace(temp_name, policy_path)
        temp_name = None

        dir_fd = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        if isinstance(exc, PolicyFileError):
            raise
        raise PolicyFileError("atomic policy update failed") from exc
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def mutate_allowlist(
    path: str | os.PathLike[str],
    user_id: object,
    *,
    operation: str,
    expected_fingerprint: str | None = None,
    admin_ids: Iterable[object] | None = (),
) -> MutationResult:
    """Atomically add or remove one user from the Hermes allowlist.

    The file is parsed again while holding a stable sibling lock.  A supplied
    fingerprint is a compare-and-swap guard.  Repeating an operation against
    the same current fingerprint is an idempotent no-op; a stale fingerprint
    is always rejected before any write.  Administrators cannot be removed.
    """

    if operation not in {"add", "remove"}:
        raise ValueError("operation must be add or remove")
    try:
        target = _canonical_id(user_id)
    except ValueError as exc:
        raise ValueError("allowlist user ID must be a positive integer") from exc
    if admin_ids is None:
        admin_ids = ()
    try:
        admins = {_canonical_id(value) for value in admin_ids}
    except ValueError as exc:
        raise ValueError("admin IDs must be positive integers") from exc
    policy_path = _path(path)
    # Check the target path before policy decisions so an unsafe symlink never
    # becomes an administrator-removal side channel.
    _reject_symlink_components(policy_path)
    if operation == "remove" and target in admins:
        raise AdminRemovalDenied("configured administrator cannot be removed")

    with _MUTATION_LOCK:
        lock_fd, _ = _acquire_lock(policy_path)
        try:
            current = _read_policy(policy_path)
            _validate_expected(current.snapshot.fingerprint, expected_fingerprint)
            present = target in current.snapshot.user_ids
            if operation == "add":
                if present:
                    return MutationResult(
                        "add", target, False, "already_present", current.snapshot
                    )
                updated_ids = tuple(sorted((*current.snapshot.user_ids, target)))
                status = "added"
            else:
                if not present:
                    return MutationResult(
                        "remove", target, False, "not_present", current.snapshot
                    )
                updated_ids = tuple(
                    value for value in current.snapshot.user_ids if value != target
                )
                status = "removed"

            updated = _snapshot_from_ids(updated_ids)
            replacement = _render_assignment(current, updated.user_ids)
            # Validate the exact post-edit semantics before touching the target.
            assignment, reparsed = _assignment_and_snapshot(replacement)
            del assignment
            if reparsed != updated:
                raise PolicyFileError("policy update validation failed")
            if operation == "add" and (
                len(updated.user_ids) != len(current.snapshot.user_ids) + 1
                or target not in updated.user_ids
            ):
                raise PolicyFileError("policy update validation failed")
            if operation == "remove" and (
                len(updated.user_ids) != len(current.snapshot.user_ids) - 1
                or target in updated.user_ids
            ):
                raise PolicyFileError("policy update validation failed")
            _write_atomic(policy_path, current, replacement)
            return MutationResult(operation, target, True, status, updated)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


def add_allowed_user(
    path: str | os.PathLike[str],
    user_id: object,
    *,
    expected_fingerprint: str | None = None,
    admin_ids: Iterable[object] | None = (),
) -> MutationResult:
    """Idempotently add one positive Telegram user ID."""

    return mutate_allowlist(
        path,
        user_id,
        operation="add",
        expected_fingerprint=expected_fingerprint,
        admin_ids=admin_ids,
    )


def remove_allowed_user(
    path: str | os.PathLike[str],
    user_id: object,
    *,
    expected_fingerprint: str | None = None,
    admin_ids: Iterable[object] | None = (),
) -> MutationResult:
    """Idempotently remove one regular Telegram user ID."""

    return mutate_allowlist(
        path,
        user_id,
        operation="remove",
        expected_fingerprint=expected_fingerprint,
        admin_ids=admin_ids,
    )


def extract_blocked_user_event(
    line: str | bytes,
    *,
    source: str | os.PathLike[str] | None = None,
) -> BlockedUserEvent | None:
    """Parse one exact sanitized blocked-user log line.

    The optional prefix matches Hermes' normal ``logger: message`` format but
    is never returned.  A line with extra text after the message, malformed
    IDs, wildcard values, or an embedded message does not produce an event.
    """

    if isinstance(line, str):
        try:
            data = line.encode("ascii")
        except UnicodeEncodeError:
            return None
    else:
        data = line
    if len(data) > DEFAULT_MAX_LOG_LINE_BYTES:
        return None
    if any(byte > 0x7F for byte in data):
        return None
    data = data.rstrip(b"\r\n")
    match = _BLOCKED_LINE_RE.fullmatch(data)
    if match is None:
        return None
    try:
        user_id = _canonical_id(match.group(1).decode("ascii"))
        chat_id = _canonical_id(match.group(2).decode("ascii"), allow_negative=True)
    except (UnicodeDecodeError, ValueError):
        return None
    sanitized_source = None
    if source is not None:
        sanitized_source = Path(source).name
    return BlockedUserEvent(user_id, chat_id, sanitized_source)


def _bounded_file_lines(
    path: Path,
    *,
    max_bytes: int,
    max_line_bytes: int,
) -> Iterator[bytes]:
    if max_bytes <= 0 or max_line_bytes <= 0:
        raise ValueError("log bounds must be positive")
    fd = _open_regular(path, os.O_RDONLY)
    try:
        file_stat = os.fstat(fd)
        if file_stat.st_size > max_bytes:
            # Reading the tail keeps this helper useful for a live rotating
            # gateway log while keeping memory and work bounded.  Discard the
            # partial first line because it is not a complete record.
            os.lseek(fd, file_stat.st_size - max_bytes, os.SEEK_SET)
            data = os.read(fd, max_bytes)
            first_newline = data.find(b"\n")
            if first_newline < 0:
                return
            data = data[first_newline + 1 :]
        else:
            chunks: list[bytes] = []
            remaining = max_bytes
            while remaining:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
    except OSError as exc:
        raise LogInputError("unable to read gateway log") from exc
    finally:
        os.close(fd)

    for line in data.splitlines():
        if len(line) > max_line_bytes:
            continue
        yield line


def parse_blocked_user_logs(
    paths: Iterable[str | os.PathLike[str]] | str | os.PathLike[str],
    *,
    max_bytes_per_file: int = DEFAULT_MAX_LOG_BYTES,
    max_line_bytes: int = DEFAULT_MAX_LOG_LINE_BYTES,
    max_records: int = DEFAULT_MAX_LOG_RECORDS,
    include_source: bool = False,
    max_bytes: int | None = None,
    max_line_length: int | None = None,
    limit: int | None = None,
) -> tuple[BlockedUserEvent, ...]:
    """Extract exact blocked-user events from configured log rotations.

    Missing rotations are normal and skipped.  Existing paths are opened
    without following symlinks.  At most the configured tail of each file and
    ``max_records`` sanitized events are returned; raw lines are never stored.
    """

    if max_records <= 0:
        raise ValueError("max_records must be positive")
    if max_bytes is not None:
        max_bytes_per_file = max_bytes
    if max_line_length is not None:
        max_line_bytes = max_line_length
    if limit is not None:
        max_records = limit
        if max_records <= 0:
            raise ValueError("limit must be positive")
    if isinstance(paths, (str, os.PathLike)):
        paths = (paths,)
    events: list[BlockedUserEvent] = []
    for raw_path in paths:
        path = _path(raw_path)
        try:
            lines = _bounded_file_lines(
                path, max_bytes=max_bytes_per_file, max_line_bytes=max_line_bytes
            )
            for line in lines:
                event = extract_blocked_user_event(
                    line, source=path if include_source else None
                )
                if event is None:
                    continue
                events.append(event)
                if len(events) >= max_records:
                    return tuple(events)
        except FileNotFoundError:
            continue
    return tuple(events)


# A few descriptive aliases keep callers from needing to know whether the
# source is called an allowlist or whitelist.  They all share the same strict
# implementation above.
parse_whitelist = parse_allowed_users
read_allowlist = parse_allowed_users
read_telegram_allowlist = parse_allowed_users
parse_blocked_logs = parse_blocked_user_logs
parse_blocked_log = parse_blocked_user_logs
parse_blocked_gateway_logs = parse_blocked_user_logs
parse_blocked_user_log = extract_blocked_user_event


__all__ = [
    "ALLOWLIST_VARIABLE",
    "DEFAULT_MAX_LOG_BYTES",
    "DEFAULT_MAX_LOG_LINE_BYTES",
    "DEFAULT_MAX_LOG_RECORDS",
    "MAX_POLICY_FILE_BYTES",
    "AdminRemovalDenied",
    "AllowlistError",
    "AllowlistParseError",
    "AllowlistSnapshot",
    "BlockedUserEvent",
    "FingerprintMismatch",
    "LogInputError",
    "MutationResult",
    "PolicyFileError",
    "PolicyParseError",
    "SymlinkRejected",
    "StaleFingerprintError",
    "add_allowed_user",
    "allowlist_fingerprint",
    "extract_blocked_user_event",
    "mutate_allowlist",
    "parse_allowed_users",
    "parse_blocked_logs",
    "parse_blocked_gateway_logs",
    "parse_blocked_log",
    "parse_blocked_user_log",
    "parse_blocked_user_logs",
    "parse_whitelist",
    "read_allowlist",
    "read_telegram_allowlist",
    "remove_allowed_user",
    "semantic_fingerprint",
]
