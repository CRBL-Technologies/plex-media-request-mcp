"""Closed tool policy for the media companion.

The two MCP surfaces have deliberately different contracts:

* regular users receive only the seven companion-owned tools in
  :data:`SHARED_TOOLS`; and
* the private administrator surface may proxy only the exact tools registered
  by upstream media-server-mcp v2.3.0, plus the reviewed companion repair
  operation.

This module is intentionally data-only.  The tuples, sets, and mappings below
are immutable so a future upstream discovery response cannot expand the live
surface.  A tool not present in a surface's frozen set is denied by callers;
classification also returns ``None`` for unknown names rather than assigning a
permissive default.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Final, Mapping


# The source revision is the signed upstream v2.3.0 release commit.  The
# registry currently exposes only ``main``/``latest`` for this image because
# release-please's tag does not match the image workflow's semver pattern.  A
# registry HEAD/config inspection verified that this OCI index was built from
# the pinned revision, so production uses this digest rather than a mutable
# tag.
UPSTREAM_VERSION: Final[str] = "2.3.0"
UPSTREAM_REVISION: Final[str] = "8b469d2b321b27dd1e4f5b89a7236b3ea43c3c72"
UPSTREAM_OCI_DIGEST: Final[str] = (
    "sha256:f83620da1d008ef18df3324b15e44854572ea41b528eff585033e4054b438377"
)
UPSTREAM_IMAGE: Final[str] = "ghcr.io/wyattjoh/media-server-mcp@" + UPSTREAM_OCI_DIGEST

# Explicit aliases make the provenance names usable by deployment/manifest
# code without requiring callers to duplicate or reinterpret the constants.
UPSTREAM_SOURCE_REVISION: Final[str] = UPSTREAM_REVISION
UPSTREAM_IMAGE_DIGEST: Final[str] = UPSTREAM_OCI_DIGEST
UPSTREAM_DIGEST: Final[str] = UPSTREAM_OCI_DIGEST
UPSTREAM_IMAGE_REFERENCE: Final[str] = UPSTREAM_IMAGE


class ToolClassification(str, Enum):
    """Side-effect classification used by the private admin surface."""

    READ = "read"
    MUTATE = "mutate"


# Compatibility aliases retain one canonical enum type while keeping the API
# readable at call sites that refer to a tool's class or kind.
ToolClass = ToolClassification
ToolKind = ToolClassification


SHARED_TOOLS: Final[tuple[str, ...]] = (
    "search_media",
    "request_movie",
    "request_series",
    "request_status",
    "download_status",
    "browse_library",
    "media_status",
)
SHARED_TOOL_NAMES: Final[tuple[str, ...]] = SHARED_TOOLS
SHARED_TOOL_SET: Final[frozenset[str]] = frozenset(SHARED_TOOLS)

_PLEX_TOOLS: Final[tuple[str, ...]] = (
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
)

_RADARR_TOOLS: Final[tuple[str, ...]] = (
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
)

_SONARR_TOOLS: Final[tuple[str, ...]] = (
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
)

_TMDB_TOOLS: Final[tuple[str, ...]] = (
    "tmdb_find_by_external_id",
    "tmdb_search_movies",
    "tmdb_search_tv",
    "tmdb_search_multi",
    "tmdb_get_popular_movies",
    "tmdb_discover_movies",
    "tmdb_discover_tv",
    "tmdb_get_genres",
    "tmdb_get_trending",
    "tmdb_get_now_playing_movies",
    "tmdb_get_top_rated_movies",
    "tmdb_get_upcoming_movies",
    "tmdb_get_popular_tv",
    "tmdb_get_top_rated_tv",
    "tmdb_get_on_the_air_tv",
    "tmdb_get_airing_today_tv",
    "tmdb_get_movie_details",
    "tmdb_get_tv_details",
    "tmdb_get_movie_recommendations",
    "tmdb_get_tv_recommendations",
    "tmdb_get_similar_movies",
    "tmdb_get_similar_tv",
    "tmdb_search_people",
    "tmdb_get_popular_people",
    "tmdb_get_person_details",
    "tmdb_get_person_movie_credits",
    "tmdb_get_person_tv_credits",
    "tmdb_search_collections",
    "tmdb_get_collection_details",
    "tmdb_search_keywords",
    "tmdb_get_movies_by_keyword",
    "tmdb_get_certifications",
    "tmdb_get_watch_providers",
    "tmdb_get_configuration",
    "tmdb_get_countries",
    "tmdb_get_languages",
    "tmdb_get_movie_credits",
    "tmdb_get_tv_credits",
)

# Keep service order and tool order aligned with the canonical audit.  The
# values are tuples so this remains a checked-in inventory, not a discovery
# cache.
UPSTREAM_TOOLS_BY_SERVICE: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "plex": _PLEX_TOOLS,
        "radarr": _RADARR_TOOLS,
        "sonarr": _SONARR_TOOLS,
        "tmdb": _TMDB_TOOLS,
    }
)
UPSTREAM_TOOL_CATEGORIES: Final[Mapping[str, tuple[str, ...]]] = (
    UPSTREAM_TOOLS_BY_SERVICE
)
TOOLS_BY_CATEGORY: Final[Mapping[str, tuple[str, ...]]] = UPSTREAM_TOOLS_BY_SERVICE

UPSTREAM_TOOLS: Final[tuple[str, ...]] = tuple(
    tool for tools in UPSTREAM_TOOLS_BY_SERVICE.values() for tool in tools
)
UPSTREAM_TOOL_NAMES: Final[tuple[str, ...]] = UPSTREAM_TOOLS
UPSTREAM_TOOL_SET: Final[frozenset[str]] = frozenset(UPSTREAM_TOOLS)

UPSTREAM_TOOL_CATEGORY_COUNTS: Final[Mapping[str, int]] = MappingProxyType(
    {service: len(tools) for service, tools in UPSTREAM_TOOLS_BY_SERVICE.items()}
)
UPSTREAM_CATEGORY_COUNTS: Final[Mapping[str, int]] = UPSTREAM_TOOL_CATEGORY_COUNTS
TOOL_CATEGORY_COUNTS: Final[Mapping[str, int]] = UPSTREAM_TOOL_CATEGORY_COUNTS


# The upstream annotation for radarr_search_movie_releases says idempotent,
# but its handler POSTs a MoviesSearch command.  It therefore belongs in the
# confirmation-gated mutation set for this policy.
_UPSTREAM_MUTATING_TOOLS: Final[frozenset[str]] = frozenset(
    {
        # Plex
        "plex_refresh_library",
        "plex_create_collection",
        "plex_add_to_collection",
        "plex_remove_from_collection",
        "plex_delete_collection",
        # Radarr
        "radarr_search_movie_releases",
        "radarr_add_movie",
        "radarr_delete_movie",
        "radarr_refresh_movie",
        "radarr_update_movie",
        "radarr_refresh_all_movies",
        "radarr_disk_scan",
        "radarr_grab_release",
        "radarr_delete_queue_item",
        "radarr_grab_queue_item",
        "radarr_search_all_missing",
        "radarr_mark_failed",
        # Sonarr
        "sonarr_add_series",
        "sonarr_delete_series",
        "sonarr_update_episode_monitoring",
        "sonarr_refresh_series",
        "sonarr_search_series_episodes",
        "sonarr_search_season",
        "sonarr_update_series",
        "sonarr_refresh_all_series",
        "sonarr_search_episodes",
        "sonarr_disk_scan",
        "sonarr_grab_release",
        "sonarr_delete_queue_item",
        "sonarr_grab_queue_item",
        "sonarr_search_all_missing",
        "sonarr_mark_failed",
    }
)

COMPANION_REPAIR_TOOL: Final[str] = "repair_blocked_imports"
COMPANION_ADMIN_TOOLS: Final[tuple[str, ...]] = (COMPANION_REPAIR_TOOL,)
COMPANION_TOOL_NAMES: Final[tuple[str, ...]] = COMPANION_ADMIN_TOOLS
COMPANION_ADMIN_TOOL_SET: Final[frozenset[str]] = frozenset(COMPANION_ADMIN_TOOLS)

UPSTREAM_MUTATING_TOOLS: Final[frozenset[str]] = _UPSTREAM_MUTATING_TOOLS
UPSTREAM_MUTATION_TOOLS: Final[frozenset[str]] = UPSTREAM_MUTATING_TOOLS
UPSTREAM_READ_ONLY_TOOLS: Final[frozenset[str]] = frozenset(
    UPSTREAM_TOOL_SET - UPSTREAM_MUTATING_TOOLS
)
UPSTREAM_READ_TOOLS: Final[frozenset[str]] = UPSTREAM_READ_ONLY_TOOLS

ADMIN_TOOLS: Final[tuple[str, ...]] = UPSTREAM_TOOLS + COMPANION_ADMIN_TOOLS
ADMIN_TOOL_NAMES: Final[tuple[str, ...]] = ADMIN_TOOLS
ADMIN_TOOL_SET: Final[frozenset[str]] = frozenset(ADMIN_TOOLS)
ADMIN_MUTATING_TOOLS: Final[frozenset[str]] = frozenset(
    (*UPSTREAM_MUTATING_TOOLS, COMPANION_REPAIR_TOOL)
)
MUTATING_TOOLS: Final[frozenset[str]] = ADMIN_MUTATING_TOOLS
MUTATION_TOOLS: Final[frozenset[str]] = MUTATING_TOOLS
READ_ONLY_TOOLS: Final[frozenset[str]] = UPSTREAM_READ_ONLY_TOOLS
READ_TOOLS: Final[frozenset[str]] = READ_ONLY_TOOLS


UPSTREAM_TOOL_CLASSIFICATIONS: Final[Mapping[str, ToolClassification]] = (
    MappingProxyType(
        {
            tool: (
                ToolClassification.MUTATE
                if tool in UPSTREAM_MUTATING_TOOLS
                else ToolClassification.READ
            )
            for tool in UPSTREAM_TOOLS
        }
    )
)
ADMIN_TOOL_CLASSIFICATIONS: Final[Mapping[str, ToolClassification]] = MappingProxyType(
    {
        **UPSTREAM_TOOL_CLASSIFICATIONS,
        COMPANION_REPAIR_TOOL: ToolClassification.MUTATE,
    }
)
TOOL_CLASSIFICATIONS: Final[Mapping[str, ToolClassification]] = (
    ADMIN_TOOL_CLASSIFICATIONS
)
TOOL_CLASSIFICATION: Final[Mapping[str, ToolClassification]] = TOOL_CLASSIFICATIONS

# Shared request tools are safe, bounded companion workflows and do not use
# the admin confirmation policy.  They are classified here only for callers
# that need to describe the complete seven-tool surface.
SHARED_TOOL_CLASSIFICATIONS: Final[Mapping[str, ToolClassification]] = MappingProxyType(
    {
        "search_media": ToolClassification.READ,
        "request_movie": ToolClassification.MUTATE,
        "request_series": ToolClassification.MUTATE,
        "request_status": ToolClassification.READ,
        "download_status": ToolClassification.READ,
        "browse_library": ToolClassification.READ,
        "media_status": ToolClassification.READ,
    }
)


def classify_upstream_tool(tool_name: object) -> ToolClassification | None:
    """Return an upstream tool's class, or ``None`` for an unknown name."""

    if not isinstance(tool_name, str):
        return None
    return UPSTREAM_TOOL_CLASSIFICATIONS.get(tool_name)


