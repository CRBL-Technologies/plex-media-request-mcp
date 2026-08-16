"""Pinned upstream provenance and closed tool inventories."""

from typing import Final

UPSTREAM_VERSION: Final = "2.3.0"
UPSTREAM_REVISION: Final = "8b469d2b321b27dd1e4f5b89a7236b3ea43c3c72"
UPSTREAM_IMAGE: Final = (
    "ghcr.io/wyattjoh/media-server-mcp@"
    "sha256:f83620da1d008ef18df3324b15e44854572ea41b528eff585033e4054b438377"
)

SHARED_TOOLS: Final = (
    "search_media",
    "recommend_media",
    "request_movie",
    "request_series",
    "request_status",
    "download_status",
    "browse_library",
    "media_status",
)

# This is a security allowlist, not a copied schema. Schemas are discovered
# from the pinned upstream service and checked against these exact names.
ADMIN_UPSTREAM_TOOLS: Final = frozenset(
    {
        "plex_get_capabilities",
        "plex_get_libraries",
        "plex_search",
        "plex_get_metadata",
        "plex_refresh_library",
        "plex_get_library_items",
        "plex_get_collections",
        "plex_get_collection_items",
        "plex_create_collection",
        "plex_add_to_collection",
        "plex_remove_from_collection",
        "plex_delete_collection",
        "radarr_search_movie",
        "radarr_add_movie",
        "radarr_delete_movie",
        "radarr_refresh_movie",
        "radarr_search_movie_releases",
        "radarr_get_movies",
        "radarr_get_movie",
        "radarr_get_configuration",
        "radarr_update_movie",
        "radarr_refresh_all_movies",
        "radarr_disk_scan",
        "radarr_get_wanted_missing",
        "radarr_get_wanted_cutoff",
        "radarr_get_history",
        "radarr_get_movie_history",
        "radarr_get_calendar",
        "radarr_get_releases",
        "radarr_grab_release",
        "radarr_delete_queue_item",
        "radarr_grab_queue_item",
        "radarr_search_all_missing",
        "radarr_mark_failed",
        "sonarr_search_series",
        "sonarr_add_series",
        "sonarr_delete_series",
        "sonarr_update_episode_monitoring",
        "sonarr_refresh_series",
        "sonarr_search_series_episodes",
        "sonarr_search_season",
        "sonarr_get_series",
        "sonarr_get_series_by_id",
        "sonarr_get_episodes",
        "sonarr_get_calendar",
        "sonarr_get_queue",
        "sonarr_get_configuration",
        "sonarr_get_system_status",
        "sonarr_get_health",
        "sonarr_update_series",
        "sonarr_get_episode",
        "sonarr_refresh_all_series",
        "sonarr_search_episodes",
        "sonarr_disk_scan",
        "sonarr_get_wanted_missing",
        "sonarr_get_wanted_cutoff",
        "sonarr_get_history",
        "sonarr_get_series_history",
        "sonarr_get_releases",
        "sonarr_grab_release",
        "sonarr_delete_queue_item",
        "sonarr_grab_queue_item",
        "sonarr_search_all_missing",
        "sonarr_mark_failed",
    }
)
