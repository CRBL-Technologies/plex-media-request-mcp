from __future__ import annotations

import unittest
from datetime import datetime, timezone

from media_companion.config import (
    TimeoutConfig,
    load_config,
    normalize_url,
    parse_secret_file_reference,
)
from media_companion.errors import (
    InvalidSecretReferenceError,
    InvalidTimeoutConfigurationError,
    InvalidURLConfigurationError,
)
from media_companion.models import (
    MediaRequest,
    MediaType,
    PlexItem,
    QueueItem,
    QueueState,
    ServiceName,
    canonical_rating_key,
)


SYNTHETIC_REQUESTER_USER_ID = 42
SYNTHETIC_REQUESTER_CHAT_ID = -10042


class SecretReferenceTests(unittest.TestCase):
    def test_file_reference_is_lexical_and_does_not_require_existing_file(self) -> None:
        reference = parse_secret_file_reference(
            "file:///run/secrets/does-not-exist.key", field_name="actor_key"
        )

        self.assertEqual(str(reference.path), "/run/secrets/does-not-exist.key")
        self.assertEqual(reference.basename, "does-not-exist.key")
        self.assertEqual(str(reference), "<secret-file>")
        self.assertNotIn("does-not-exist", repr(reference))

    def test_relative_and_remote_references_are_rejected_without_echoing_value(
        self,
    ) -> None:
        secretish = "super-secret-value"
        for value in ("relative.key", "file://other-host/key", secretish):
            with self.subTest(value=value):
                with self.assertRaises(InvalidSecretReferenceError) as raised:
                    parse_secret_file_reference(value, field_name="token_file")
                self.assertNotIn(secretish, str(raised.exception))


class ConfigValidationTests(unittest.TestCase):
    def test_url_normalization_rejects_credentials_and_arguments(self) -> None:
        self.assertEqual(
            normalize_url("HTTPS://Media.Example:443/api/"),
            "https://media.example:443/api",
        )
        for value in (
            "file:///tmp/service",
            "https://user:password@example.test",
            "https://example.test/api?token=secret",
            "https://example.test/api#fragment",
        ):
            with self.subTest(value=value):
                with self.assertRaises(InvalidURLConfigurationError):
                    normalize_url(value)

    def test_timeout_defaults_and_hard_ceilings(self) -> None:
        self.assertEqual(TimeoutConfig().requests_timeout, (3.0, 15.0))
        with self.assertRaises(InvalidTimeoutConfigurationError):
            TimeoutConfig(connect_seconds=3.1)
        with self.assertRaises(InvalidTimeoutConfigurationError):
            TimeoutConfig(connect_seconds=2.0, total_seconds=1.0)
        with self.assertRaises(InvalidTimeoutConfigurationError):
            TimeoutConfig(connect_seconds=True)  # bool is not a duration.

    def test_load_config_only_retains_secret_paths(self) -> None:
        config = load_config(
            {
                "MEDIA_COMPANION_UPSTREAM_URL": "http://media-server-mcp:3000/",
                "MEDIA_COMPANION_UPSTREAM_TOKEN_FILE": "/run/secrets/upstream.env",
                "MEDIA_COMPANION_PLEX_URL": "http://plex:32400/",
                "MEDIA_COMPANION_PLEX_TOKEN_FILE": "/run/secrets/plex.token",
            }
        )

        self.assertEqual(config.upstream_url, "http://media-server-mcp:3000")
        self.assertEqual(config.plex_url, "http://plex:32400")
        self.assertEqual(config.timeouts.connect_seconds, 3.0)
        self.assertEqual(config.timeouts.total_seconds, 15.0)
        assert config.upstream_token_file is not None
        assert config.plex_token_file is not None
        self.assertEqual(config.upstream_token_file.basename, "upstream.env")
        self.assertEqual(config.upstream_token_file.key, "MCP_AUTH_TOKEN")
        self.assertEqual(config.plex_token_file.key, "PLEX_API_KEY")
        self.assertEqual(str(config.upstream_token_file), "<secret-file>")

    def test_inline_secret_is_not_accepted(self) -> None:
        with self.assertRaises(InvalidSecretReferenceError):
            load_config(
                {
                    "MEDIA_COMPANION_UPSTREAM_URL": "http://media-server-mcp:3000",
                    "MEDIA_COMPANION_UPSTREAM_TOKEN_FILE": "/run/secrets/upstream.env",
                    "MEDIA_COMPANION_RADARR_API_KEY": "do-not-accept-inline",
                }
            )

    def test_arr_request_defaults_are_loaded_from_legacy_or_companion_names(
        self,
    ) -> None:
        config = load_config(
            {
                "MEDIA_COMPANION_UPSTREAM_URL": "http://media-server-mcp:3000",
                "MEDIA_COMPANION_UPSTREAM_TOKEN_FILE": "/run/secrets/upstream.env",
                "PLEX_MEDIA_REQUEST_RADARR_QUALITY_PROFILE_ID": "7",
                "PLEX_MEDIA_REQUEST_RADARR_ROOT_FOLDER_PATH": "/data/media/movies",
                "PLEX_MEDIA_REQUEST_RADARR_TAG_IDS": "2,3,2",
                "MEDIA_COMPANION_SONARR_NORMAL_QUALITY_PROFILE_ID": "8",
                "PLEX_MEDIA_REQUEST_SONARR_ANIME_QUALITY_PROFILE_ID": "9",
                "PLEX_MEDIA_REQUEST_SONARR_ROOT_FOLDER_PATH": "/data/media/tv",
                "PLEX_MEDIA_REQUEST_SONARR_TAG_IDS": "4,5",
            }
        )

        self.assertEqual(config.radarr_quality_profile_id, 7)
        self.assertEqual(config.radarr_root_folder_path, "/data/media/movies")
        self.assertEqual(config.radarr_tag_ids, (2, 3))
        self.assertEqual(config.sonarr_normal_quality_profile_id, 8)
        self.assertEqual(config.sonarr_anime_quality_profile_id, 9)
        self.assertEqual(config.sonarr_root_folder_path, "/data/media/tv")
        self.assertEqual(config.sonarr_tag_ids, (4, 5))


