from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

API_PROXY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_PROXY))

import generate_contracts  # noqa: E402
import generate_schema  # noqa: E402
from fal_adapter import RouteDefinition  # noqa: E402


class ContractFleetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.flux_document = json.loads(
            (API_PROXY / "contracts" / "flux-schnell" / "openapi.json").read_bytes()
        )
        route_index = json.loads((API_PROXY / "routes" / "index.json").read_bytes())
        cls.flux_entry = next(
            entry
            for entry in route_index["routes"]
            if entry["capability"] == "flux-schnell"
        )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.route_index_path = self.root / "routes" / "index.json"
        self.report_path = self.root / "contracts" / "report.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    @classmethod
    def _document_for(cls, endpoint_id: str) -> dict[str, object]:
        document = copy.deepcopy(cls.flux_document)
        old_endpoint = "fal-ai/flux/schnell"
        document["info"]["x-fal-metadata"]["endpointId"] = endpoint_id
        document["info"]["title"] = f"Queue OpenAPI for {endpoint_id}"
        document["paths"] = {
            path.replace(old_endpoint, endpoint_id): operation
            for path, operation in document["paths"].items()
        }
        return document

    def _entry(self, endpoint_id: str = "vendor/example/generate") -> dict[str, object]:
        entry = copy.deepcopy(self.flux_entry)
        entry.update(
            {
                "app_id": "livepeer-example/fal-example-route",
                "capability": "example-route",
                "endpoint_id": endpoint_id,
                "queue_app_id": "/".join(endpoint_id.split("/")[:2]),
            }
        )
        return entry

    def _write_route_index(self, entry: dict[str, object]) -> bytes:
        self.route_index_path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(
            {"format": "livepeer.fal.route-index.v1", "routes": [entry]},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        self.route_index_path.write_bytes(raw)
        return raw

    def test_refresh_writes_route_model_without_mutating_source_index(self) -> None:
        entry = self._entry()
        original_index = self._write_route_index(entry)
        document = self._document_for(entry["endpoint_id"])

        result = generate_contracts.refresh_routes(
            route_index_path=self.route_index_path,
            report_path=self.report_path,
            root=self.root,
            fetch_document=lambda route: document,
            jobs=1,
        )

        self.assertEqual(result, 0)
        self.assertEqual(self.route_index_path.read_bytes(), original_index)
        report = json.loads(self.report_path.read_bytes())
        self.assertEqual(
            report["summary"],
            {"failed": 0, "snapshots_verified": 1, "total": 1},
        )
        self.assertIn(
            "not provider execution", report["provenance"]["verification_scope"]
        )
        self.assertEqual(report["results"][0]["status"], "snapshot_verified")
        contract_root = self.root / "contracts" / "example-route"
        self.assertTrue((contract_root / "openapi.json").is_file())
        self.assertTrue((contract_root / "route.json").is_file())
        self.assertTrue((contract_root / "schema.json").is_file())
        _, items = generate_contracts._route_items(self.route_index_path)
        self.assertEqual(
            (self.root / "routes" / "INVENTORY.md").read_bytes(),
            generate_contracts._inventory_bytes(items),
        )
        self.assertEqual(list(self.root.rglob("runners.json")), [])

        with mock.patch.object(
            generate_contracts.urllib.request,
            "urlopen",
            side_effect=AssertionError("offline check attempted network access"),
        ):
            self.assertEqual(
                generate_contracts.check_routes(
                    route_index_path=self.route_index_path,
                    report_path=self.report_path,
                    root=self.root,
                ),
                0,
            )

    def test_refresh_failure_records_error_without_claiming_a_route(self) -> None:
        entry = self._entry()
        self._write_route_index(entry)

        def fail_fetch(route: RouteDefinition) -> dict[str, object]:
            raise OSError(f"unavailable {route.endpoint_id}")

        result = generate_contracts.refresh_routes(
            route_index_path=self.route_index_path,
            report_path=self.report_path,
            root=self.root,
            fetch_document=fail_fetch,
            jobs=1,
        )

        self.assertEqual(result, 1)
        row = json.loads(self.report_path.read_bytes())["results"][0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"]["type"], "OSError")
        self.assertIn("unavailable vendor/example/generate", row["error"]["message"])
        self.assertFalse((self.root / "contracts" / "example-route").exists())

    def test_invalid_identity_writes_no_route_pack(self) -> None:
        entry = self._entry()
        self._write_route_index(entry)

        result = generate_contracts.refresh_routes(
            route_index_path=self.route_index_path,
            report_path=self.report_path,
            root=self.root,
            fetch_document=lambda route: self.flux_document,
            jobs=1,
        )

        self.assertEqual(result, 1)
        self.assertFalse((self.root / "contracts" / "example-route").exists())
        row = json.loads(self.report_path.read_bytes())["results"][0]
        self.assertIn("endpoint mismatch", row["error"]["message"])

    def test_offline_check_detects_schema_drift(self) -> None:
        entry = self._entry()
        self._write_route_index(entry)
        document = self._document_for(entry["endpoint_id"])
        self.assertEqual(
            generate_contracts.refresh_routes(
                route_index_path=self.route_index_path,
                report_path=self.report_path,
                root=self.root,
                fetch_document=lambda route: document,
                jobs=1,
            ),
            0,
        )
        schema_path = self.root / "contracts" / "example-route" / "schema.json"
        schema_path.write_text("{}\n", encoding="utf-8")
        self.assertEqual(
            generate_contracts.check_routes(
                route_index_path=self.route_index_path,
                report_path=self.report_path,
                root=self.root,
            ),
            1,
        )

    def test_offline_check_detects_inventory_drift(self) -> None:
        entry = self._entry()
        self._write_route_index(entry)
        document = self._document_for(entry["endpoint_id"])
        self.assertEqual(
            generate_contracts.refresh_routes(
                route_index_path=self.route_index_path,
                report_path=self.report_path,
                root=self.root,
                fetch_document=lambda route: document,
                jobs=1,
            ),
            0,
        )
        (self.root / "routes" / "INVENTORY.md").write_text(
            "stale inventory\n", encoding="utf-8"
        )
        self.assertEqual(
            generate_contracts.check_routes(
                route_index_path=self.route_index_path,
                report_path=self.report_path,
                root=self.root,
            ),
            1,
        )

    def test_projection_requires_submit_status_result_and_cancel(self) -> None:
        endpoint_id = "vendor/example/generate"
        route = RouteDefinition.from_index_entry(self._entry(endpoint_id))
        for suffix, stage in (
            ("", "submit"),
            ("/requests/{request_id}/status", "status"),
            ("/requests/{request_id}", "result"),
            ("/requests/{request_id}/cancel", "cancel"),
        ):
            with self.subTest(stage=stage):
                document = self._document_for(endpoint_id)
                del document["paths"][f"/{endpoint_id}{suffix}"]
                with self.assertRaisesRegex(RuntimeError, stage):
                    generate_schema._build_schema_bundle(document, route)

    def test_committed_inventory_has_73_self_contained_route_models(self) -> None:
        index, items = generate_contracts._route_items(
            API_PROXY / "routes" / "index.json"
        )
        report = json.loads((API_PROXY / "contracts" / "report.json").read_bytes())
        report_by_endpoint = {
            result["endpoint_id"]: result for result in report["results"]
        }
        self.assertEqual(len(items), 73)
        self.assertEqual(len(report_by_endpoint), 73)
        self.assertEqual(report["provenance"]["contract_authority"], "fal")
        self.assertEqual(report["route_index_format"], index["format"])
        self.assertEqual(
            (API_PROXY / "routes" / "INVENTORY.md").read_bytes(),
            generate_contracts._inventory_bytes(items),
        )

        for item in items:
            with self.subTest(endpoint_id=item.route.endpoint_id):
                pack = generate_contracts._pack_from_disk(API_PROXY, item)
                bundle = json.loads(pack.schema_bytes)
                expected_url = generate_schema._openapi_url(item.route)
                snapshot = json.loads(pack.snapshot_bytes)
                self.assertEqual(
                    snapshot["info"]["x-fal-metadata"]["endpointId"],
                    item.route.endpoint_id,
                )
                self.assertEqual(bundle["source"]["openapi_url"], expected_url)
                self.assertEqual(bundle["endpoint_id"], item.route.endpoint_id)
                self.assertEqual(
                    report_by_endpoint[item.route.endpoint_id]["artifacts"][
                        "openapi_sha256"
                    ],
                    hashlib.sha256(pack.snapshot_bytes).hexdigest(),
                )
                self.assertEqual(
                    report_by_endpoint[item.route.endpoint_id]["artifacts"][
                        "schema_sha256"
                    ],
                    pack.schema_sha256,
                )
                route_document = json.loads(
                    (API_PROXY / "contracts" / item.route.capability / "route.json").read_bytes()
                )
                pricing = generate_contracts.normalize_pricing(
                    route_document["pricing"], item.route.endpoint_id
                )
                self.assertEqual(pricing["provider"], "fal")
                self.assertEqual(pricing["endpoint_id"], item.route.endpoint_id)
                self.assertEqual(pricing["currency"], "usd")
                self.assertGreater(float(pricing["unit_price"]), 0)

    def test_parse_fal_pricing_reads_prices_array(self) -> None:
        body = json.dumps(
            {
                "prices": [
                    {
                        "currency": "USD",
                        "endpoint_id": "fal-ai/flux/schnell",
                        "unit": "image",
                        "unit_price": 0.003,
                    }
                ]
            }
        ).encode()
        pricing = generate_contracts.parse_fal_pricing(
            "fal-ai/flux/schnell", body, fetched_at="2026-09-09T00:00:00Z"
        )
        self.assertEqual(
            pricing,
            {
                "currency": "usd",
                "endpoint_id": "fal-ai/flux/schnell",
                "fetched_at": "2026-09-09T00:00:00Z",
                "provider": "fal",
                "unit": "image",
                "unit_price": "0.003",
            },
        )

    def test_refresh_stamps_pricing_and_offline_generate_preserves_it(self) -> None:
        entry = self._entry()
        self._write_route_index(entry)
        document = self._document_for(entry["endpoint_id"])
        pricing = {
            "currency": "usd",
            "endpoint_id": entry["endpoint_id"],
            "fetched_at": "2026-09-09T00:00:00Z",
            "provider": "fal",
            "unit": "image",
            "unit_price": "0.025",
        }
        self.assertEqual(
            generate_contracts.refresh_routes(
                route_index_path=self.route_index_path,
                report_path=self.report_path,
                root=self.root,
                fetch_document=lambda route: document,
                fetch_pricing=lambda route: pricing,
                jobs=1,
            ),
            0,
        )
        route_path = self.root / "contracts" / "example-route" / "route.json"
        written = json.loads(route_path.read_bytes())
        self.assertEqual(written["pricing"], pricing)

        self.assertEqual(
            generate_contracts.generate_routes(
                route_index_path=self.route_index_path,
                report_path=self.report_path,
                root=self.root,
            ),
            0,
        )
        self.assertEqual(json.loads(route_path.read_bytes())["pricing"], pricing)
        self.assertEqual(
            generate_contracts.check_routes(
                route_index_path=self.route_index_path,
                report_path=self.report_path,
                root=self.root,
            ),
            0,
        )

    def test_pricing_fetch_failure_keeps_committed_price(self) -> None:
        entry = self._entry()
        self._write_route_index(entry)
        document = self._document_for(entry["endpoint_id"])
        first = {
            "currency": "usd",
            "endpoint_id": entry["endpoint_id"],
            "fetched_at": "2026-09-09T00:00:00Z",
            "provider": "fal",
            "unit": "image",
            "unit_price": "0.01",
        }
        self.assertEqual(
            generate_contracts.refresh_routes(
                route_index_path=self.route_index_path,
                report_path=self.report_path,
                root=self.root,
                fetch_document=lambda route: document,
                fetch_pricing=lambda route: first,
                jobs=1,
            ),
            0,
        )

        def fail_pricing(route: RouteDefinition) -> dict[str, object]:
            raise OSError(f"pricing down {route.endpoint_id}")

        self.assertEqual(
            generate_contracts.refresh_routes(
                route_index_path=self.route_index_path,
                report_path=self.report_path,
                root=self.root,
                fetch_document=lambda route: document,
                fetch_pricing=fail_pricing,
                jobs=1,
            ),
            0,
        )
        written = json.loads(
            (self.root / "contracts" / "example-route" / "route.json").read_bytes()
        )
        self.assertEqual(written["pricing"]["unit_price"], "0.01")

    def test_refuses_zero_or_missing_unit_price(self) -> None:
        with self.assertRaisesRegex(generate_contracts.FleetError, "positive decimal"):
            generate_contracts.parse_fal_pricing(
                "fal-ai/flux/schnell",
                json.dumps(
                    {
                        "prices": [
                            {
                                "currency": "USD",
                                "endpoint_id": "fal-ai/flux/schnell",
                                "unit": "image",
                                "unit_price": 0,
                            }
                        ]
                    }
                ).encode(),
                fetched_at="2026-09-09T00:00:00Z",
            )


if __name__ == "__main__":
    unittest.main()