def classify_admin_tool(tool_name: object) -> ToolClassification | None:
    """Return an admin tool's class, or ``None`` for an unknown name."""

    if not isinstance(tool_name, str):
        return None
    return ADMIN_TOOL_CLASSIFICATIONS.get(tool_name)


def classify_tool(tool_name: object) -> ToolClassification | None:
    """Classify a reviewed admin tool; unknown tools fail closed.

    The generic helper intentionally uses the private/admin inventory because
    the read/mutate confirmation decision applies to that surface.  Shared
    wrappers can use :func:`classify_shared_tool` when they need the separate
    safe-request classification.
    """

    return classify_admin_tool(tool_name)


def classify_shared_tool(tool_name: object) -> ToolClassification | None:
    """Return a shared wrapper's class, or ``None`` for an unknown name."""

    if not isinstance(tool_name, str):
        return None
    return SHARED_TOOL_CLASSIFICATIONS.get(tool_name)


def is_shared_tool(tool_name: object) -> bool:
    """Return whether ``tool_name`` is in the exact shared surface."""

    return isinstance(tool_name, str) and tool_name in SHARED_TOOL_SET


def is_admin_tool(tool_name: object) -> bool:
    """Return whether ``tool_name`` is in the reviewed admin surface."""

    return isinstance(tool_name, str) and tool_name in ADMIN_TOOL_SET


