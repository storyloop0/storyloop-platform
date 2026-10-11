"""Scenario-owned status values remain authoritative across saves and turns."""

import unittest

from storyloop_harness.advanced import Snapshot
from storyloop_platform.gameplay.presentation import turn_view
from storyloop_platform.runtime.guidance import GuidanceResult
from scenario_fixtures import loaded_status_fields


class StatusFieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {
            "actors": {"player": {"location": "villa"}},
            "relationships": {"a": {"met": False, "affinity": 0}},
            "player_stats": {"mood": 80},
        }
        self.declarations = [
            {"id": "mood", "label": "心情值", "path": ["player_stats", "mood"],
             "bounds": {"min": 0, "max": 100, "max_delta": 3}},
            {"id": "affinity_a", "label": "与甲的好感度",
             "path": ["relationships", "a", "affinity"],
             "when": {"path": ["relationships", "a", "met"], "equals": True},
             "bounds": {"min": 0, "max": 100, "max_delta": 2}},
            {"id": "location", "label": "当前位置", "path": ["actors", "player", "location"]},
        ]


    def test_portal_view_projects_only_declared_player_visible_status(self) -> None:
        fields = loaded_status_fields(self.declarations + [
            {"id": "secret", "label": "隐藏状态", "path": ["relationships", "a", "met"],
             "visible": False},
        ], self.state)
        view = turn_view("game", "scenario", "freeform", "", Snapshot("game", 0, 0, self.state),
                                  GuidanceResult((), "test"), status_fields=fields)
        self.assertEqual([item["id"] for item in view["status_fields"]], ["mood", "location"])




if __name__ == "__main__":
    unittest.main()
