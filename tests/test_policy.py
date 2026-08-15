from __future__ import annotations

from pathlib import Path

import pytest

from media_gateway.policy import Policy
from media_gateway.types import Role


def test_policy_reads_roles_and_preserves_unrelated_values(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "SECRET=untouched\nTELEGRAM_ALLOWED_USERS='10,20'\nTELEGRAM_ADMIN_USERS=99\n",
        encoding="utf-8",
    )
    policy = Policy(path)
    assert policy.snapshot().role(10) is Role.USER
    assert policy.snapshot().role(99) is Role.ADMIN
    assert policy.snapshot().role(30) is Role.BLOCKED

    policy.set_allowed(30, allowed=True)
    assert "SECRET=untouched" in path.read_text(encoding="utf-8")
    assert "TELEGRAM_ALLOWED_USERS='10,20,30'" in path.read_text(encoding="utf-8")
    policy.set_allowed(20, allowed=False)
    assert policy.snapshot().allowed == frozenset({10, 30, 99})


def test_policy_never_removes_admin(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("TELEGRAM_ADMIN_USERS=99\n", encoding="utf-8")
    with pytest.raises(ValueError, match="administrators"):
        Policy(path).set_allowed(99, allowed=False)


def test_policy_rejects_duplicate_or_malformed_assignments(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("TELEGRAM_ALLOWED_USERS=10\nTELEGRAM_ALLOWED_USERS=20\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        Policy(path).snapshot()
    path.write_text("TELEGRAM_ALLOWED_USERS=10,$USER\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid"):
        Policy(path).snapshot()
