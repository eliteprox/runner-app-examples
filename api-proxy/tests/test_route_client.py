from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

API_PROXY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_PROXY))

import client as client_cli  # noqa: E402
from fal_adapter.route_client import (  # noqa: E402
    RouteContractError,
    _runner_url,
    load_runner_route,
    run_route_arguments,
)


class RoutePackTest(unittest.TestCase):
    def test_execute_root_keeps_discovery_app_path(self) -> None:
        base = "https://ai1.eliteencoder.net:8936/apps/fal-grok-image-2/app"
        self.assertEqual(_runner_url(base, "./"), base)
        self.assertEqual(
            _runner_url(base, "./health"),
            f"{base}/health",
        )

    def test_all_73_routes_have_the_same_self_contained_shape(self) -> None:
        index = json.loads((API_PROXY / "routes" / "index.json").read_bytes())
        routes = [load_runner_route(entry["capability"]) for entry in index["routes"]]
        self.assertEqual(len(routes), 73)
        self.assertEqual(
            {route.document["format"] for route in routes},
            {"livepeer.fal.runner-route.v1"},
        )
        self.assertEqual(
            {route.document["credential"]["environment_variable"] for route in routes},
            {"FAL_KEY"},
        )


class FakeRunner:
    def __init__(self) -> None:
        self.schema_bytes = (
            API_PROXY / "contracts" / "flux-schnell" / "schema.json"
        ).read_bytes()
        self.receipt = {
            "endpoint_id": "fal-ai/flux/schnell",
            "output": {"provider_native": {"value": "unchanged"}},
            "request_id": "req-route-1",
            "schema_sha256": hashlib.sha256(self.schema_bytes).hexdigest(),
        }
        self.app = web.Application()
        self.app.router.add_get("/schema", self.schema)

    async def schema(self, request: web.Request) -> web.Response:
        return web.Response(body=self.schema_bytes, content_type="application/json")


class RouteRunnerExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.route = load_runner_route("flux-schnell")
        self.fake = FakeRunner()
        self.server = TestServer(self.fake.app)
        await self.server.start_server()
        self.session = aiohttp.ClientSession()
        self.metadata = {
            "endpoint_id": self.route.endpoint_id,
            "provider": "fal",
            "schema_sha256": self.route.schema_sha256,
            "schema_url": "./schema",
            "transport": "queue",
        }
        self.runner = SimpleNamespace(
            app=self.route.app_id,
            raw={"metadata": json.dumps(self.metadata)},
            url=str(self.server.make_url("")),
        )

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.server.close()

    async def test_schema_is_verified_before_provider_native_route_call(self) -> None:
        raw = b'{"prompt":"route example","seed":17}'
        call = mock.AsyncMock(return_value=SimpleNamespace(data=self.fake.receipt))
        with mock.patch("fal_adapter.route_client.call_runner", call):
            receipt = await run_route_arguments(
                self.session,
                runner=self.runner,
                route=self.route,
                arguments_bytes=raw,
                signer_url="https://signer.example",
                timeout_seconds=210,
            )

        self.assertEqual(receipt, self.fake.receipt)
        call.assert_awaited_once_with(
            runner=self.runner,
            runner_url=str(self.server.make_url("")).rstrip("/"),
            payload={"prompt": "route example", "seed": 17},
            signer_url="https://signer.example",
            signer_headers=None,
            timeout=210,
        )

    async def test_schema_hash_mismatch_refuses_before_execution(self) -> None:
        self.fake.schema_bytes += b" "
        call = mock.AsyncMock()
        with (
            mock.patch("fal_adapter.route_client.call_runner", call),
            self.assertRaisesRegex(RouteContractError, "schema hash"),
        ):
            await run_route_arguments(
                self.session,
                runner=self.runner,
                route=self.route,
                arguments_bytes=b"{}",
                timeout_seconds=210,
            )
        call.assert_not_awaited()

    async def test_discovery_metadata_hash_drift_refuses_before_execution(self) -> None:
        self.metadata["schema_sha256"] = "0" * 64
        self.runner.raw["metadata"] = json.dumps(self.metadata)
        call = mock.AsyncMock()
        with (
            mock.patch("fal_adapter.route_client.call_runner", call),
            self.assertRaisesRegex(RouteContractError, "metadata schema_sha256 drift"),
        ):
            await run_route_arguments(
                self.session,
                runner=self.runner,
                route=self.route,
                arguments_bytes=b"{}",
                timeout_seconds=210,
            )
        call.assert_not_awaited()

    async def test_discovery_metadata_schema_url_is_pinned(self) -> None:
        self.metadata["schema_url"] = "https://other.example/schema"
        self.runner.raw["metadata"] = json.dumps(self.metadata)
        call = mock.AsyncMock()
        with (
            mock.patch("fal_adapter.route_client.call_runner", call),
            self.assertRaisesRegex(RouteContractError, "metadata schema_url drift"),
        ):
            await run_route_arguments(
                self.session,
                runner=self.runner,
                route=self.route,
                arguments_bytes=b"{}",
                timeout_seconds=210,
            )
        call.assert_not_awaited()


class RouteCliTest(unittest.IsolatedAsyncioTestCase):
    async def test_cli_discovers_only_the_modeled_app(self) -> None:
        route = load_runner_route("flux-schnell")
        discovered = SimpleNamespace(url="https://runner.example/app")
        cursor = SimpleNamespace(candidates=[discovered])
        receipt = {
            "endpoint_id": route.endpoint_id,
            "output": {"provider_native": "value"},
            "request_id": "req-cli-1",
            "schema_sha256": route.schema_sha256,
        }

        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.json"
            input_path.write_bytes(b'{"prompt":"complete object","seed":9}')
            args = SimpleNamespace(
                capability="flux-schnell",
                discovery="https://discovery.example",
                insecure=False,
                input_json=input_path,
                receipt_output=None,
                signer="https://signer.example",
                api_key="",
                timeout=210,
            )
            selector = mock.AsyncMock(return_value=cursor)
            run = mock.AsyncMock(return_value=receipt)
            with (
                mock.patch.object(client_cli, "runner_selector", selector),
                mock.patch.object(client_cli, "run_route_arguments", run),
                mock.patch("builtins.print"),
            ):
                await client_cli._run(args)

        selector.assert_awaited_once_with(
            discovery_url="https://discovery.example",
            app=route.app_id,
            signer_url="https://signer.example",
            signer_headers=None,
        )
        self.assertIs(run.await_args.kwargs["runner"], discovered)
        self.assertEqual(run.await_args.kwargs["route"], route)
        self.assertEqual(run.await_args.kwargs["timeout_seconds"], 210)


if __name__ == "__main__":
    unittest.main()
