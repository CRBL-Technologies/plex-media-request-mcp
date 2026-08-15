from __future__ import annotations

import unittest

from media_companion import tool_policy as policy


class ToolPolicyTests(unittest.TestCase):
    def test_shared_surface_is_exactly_seven_tools(self) -> None:
        self.assertEqual(
            policy.SHARED_TOOLS,
            (
                "search_media",
                "request_movie",
                "request_series",
                "request_status",
                "download_status",
                "browse_library",
                "media_status",
            ),
        )
        self.assertEqual(len(policy.SHARED_TOOLS), 7)
        self.assertEqual(len(policy.SHARED_TOOL_SET), 7)

    def test_upstream_surface_has_exact_category_counts(self) -> None:
        self.assertEqual(len(policy.UPSTREAM_TOOLS), 102)
        self.assertEqual(len(policy.UPSTREAM_TOOL_SET), 102)
        self.assertEqual(
            dict(policy.UPSTREAM_TOOL_CATEGORY_COUNTS),
            {"plex": 12, "radarr": 22, "sonarr": 30, "tmdb": 38},
        )
        self.assertEqual(
            sum(policy.UPSTREAM_TOOL_CATEGORY_COUNTS.values()),
            len(policy.UPSTREAM_TOOLS),
        )

    def test_every_frozen_inventory_is_unique_and_disjoint(self) -> None:
        for tools in policy.UPSTREAM_TOOLS_BY_SERVICE.values():
            self.assertEqual(len(tools), len(set(tools)))
        self.assertEqual(len(policy.UPSTREAM_TOOLS), len(set(policy.UPSTREAM_TOOLS)))
        self.assertEqual(
            set(policy.SHARED_TOOLS).intersection(policy.UPSTREAM_TOOL_SET), set()
        )
        self.assertEqual(len(policy.ADMIN_TOOLS), len(set(policy.ADMIN_TOOLS)))
        self.assertNotIn(policy.COMPANION_REPAIR_TOOL, policy.UPSTREAM_TOOL_SET)

    def test_total_admin_classification_covers_upstream_and_repair(self) -> None:
        self.assertEqual(
            set(policy.ADMIN_TOOL_CLASSIFICATIONS), set(policy.ADMIN_TOOLS)
        )
        self.assertEqual(
            set(policy.UPSTREAM_TOOL_CLASSIFICATIONS), set(policy.UPSTREAM_TOOLS)
        )
        self.assertEqual(len(policy.ADMIN_TOOL_CLASSIFICATIONS), 103)
        self.assertEqual(
            sum(
                value is policy.ToolClassification.READ
                for value in policy.ADMIN_TOOL_CLASSIFICATIONS.values()
            ),
            70,
        )
        self.assertEqual(
            sum(
                value is policy.ToolClassification.MUTATE
                for value in policy.ADMIN_TOOL_CLASSIFICATIONS.values()
            ),
            33,
        )
        self.assertEqual(len(policy.UPSTREAM_READ_ONLY_TOOLS), 70)
        self.assertEqual(len(policy.UPSTREAM_MUTATING_TOOLS), 32)

    def test_unknown_names_fail_closed_on_both_surfaces(self) -> None:
        unknown = "future_tool_not_reviewed"
        self.assertIsNone(policy.classify_tool(unknown))
        self.assertIsNone(policy.classify_upstream_tool(unknown))
        self.assertIsNone(policy.classify_shared_tool(unknown))
        self.assertFalse(policy.is_known_tool(unknown))
        self.assertFalse(policy.is_admin_tool(unknown))
        self.assertFalse(policy.is_shared_tool(unknown))
        self.assertFalse(policy.is_mutating_tool(unknown))
        self.assertFalse(policy.is_tool_allowed(unknown, surface="admin"))
        self.assertFalse(policy.is_tool_allowed(unknown, surface="shared"))
        self.assertFalse(policy.is_tool_allowed("search_media", surface="other"))
        self.assertIsNone(policy.classify_tool(None))
        self.assertFalse(policy.is_tool_allowed(None, surface="admin"))

    def test_canonical_admin_mutation_set_includes_release_search(self) -> None:
        expected = {
            "plex_refresh_library",
            "plex_create_collection",
            "plex_add_to_collection",
            "plex_remove_from_collection",
            "plex_delete_collection",
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
            "repair_blocked_imports",
        }
        self.assertEqual(policy.ADMIN_MUTATING_TOOLS, frozenset(expected))
        self.assertEqual(
            policy.classify_tool("radarr_search_movie_releases"),
            policy.ToolClassification.MUTATE,
        )
        self.assertEqual(
            policy.classify_tool("repair_blocked_imports"),
            policy.ToolClassification.MUTATE,
        )
        self.assertEqual(
            policy.classify_tool("plex_get_metadata"), policy.ToolClassification.READ
        )
        self.assertEqual(
            policy.classify_tool("tmdb_get_tv_credits"), policy.ToolClassification.READ
        )

    def test_provenance_is_pinned(self) -> None:
        self.assertEqual(policy.UPSTREAM_VERSION, "2.3.0")
        self.assertEqual(
            policy.UPSTREAM_REVISION,
            "8b469d2b321b27dd1e4f5b89a7236b3ea43c3c72",
        )
        self.assertEqual(
            policy.UPSTREAM_OCI_DIGEST,
            "sha256:f83620da1d008ef18df3324b15e44854572ea41b528eff585033e4054b438377",
        )
        self.assertEqual(
            policy.UPSTREAM_IMAGE,
            "ghcr.io/wyattjoh/media-server-mcp@" + policy.UPSTREAM_OCI_DIGEST,
        )


if __name__ == "__main__":
    unittest.main()
