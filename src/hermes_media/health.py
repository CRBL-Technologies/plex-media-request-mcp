"""Container health requires a live gateway with its Telegram adapter connected."""

from .compat import gateway_healthy

if __name__ == "__main__":
    raise SystemExit(0 if gateway_healthy() else 1)