def is_known_tool(tool_name: object) -> bool:
    """Return whether ``tool_name`` belongs to either frozen surface."""

    return is_shared_tool(tool_name) or is_admin_tool(tool_name)


def is_mutating_tool(tool_name: object) -> bool:
    """Return true only for a known admin mutation.

    Unknown names and shared wrappers return ``False``.  Callers must still
    check :func:`is_admin_tool` before dispatching; this predicate is not an
    allowlist by itself.
    """

    return isinstance(tool_name, str) and tool_name in ADMIN_MUTATING_TOOLS


def is_read_only_tool(tool_name: object) -> bool:
    """Return true only for a known upstream read-only tool."""

    return isinstance(tool_name, str) and tool_name in UPSTREAM_READ_ONLY_TOOLS


def is_tool_allowed(tool_name: object, *, surface: str) -> bool:
    """Check a tool against an explicit frozen surface.

    ``surface`` accepts only ``"shared"`` or ``"admin"``.  Invalid surface
    values and unknown tool names deny rather than falling back to another
    surface.
    """

    if not isinstance(tool_name, str):
        return False
    if surface == "shared":
        return tool_name in SHARED_TOOL_SET
    if surface == "admin":
        return tool_name in ADMIN_TOOL_SET
    return False


__all__ = [
    "ADMIN_MUTATING_TOOLS",
    "ADMIN_TOOL_CLASSIFICATIONS",
    "ADMIN_TOOL_NAMES",
    "ADMIN_TOOL_SET",
    "ADMIN_TOOLS",
    "COMPANION_ADMIN_TOOL_SET",
    "COMPANION_ADMIN_TOOLS",
    "COMPANION_REPAIR_TOOL",
    "COMPANION_TOOL_NAMES",
    "MUTATING_TOOLS",
    "MUTATION_TOOLS",
    "READ_ONLY_TOOLS",
    "READ_TOOLS",
    "SHARED_TOOL_CLASSIFICATIONS",
    "SHARED_TOOL_NAMES",
    "SHARED_TOOL_SET",
    "SHARED_TOOLS",
    "TOOL_CATEGORY_COUNTS",
    "TOOL_CLASSIFICATION",
    "TOOL_CLASSIFICATIONS",
    "ToolClass",
    "ToolClassification",
    "ToolKind",
    "TOOLS_BY_CATEGORY",
    "UPSTREAM_CATEGORY_COUNTS",
    "UPSTREAM_DIGEST",
    "UPSTREAM_IMAGE",
    "UPSTREAM_IMAGE_DIGEST",
    "UPSTREAM_IMAGE_REFERENCE",
    "UPSTREAM_MUTATING_TOOLS",
    "UPSTREAM_MUTATION_TOOLS",
    "UPSTREAM_OCI_DIGEST",
    "UPSTREAM_READ_ONLY_TOOLS",
    "UPSTREAM_READ_TOOLS",
    "UPSTREAM_REVISION",
    "UPSTREAM_SOURCE_REVISION",
    "UPSTREAM_TOOL_CATEGORIES",
    "UPSTREAM_TOOL_CATEGORY_COUNTS",
    "UPSTREAM_TOOL_CLASSIFICATIONS",
    "UPSTREAM_TOOL_NAMES",
    "UPSTREAM_TOOL_SET",
    "UPSTREAM_TOOLS",
    "UPSTREAM_TOOLS_BY_SERVICE",
    "UPSTREAM_VERSION",
    "classify_admin_tool",
    "classify_shared_tool",
    "classify_tool",
    "classify_upstream_tool",
    "is_admin_tool",
    "is_known_tool",
    "is_mutating_tool",
    "is_read_only_tool",
    "is_shared_tool",
    "is_tool_allowed",
]
