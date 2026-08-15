"""Process entry point for the authenticated media operations dashboard."""

from __future__ import annotations

from .app import DashboardApp, DashboardConfig, DashboardConfigurationError
from .companion import CompanionClientError


def main() -> int:
    try:
        config = DashboardConfig.from_env()
        DashboardApp(config).serve_forever()
    except (DashboardConfigurationError, CompanionClientError):
        # Startup diagnostics intentionally do not include secret paths, hash
        # values, URLs, or exception text.  Supervisors receive a non-zero exit
        # and can inspect their bounded service status separately.
        return 78
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by process launchers
    raise SystemExit(main())
