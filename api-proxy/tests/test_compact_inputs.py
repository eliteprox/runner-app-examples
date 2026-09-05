from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

API_PROXY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_PROXY))

from fal_adapter import compact_input_summary  # noqa: E402


def _schema(capability: str) -> dict:
    return json.loads(
        (API_PROXY / "contracts" / capability / "schema.json").read_bytes()
    )


class CompactInputSummaryTest(unittest.TestCase):
    def test_flux_2_edit_requires_image_urls_array(self) -> None:
        summary = compact_input_summary(_schema("flux-2-edit"))
        self.assertEqual(summary["required"], ["prompt", "image_urls"])
        self.assertEqual(
            summary["fields"]["image_urls"], {"items": "string", "type": "array"}
        )
        self.assertEqual(summary["fields"]["prompt"], {"type": "string"})
        self.assertNotIn("image_url", summary["fields"])

    def test_bg_remove_and_topaz_require_image_url(self) -> None:
        bg_remove = compact_input_summary(_schema("bg-remove"))
        self.assertEqual(bg_remove["required"], ["image_url"])
        self.assertEqual(bg_remove["fields"]["image_url"], {"type": "string"})
        topaz = compact_input_summary(_schema("topaz-image-upscale"))
        self.assertEqual(topaz["required"], ["image_url"])
        self.assertEqual(topaz["fields"]["image_url"], {"type": "string"})

    def test_fill_and_erase_require_mask_url(self) -> None:
        fill = compact_input_summary(_schema("flux-fill"))
        self.assertEqual(fill["required"], ["prompt", "image_url", "mask_url"])
        self.assertEqual(fill["fields"]["mask_url"], {"type": "string"})
        erase = compact_input_summary(_schema("flux-erase"))
        self.assertEqual(erase["required"], ["image_url", "mask_url"])
        self.assertEqual(erase["fields"]["mask_url"], {"type": "string"})

    def test_grok_edit_requires_only_prompt_and_lists_image_urls(self) -> None:
        summary = compact_input_summary(_schema("grok-image-2-edit"))
        self.assertEqual(summary["required"], ["prompt"])
        self.assertEqual(
            summary["fields"]["image_urls"], {"items": "string", "type": "array"}
        )
        self.assertNotIn("image_url", summary["fields"])

    def test_missing_input_schema_is_empty(self) -> None:
        self.assertEqual(
            compact_input_summary({"endpoint_id": "x"}),
            {"fields": {}, "required": []},
        )


if __name__ == "__main__":
    unittest.main()
