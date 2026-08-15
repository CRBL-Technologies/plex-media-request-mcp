"""No-loss oracle view adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import operation_result


def render(data: Mapping[str, Any], *, actor: str | None = None) -> str:
    return operation_result("oracle", data, actor=actor)


__all__ = ["render"]
