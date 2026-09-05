from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

API_PROXY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_PROXY))

import generate_deployment  # noqa: E402
from fal_adapter import RouteDefinition, route_contract  # noqa: E402


def _entry(
    capability: str = "flux-schnell",
    endpoint_id: str = "fal-ai/flux/schnell",
    *,
    app_id: str | None = None,
    queue_app_id: str | None = None,
) -> dict[str, Any]:
    return {
        "app_id": app_id or f"livepeer-example/fal-{capability}",
        "capability": capability,
        "endpoint_id": endpoint_id,
        "input_policy": {
            "caller_authorization": "drop",
            "endpoint_selection": "pinned",
            "request_body": "provider_native_json",
        },
        "queue_app_id": queue_app_id or "/".join(endpoint_id.split("/")[:2]),
        "transport_profile": "fal_queue_blocking_v1",
    }


class DeploymentGeneratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.route_index_path = self.root / "routes" / "index.json"
        self.route_index_path.parent.mkdir()
        self.deployment_dir = self.root / "deployments" / "test-example"
        self.deployment_dir.mkdir(parents=True)
        self.deployment_path = self.deployment_dir / "deployment.json"
        self.entries = [_entry(), _entry("video-route", "vendor/video")]
        self.route_index = {
            "format": "livepeer.fal.route-index.v1",
            "routes": self.entries,
        }
        for entry in self.entries:
            self._write_route_model(entry)
        self.deployment = {
            "adapter_image": "runner-example-fal-adapter:local",
            "deployment_id": "test-example",
            "format": "livepeer.fal.deployment.v2",
            "network": "offchain",
            "pricing_scope": "local_demo_only",
            "routes": [
                self._selection("flux-schnell", "flux-service", "fal-flux-schnell")
            ],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _write(path: Path, value: Any) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")

    def _write_route_model(self, entry: dict[str, Any]) -> str:
        route = RouteDefinition.from_index_entry(entry)
        contract_dir = self.root / "contracts" / route.capability
        contract_dir.mkdir(parents=True, exist_ok=True)
        schema_bytes = (
            json.dumps(
                {"endpoint_id": route.endpoint_id, "format": "test-schema"},
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()
        digest = hashlib.sha256(schema_bytes).hexdigest()
        (contract_dir / "schema.json").write_bytes(schema_bytes)
        self._write(contract_dir / "route.json", route_contract(entry, route, digest))
        return digest

    @staticmethod
    def _selection(capability: str, service: str, label: str) -> dict[str, Any]:
        return {
            "capability": capability,
            "capacity": 4,
            "deadline_seconds": 180,
            "label": label,
            "max_request_bytes": 1024 * 1024,
            "price_info": {"currency": "usd", "price": 0.0001, "unit": "fixed"},
            "service": service,
        }

    def _build(
        self,
        *,
        route_index: dict[str, Any] | None = None,
        deployment: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        self._write(self.route_index_path, route_index or self.route_index)
        self._write(self.deployment_path, deployment or self.deployment)
        return generate_deployment.build_artifacts(
            self.route_index_path, self.deployment_path
        )

    def test_only_explicitly_selected_route_is_generated(self) -> None:
        artifacts = self._build()
        runners = artifacts["runners.json"]["runners"]
        services = artifacts["compose.json"]["services"]

        self.assertEqual(len(runners), 1)
        self.assertEqual(runners[0]["app"], "livepeer-example/fal-flux-schnell")
        self.assertEqual(runners[0]["runner_url"], "http://flux-service:8989")
        self.assertEqual(runners[0]["routing"], "label")
        self.assertEqual(
            services["flux-service"]["command"],
            [
                "--endpoint-key=flux-schnell",
                "--deadline-seconds=180",
                "--max-request-bytes=1048576",
            ],
        )
        self.assertNotIn("video-route", json.dumps(artifacts))
        metadata = json.loads(runners[0]["metadata"])
        self.assertLessEqual(len(runners[0]["metadata"].encode()), 1024)
        self.assertEqual(metadata["endpoint_id"], "fal-ai/flux/schnell")
        self.assertEqual(metadata["deadline_seconds"], 180)

    def test_each_selected_route_gets_a_distinct_service_and_registration(self) -> None:
        deployment = copy.deepcopy(self.deployment)
        deployment["routes"].append(
            self._selection("video-route", "video-service", "fal-video")
        )
        artifacts = self._build(deployment=deployment)
        runners = artifacts["runners.json"]["runners"]

        self.assertEqual(len(runners), 2)
        self.assertEqual(
            {runner["runner_url"] for runner in runners},
            {"http://flux-service:8989", "http://video-service:8989"},
        )

    def test_unknown_route_is_refused(self) -> None:
        deployment = copy.deepcopy(self.deployment)
        deployment["routes"][0]["capability"] = "not-modeled"
        with self.assertRaisesRegex(generate_deployment.DeploymentError, "not modeled"):
            self._build(deployment=deployment)

    def test_operator_policy_is_required_in_deployment_selection(self) -> None:
        fields = {
            "deadline_seconds",
            "max_request_bytes",
            "capacity",
            "price_info",
        }
        for field in fields:
            with self.subTest(field=field):
                deployment = copy.deepcopy(self.deployment)
                deployment["routes"][0][field] = None
                with self.assertRaises(generate_deployment.DeploymentError):
                    self._build(deployment=deployment)

    def test_route_model_schema_hash_and_identity_are_verified(self) -> None:
        schema_path = self.root / "contracts" / "flux-schnell" / "schema.json"
        schema_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(generate_deployment.DeploymentError, "schema hash"):
            self._build()

        self._write_route_model(self.entries[0])
        route_path = self.root / "contracts" / "flux-schnell" / "route.json"
        route = json.loads(route_path.read_bytes())
        route["identity"]["endpoint_id"] = "vendor/wrong"
        self._write(route_path, route)
        with self.assertRaisesRegex(
            generate_deployment.DeploymentError, "identity drift"
        ):
            self._build()

    def test_duplicate_apps_labels_and_services_are_refused(self) -> None:
        for duplicate in ("app", "label", "service"):
            with self.subTest(duplicate=duplicate):
                entries = copy.deepcopy(self.entries)
                if duplicate == "app":
                    entries[1]["app_id"] = entries[0]["app_id"]
                    self._write_route_model(entries[1])
                route_index = {
                    "format": "livepeer.fal.route-index.v1",
                    "routes": entries,
                }
                deployment = copy.deepcopy(self.deployment)
                deployment["routes"].append(
                    self._selection(
                        "video-route",
                        "flux-service" if duplicate == "service" else "video-service",
                        "fal-flux-schnell" if duplicate == "label" else "fal-video",
                    )
                )
                with self.assertRaisesRegex(
                    generate_deployment.DeploymentError,
                    f"duplicate selected {duplicate}",
                ):
                    self._build(route_index=route_index, deployment=deployment)
                if duplicate == "app":
                    self._write_route_model(self.entries[1])

    def test_oversized_metadata_and_onchain_deployment_are_refused(self) -> None:
        huge_entry = copy.deepcopy(self.entries[0])
        huge_entry["endpoint_id"] = "vendor/" + "x" * 1100
        huge_entry["queue_app_id"] = "vendor/" + "x" * 1100
        self._write_route_model(huge_entry)
        route_index = copy.deepcopy(self.route_index)
        route_index["routes"][0] = huge_entry
        with self.assertRaisesRegex(generate_deployment.DeploymentError, "metadata"):
            self._build(route_index=route_index)

        self._write_route_model(self.entries[0])
        deployment = copy.deepcopy(self.deployment)
        deployment["network"] = "arbitrum-one-mainnet"
        with self.assertRaisesRegex(
            generate_deployment.DeploymentError, "onchain pricing"
        ):
            self._build(deployment=deployment)

    def test_check_mode_detects_generated_file_drift(self) -> None:
        artifacts = self._build()
        output_dir = self.deployment_dir / "generated"
        self.assertTrue(
            generate_deployment._write_or_check(artifacts, output_dir, check=False)
        )
        self.assertTrue(
            generate_deployment._write_or_check(artifacts, output_dir, check=True)
        )
        (output_dir / "runners.json").write_text("{}\n", encoding="utf-8")
        self.assertFalse(
            generate_deployment._write_or_check(artifacts, output_dir, check=True)
        )


class RepositoryDeploymentFixtureTest(unittest.TestCase):
    def test_local_flux_generated_files_have_no_drift(self) -> None:
        deployment = (
            API_PROXY / "deployments" / "local-offchain-flux" / "deployment.json"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(API_PROXY / "generate_deployment.py"),
                str(deployment),
                "--check",
            ],
            cwd=API_PROXY.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
