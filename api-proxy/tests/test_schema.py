from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

API_PROXY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_PROXY))

from fal_adapter import (  # noqa: E402
    EndpointDescriptor,
    RouteDefinition,
    artifact_paths,
    load_endpoint_descriptor,
    load_route_definition,
)


def _refs(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str):
                found.add(child)
            else:
                found.update(_refs(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_refs(child))
    return found


def _route_entry(
    endpoint_id: str = "fal-ai/flux/schnell",
    queue_app_id: str = "fal-ai/flux",
) -> dict[str, Any]:
    return {
        "app_id": "livepeer-example/fal-flux-schnell",
        "capability": "flux-schnell",
        "endpoint_id": endpoint_id,
        "input_policy": {
            "caller_authorization": "drop",
            "endpoint_selection": "pinned",
            "request_body": "provider_native_json",
        },
        "queue_app_id": queue_app_id,
        "transport_profile": "fal_queue_blocking_v1",
    }


class SchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.route = load_route_definition("flux-schnell")
        self.descriptor = load_endpoint_descriptor(
            "flux-schnell", deadline_seconds=180, max_request_bytes=1024 * 1024
        )
        self.schema_bytes = (
            API_PROXY / "contracts" / "flux-schnell" / "schema.json"
        ).read_bytes()
        self.schema = json.loads(self.schema_bytes)
        self.runners = json.loads(
            (
                API_PROXY
                / "deployments"
                / "local-offchain-flux"
                / "generated"
                / "runners.json"
            ).read_bytes()
        )
        self.metadata = json.loads(self.runners["runners"][0]["metadata"])

    def test_contract_identity_and_roots(self) -> None:
        self.assertEqual(self.route.endpoint_id, "fal-ai/flux/schnell")
        self.assertEqual(self.route.queue_app_id, "fal-ai/flux")
        self.assertEqual(self.descriptor.deadline_seconds, 180)
        self.assertEqual(self.descriptor.max_request_bytes, 1024 * 1024)
        self.assertEqual(self.schema["endpoint_id"], self.route.endpoint_id)
        self.assertEqual(
            self.schema["input_schema"]["$ref"],
            "#/components/schemas/FluxSchnellInput",
        )
        self.assertEqual(
            self.schema["output_schema"]["$ref"],
            "#/components/schemas/FluxSchnellOutput",
        )

    def test_all_component_references_are_present(self) -> None:
        components = self.schema["components"]["schemas"]
        referenced = {
            ref.removeprefix("#/components/schemas/") for ref in _refs(self.schema)
        }
        self.assertEqual(referenced - set(components), set())

    def test_metadata_hash_and_size(self) -> None:
        digest = hashlib.sha256(self.schema_bytes).hexdigest()
        self.assertEqual(self.runners["runners"][0]["app"], self.route.app_id)
        self.assertEqual(self.metadata["endpoint_id"], self.route.endpoint_id)
        self.assertEqual(self.metadata["schema_sha256"], digest)
        self.assertEqual(self.metadata["schema_url"], "./schema")
        self.assertEqual(self.metadata["deadline_seconds"], 180)
        self.assertLessEqual(len(self.runners["runners"][0]["metadata"].encode()), 1024)

    def test_generated_files_have_no_drift(self) -> None:
        result = subprocess.run(
            [sys.executable, str(API_PROXY / "generate_schema.py"), "--check"],
            cwd=API_PROXY.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_route_rejects_tracking_identity_drift(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "queue_app_id"):
            RouteDefinition.from_index_entry(_route_entry(queue_app_id="fal-ai/other"))

    def test_route_accepts_explicit_namespaced_queue_app(self) -> None:
        entry = _route_entry(
            endpoint_id="workflows/example/media/step",
            queue_app_id="workflows/example/media",
        )
        entry["app_id"] = "livepeer-example/fal-workflow-step"
        entry["capability"] = "workflow-step"
        route = RouteDefinition.from_index_entry(entry)
        descriptor = EndpointDescriptor.from_route(
            route, deadline_seconds=30, max_request_bytes=1024
        )
        self.assertEqual(descriptor.queue_app_id, "workflows/example/media")

    def test_artifact_paths_are_uniform_for_every_route(self) -> None:
        paths = artifact_paths(API_PROXY, self.route)
        self.assertEqual(
            paths.snapshot,
            API_PROXY / "contracts" / "flux-schnell" / "openapi.json",
        )
        self.assertEqual(
            paths.route,
            API_PROXY / "contracts" / "flux-schnell" / "route.json",
        )


if __name__ == "__main__":
    unittest.main()
