"""Runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .secrets import read_dotenv

CONFIG_KEYS = {
    "MEDIA_GATEWAY_HOST",
    "MEDIA_GATEWAY_PORT",
    "MEDIA_GATEWAY_DB_PATH",
    "MEDIA_GATEWAY_POLICY_FILE",
    "MEDIA_GATEWAY_TOKEN_FILE",
    "MEDIA_GATEWAY_UPSTREAM_URL",
    "MEDIA_GATEWAY_UPSTREAM_TOKEN_FILE",
    "MEDIA_GATEWAY_DASHBOARD_PASSWORD_HASH_FILE",
    "MEDIA_GATEWAY_DASHBOARD_SESSION_KEY_FILE",
    "MEDIA_GATEWAY_PLEX_WEBHOOK_TOKEN_FILE",
    "MEDIA_GATEWAY_SECURE_COOKIES",
    "MEDIA_GATEWAY_RADARR_PROFILE_ID",
    "MEDIA_GATEWAY_RADARR_ROOT",
    "MEDIA_GATEWAY_RADARR_TAG_IDS",
    "MEDIA_GATEWAY_SONARR_PROFILE_ID",
    "MEDIA_GATEWAY_SONARR_ANIME_PROFILE_ID",
    "MEDIA_GATEWAY_SONARR_ROOT",
    "MEDIA_GATEWAY_SONARR_TAG_IDS",
    "MEDIA_GATEWAY_PLEX_MACHINE_ID",
    "MEDIA_GATEWAY_NOTIFICATION_DELAY_SECONDS",
    "MEDIA_GATEWAY_TELEGRAM_IDENTITY_SYNC",
}


def _int(name: str, default: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None and default is not None:
        return default
    if raw is None:
        raise ValueError(f"missing {name}")
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _tags(name: str) -> tuple[int, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return ()
    values = tuple(int(item.strip()) for item in raw.split(","))
    if any(item <= 0 for item in values):
        raise ValueError(f"{name} must contain positive IDs")
    return values


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return normalized == "true"


@dataclass(frozen=True, slots=True)
class Config:
    host: str
    port: int
    db_path: Path
    policy_file: Path
    gateway_token_file: Path
    upstream_url: str
    upstream_token_file: Path
    dashboard_password_hash_file: Path
    dashboard_session_key_file: Path
    plex_webhook_token_file: Path
    secure_cookies: bool
    radarr_profile_id: int
    radarr_root: str
    radarr_tags: tuple[int, ...]
    sonarr_profile_id: int
    sonarr_anime_profile_id: int
    sonarr_root: str
    sonarr_tags: tuple[int, ...]
    plex_machine_id: str
    notification_delay_seconds: int
    telegram_identity_sync: bool

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            # Deployment opts into a LAN bind explicitly. An incomplete local
            # configuration remains loopback-only.
            host=os.getenv("MEDIA_GATEWAY_HOST", "127.0.0.1"),
            port=_int("MEDIA_GATEWAY_PORT", 18082),
            db_path=Path(os.getenv("MEDIA_GATEWAY_DB_PATH", "/opt/data/state/gateway.sqlite3")),
            policy_file=Path(os.getenv("MEDIA_GATEWAY_POLICY_FILE", "/opt/data/.env")),
            gateway_token_file=Path(
                os.getenv("MEDIA_GATEWAY_TOKEN_FILE", "/run/media-secrets/gateway.key")
            ),
            upstream_url=os.getenv(
                "MEDIA_GATEWAY_UPSTREAM_URL", "http://media-server-mcp:3000"
            ).rstrip("/"),
            upstream_token_file=Path(
                os.getenv("MEDIA_GATEWAY_UPSTREAM_TOKEN_FILE", "/run/media-secrets/upstream.env")
            ),
            dashboard_password_hash_file=Path(
                os.getenv(
                    "MEDIA_GATEWAY_DASHBOARD_PASSWORD_HASH_FILE",
                    "/run/media-secrets/dashboard-password.hash",
                )
            ),
            dashboard_session_key_file=Path(
                os.getenv(
                    "MEDIA_GATEWAY_DASHBOARD_SESSION_KEY_FILE",
                    "/run/media-secrets/dashboard-session.key",
                )
            ),
            plex_webhook_token_file=Path(
                os.getenv(
                    "MEDIA_GATEWAY_PLEX_WEBHOOK_TOKEN_FILE",
                    "/run/media-secrets/plex-webhook.key",
                )
            ),
            secure_cookies=_bool("MEDIA_GATEWAY_SECURE_COOKIES", False),
            radarr_profile_id=_int("MEDIA_GATEWAY_RADARR_PROFILE_ID"),
            radarr_root=os.environ["MEDIA_GATEWAY_RADARR_ROOT"],
            radarr_tags=_tags("MEDIA_GATEWAY_RADARR_TAG_IDS"),
            sonarr_profile_id=_int("MEDIA_GATEWAY_SONARR_PROFILE_ID"),
            sonarr_anime_profile_id=_int(
                "MEDIA_GATEWAY_SONARR_ANIME_PROFILE_ID",
                _int("MEDIA_GATEWAY_SONARR_PROFILE_ID"),
            ),
            sonarr_root=os.environ["MEDIA_GATEWAY_SONARR_ROOT"],
            sonarr_tags=_tags("MEDIA_GATEWAY_SONARR_TAG_IDS"),
            plex_machine_id=os.environ["MEDIA_GATEWAY_PLEX_MACHINE_ID"],
            notification_delay_seconds=_int("MEDIA_GATEWAY_NOTIFICATION_DELAY_SECONDS", 90),
            telegram_identity_sync=_bool("MEDIA_GATEWAY_TELEGRAM_IDENTITY_SYNC", True),
        )


def load_config_file() -> None:
    raw = os.getenv("MEDIA_GATEWAY_CONFIG_FILE")
    if not raw:
        return
    for name, value in read_dotenv(Path(raw), CONFIG_KEYS).items():
        os.environ.setdefault(name, value)
