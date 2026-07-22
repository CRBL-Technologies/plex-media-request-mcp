#!/usr/bin/env python3
"""Reject runtime data and literal requester identities in the public tree."""

from __future__ import annotations

import ast
import fnmatch
import subprocess
import sys
from pathlib import Path


REQUESTER_FIELDS = {
    "requested_by_chat_id",
    "requested_by_user_id",
    "requested_by_username",
}
BLOCKED_PATTERNS = (
    "*.env",
    "*.sqlite",
    "*.sqlite-*",
    "*.sqlite3",
    "*.sqlite3-*",
    "*.db",
    "*.db-*",
    "*.log",
)
BLOCKED_DIRECTORIES = {"data", "state"}
MIN_PRIVATE_ID = 100_000_000


def tracked_files(root: Path) -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=root, stderr=subprocess.DEVNULL
    )
    return [root / name.decode() for name in output.split(b"\0") if name]


def is_blocked_path(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if relative.name == ".env.example":
        return False
    if any(part in BLOCKED_DIRECTORIES for part in relative.parts[:-1]):
        return True
    return any(fnmatch.fnmatch(relative.name, pattern) for pattern in BLOCKED_PATTERNS)


def assignment_names(node: ast.AST) -> list[str]:
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        return []
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [target.id for target in targets if isinstance(target, ast.Name)]


def is_synthetic_expression(node: ast.AST, assignments: dict[str, ast.AST]) -> bool:
    if isinstance(node, ast.Name):
        if node.id.startswith("SYNTHETIC_"):
            return True
        assigned = assignments.get(node.id)
        return assigned is not None and is_synthetic_expression(assigned, assignments)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return is_synthetic_expression(node.operand, assignments)
    return False


def scan_fixture(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    assignments: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if value is not None:
            assignments.update({name: value for name in assignment_names(node)})

    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in REQUESTER_FIELDS:
            if not is_synthetic_expression(node.value, assignments):
                errors.append(
                    f"{path.name}:{node.lineno}: {node.arg} must use a SYNTHETIC_* fixture"
                )
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int)
            and not isinstance(node.value, bool)
            and abs(node.value) >= MIN_PRIVATE_ID
        ):
            parent = parents.get(node)
            names = assignment_names(parent) if parent is not None else []
            if not names or not all(name.startswith("SYNTHETIC_") for name in names):
                errors.append(
                    f"{path.name}:{node.lineno}: large fixture IDs must use a SYNTHETIC_* constant"
                )
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    files = tracked_files(root)
    errors = [
        f"{path.relative_to(root)}: runtime or private data must not be tracked"
        for path in files
        if is_blocked_path(path, root)
    ]
    for path in files:
        if path.name.startswith("test") and path.suffix == ".py":
            errors.extend(scan_fixture(path))
    if errors:
        print("Public-repository policy failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Public-repository policy passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