class NormalizedModelTests(unittest.TestCase):
    def test_series_request_deduplicates_and_sorts_explicit_seasons(self) -> None:
        request = MediaRequest(
            media_type=MediaType.SERIES,
            provider_id=123,
            title="Example",
            seasons=(2, 0, 2, 1),
            requested_by_user_id=SYNTHETIC_REQUESTER_USER_ID,
            requested_by_chat_id=SYNTHETIC_REQUESTER_CHAT_ID,
        )

        self.assertEqual(request.seasons, (0, 1, 2))
        with self.assertRaises(ValueError):
            MediaRequest(
                media_type=MediaType.SERIES,
                provider_id=123,
                title="Example",
                seasons=(True,),
            )

    def test_plex_rating_key_and_episode_fields_are_bounded(self) -> None:
        self.assertEqual(canonical_rating_key("123"), "123")
        for value in ("0", "001", "1/2", "1?x=secret"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    canonical_rating_key(value)

        item = PlexItem(
            rating_key="123",
            media_type=MediaType.EPISODE,
            title="Episode",
            year=2026,
            season_number=0,
            episode_number=1,
            added_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        )
        self.assertEqual(item.season_number, 0)
        with self.assertRaises(ValueError):
            PlexItem(rating_key="123", media_type=MediaType.EPISODE, title="Episode")

    def test_queue_model_has_only_safe_summary_fields(self) -> None:
        item = QueueItem(
            service=ServiceName.RADARR,
            title="Movie",
            state=QueueState.DOWNLOADING,
            progress_percent=50,
            eta_seconds=60,
        )
        self.assertEqual(item.progress_percent, 50.0)
        self.assertFalse(hasattr(item, "download_id"))


if __name__ == "__main__":
    unittest.main()
