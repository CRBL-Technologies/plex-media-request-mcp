"""Strict secret and dotenv readers; no shell evaluation."""

from __future__ import annotations

from pathlib import Path


def read_secret(path: Path, *, minimum: int = 1) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < minimum:
        raise ValueError(f"secret file is too short: {path.name}")
    return value


def read_dotenv(path: Path, keys: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid dotenv assignment on line {number}")
        name, value = line.split("=", 1)
        name = name.removeprefix("export ").strip()
        if name not in keys:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name in result:
            raise ValueError(f"duplicate dotenv assignment: {name}")
        result[name] = value
    return result
