from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

API_PROXY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_PROXY))

from fal_adapter import (  # noqa: E402
    ROUTE_INDEX_FORMAT,
    RouteDefinition,
    load_route_definition,
)

ROUTE_INDEX_PATH = API_PROXY / "routes" / "index.json"


class RouteIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index: dict[str, Any] = json.loads(ROUTE_INDEX_PATH.read_bytes())
        cls.routes: list[dict[str, Any]] = cls.index["routes"]
        cls.by_capability = {route["capability"]: route for route in cls.routes}

    def test_route_count_and_identity_are_unique(self) -> None:
        self.assertEqual(self.index["format"], ROUTE_INDEX_FORMAT)
        self.assertEqual(len(self.routes), 73)
        self.assertEqual(len(self.by_capability), 73)
        self.assertEqual(len({route["endpoint_id"] for route in self.routes}), 73)
        self.assertEqual(len({route["app_id"] for route in self.routes}), 73)

    def test_every_entry_is_only_a_pinned_execution_route(self) -> None:
        required = {
            "app_id",
            "capability",
            "endpoint_id",
            "input_policy",
            "queue_app_id",
            "transport_profile",
        }
        forbidden = {
            "capacity",
            "deadline_seconds",
            "deployable",
            "input_kinds",
            "max_request_bytes",
            "operation",
            "output_kinds",
            "price_info",
            "published",
            "release_state",
            "section",
        }
        for entry in self.routes:
            with self.subTest(capability=entry["capability"]):
                self.assertEqual(set(entry), required)
                self.assertTrue(forbidden.isdisjoint(entry))
                route = RouteDefinition.from_index_entry(entry)
                self.assertEqual(route.capability, entry["capability"])
                self.assertEqual(route.endpoint_id, entry["endpoint_id"])

    def test_all_73_routes_load_by_capability(self) -> None:
        loaded = [load_route_definition(entry["capability"]) for entry in self.routes]
        self.assertEqual(len(loaded), 73)

    def test_selected_endpoint_corrections_are_preserved(self) -> None:
        expected = {
            "whisper-transcribe": "fal-ai/whisper",
            "topaz-image-upscale": "topaz/upscale/image/precision",
            "flux-video-upscale": "blackforestlabs/flux-video-upscale",
            "mmaudio-v2-video-soundtrack": "fal-ai/mmaudio-v2",
        }
        for capability, endpoint_id in expected.items():
            with self.subTest(capability=capability):
                self.assertEqual(
                    self.by_capability[capability]["endpoint_id"], endpoint_id
                )
        self.assertNotIn("aura-sr-upscale", self.by_capability)


if __name__ == "__main__":
    unittest.main()
