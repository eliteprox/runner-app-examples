from __future__ import annotations

import hashlib
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

API_PROXY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_PROXY))

import runner  # noqa: E402
from fal_adapter import load_endpoint_descriptor  # noqa: E402


class FakeFal:
    def __init__(self) -> None:
        self.submit_status = 200
        self.submit_body: Any = {
            "request_id": "req-123",
            "status_url": "https://attacker.invalid/status",
        }
        self.status_status = 200
        self.status_bodies: list[Any] = [
            {"status": "IN_QUEUE", "request_id": "req-123"},
            {"status": "IN_PROGRESS", "request_id": "req-123"},
            {"status": "COMPLETED", "request_id": "req-123"},
        ]
        self.result_status = 200
        self.result_body: Any = {
            "has_nsfw_concepts": [False],
            "images": [
                {
                    "content_type": "image/png",
                    "height": 512,
                    "url": "https://example.test/image.png",
                    "width": 512,
                }
            ],
            "prompt": "not logged",
            "seed": 7,
            "timings": {"inference": 1.0},
        }
        self.submissions = 0
        self.status_calls = 0
        self.result_calls = 0
        self.received_body = b""
        self.received_authorization = ""
        self.received_content_type = ""
        self.received_submit_path = ""
        self.received_status_path = ""
        self.received_result_path = ""

        self.app = web.Application()
        self.app.router.add_post("/fal-ai/flux/schnell", self.submit)
        self.app.router.add_get(
            "/fal-ai/flux/requests/{request_id}/status", self.status
        )
        self.app.router.add_get("/fal-ai/flux/requests/{request_id}", self.result)

    @staticmethod
    def _response(body: Any, status: int) -> web.Response:
        if isinstance(body, (dict, list)):
            return web.json_response(body, status=status)
        if isinstance(body, bytes):
            return web.Response(body=body, status=status, content_type="text/plain")
        return web.Response(text=str(body), status=status, content_type="text/plain")

    async def submit(self, request: web.Request) -> web.Response:
        self.submissions += 1
        self.received_body = await request.read()
        self.received_authorization = request.headers.get("Authorization", "")
        self.received_content_type = request.headers.get("Content-Type", "")
        self.received_submit_path = request.path_qs
        return self._response(self.submit_body, self.submit_status)

    async def status(self, request: web.Request) -> web.Response:
        self.status_calls += 1
        self.received_status_path = request.path_qs
        index = min(self.status_calls - 1, len(self.status_bodies) - 1)
        return self._response(self.status_bodies[index], self.status_status)

    async def result(self, request: web.Request) -> web.Response:
        self.result_calls += 1
        self.received_result_path = request.path_qs
        return self._response(self.result_body, self.result_status)


class AdapterTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.descriptor = load_endpoint_descriptor(
            "flux-schnell", deadline_seconds=180, max_request_bytes=1024 * 1024
        )
        self.fake = FakeFal()
        self.fake_server = TestServer(self.fake.app)
        await self.fake_server.start_server()
        self.client = await self._adapter_client()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.fake_server.close()

    async def _adapter_client(
        self,
        *,
        timeout: float = 0.5,
        poll_interval: float = 0.001,
        max_request_bytes: int | None = None,
    ) -> TestClient:
        base_url = str(self.fake_server.make_url("")).rstrip("/")
        descriptor = replace(
            self.descriptor,
            deadline_seconds=timeout,
            max_request_bytes=(
                max_request_bytes
                if max_request_bytes is not None
                else self.descriptor.max_request_bytes
            ),
        )
        app = runner.create_app(
            fal_key="operator-secret",
            fal_base_url=base_url,
            poll_interval=poll_interval,
            descriptor=descriptor,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        return client

    async def test_health_and_schema_use_local_contract(self) -> None:
        health = await self.client.get("/health")
        self.assertEqual(health.status, 200)
        health_body = await health.json()

        schema = await self.client.get("/schema")
        schema_bytes = await schema.read()
        self.assertEqual(schema.status, 200)
        self.assertEqual(
            hashlib.sha256(schema_bytes).hexdigest(), health_body["schema_sha256"]
        )
        self.assertEqual(
            json.loads(schema_bytes)["endpoint_id"], self.descriptor.endpoint_id
        )
        self.assertEqual(self.fake.submissions, 0)

    async def test_exact_body_and_operator_auth_reach_pinned_queue(self) -> None:
        raw = b'{  "prompt" : "keep spacing", "seed": 7 }'
        response = await self.client.post(
            "/?endpoint=fal-ai/other",
            data=raw,
            headers={
                "Authorization": "Bearer caller-secret",
                "Content-Type": "text/plain; charset=caller-controlled",
            },
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(self.fake.received_body, raw)
        self.assertEqual(self.fake.received_authorization, "Key operator-secret")
        self.assertEqual(self.fake.received_content_type, "application/json")
        self.assertEqual(self.fake.received_submit_path, "/fal-ai/flux/schnell")
        self.assertEqual(
            self.fake.received_status_path,
            "/fal-ai/flux/requests/req-123/status?logs=0",
        )
        self.assertEqual(
            self.fake.received_result_path, "/fal-ai/flux/requests/req-123"
        )
        self.assertEqual(self.fake.submissions, 1)
        self.assertEqual(self.fake.status_calls, 3)
        self.assertEqual(self.fake.result_calls, 1)
        self.assertEqual(payload["request_id"], "req-123")
        self.assertEqual(payload["output"], self.fake.result_body)

    async def test_request_id_is_quoted_as_one_tracking_path_segment(self) -> None:
        request_base = runner._request_base(
            "https://queue.fal.run/",
            self.descriptor,
            "req/../other?logs=1#fragment%",
        )
        self.assertEqual(
            request_base,
            "https://queue.fal.run/fal-ai/flux/requests/"
            "req%2F..%2Fother%3Flogs%3D1%23fragment%25",
        )

    async def test_submit_error_is_typed_and_not_retried(self) -> None:
        self.fake.submit_status = 422
        self.fake.submit_body = {"detail": "invalid input"}
        response = await self.client.post("/", data=b"{}")
        payload = await response.json()

        self.assertEqual(response.status, 422)
        self.assertEqual(payload["error"]["stage"], "submit")
        self.assertEqual(self.fake.submissions, 1)
        self.assertEqual(self.fake.status_calls, 0)

    async def test_submit_requires_request_id(self) -> None:
        self.fake.submit_body = {"status": "IN_QUEUE"}
        response = await self.client.post("/", data=b"{}")
        payload = await response.json()

        self.assertEqual(response.status, 502)
        self.assertEqual(payload["error"]["stage"], "submit")
        self.assertIn("request_id", payload["error"]["message"])
        self.assertEqual(self.fake.submissions, 1)

    async def test_malformed_submit_response_is_typed(self) -> None:
        self.fake.submit_body = b"not-json"
        response = await self.client.post("/", data=b"{}")
        payload = await response.json()

        self.assertEqual(response.status, 502)
        self.assertEqual(payload["error"]["stage"], "submit")
        self.assertIn("not valid JSON", payload["error"]["message"])
        self.assertEqual(self.fake.status_calls, 0)

    async def test_status_error_preserves_request_id(self) -> None:
        self.fake.status_status = 503
        self.fake.status_bodies = [{"error": "busy"}]
        response = await self.client.post("/", data=b"{}")
        payload = await response.json()

        self.assertEqual(response.status, 503)
        self.assertEqual(payload["error"]["stage"], "status")
        self.assertEqual(payload["request_id"], "req-123")
        self.assertEqual(self.fake.submissions, 1)

    async def test_completed_error_does_not_fetch_result(self) -> None:
        self.fake.status_bodies = [
            {
                "error": "generation failed",
                "error_type": "InternalError",
                "request_id": "req-123",
                "status": "COMPLETED",
            }
        ]
        response = await self.client.post("/", data=b"{}")
        payload = await response.json()

        self.assertEqual(response.status, 502)
        self.assertEqual(payload["error"]["stage"], "status")
        self.assertEqual(self.fake.result_calls, 0)

    async def test_malformed_status_response_is_typed(self) -> None:
        self.fake.status_bodies = [b"not-json"]
        response = await self.client.post("/", data=b"{}")
        payload = await response.json()

        self.assertEqual(response.status, 502)
        self.assertEqual(payload["error"]["stage"], "status")
        self.assertEqual(payload["request_id"], "req-123")
        self.assertEqual(self.fake.result_calls, 0)

    async def test_unknown_status_state_is_typed(self) -> None:
        self.fake.status_bodies = [{"status": "CANCELLED"}]
        response = await self.client.post("/", data=b"{}")
        payload = await response.json()

        self.assertEqual(response.status, 502)
        self.assertEqual(payload["error"]["stage"], "status")
        self.assertIn("unknown state", payload["error"]["message"])
        self.assertEqual(self.fake.result_calls, 0)

    async def test_result_error_is_typed(self) -> None:
        self.fake.status_bodies = [{"status": "COMPLETED", "request_id": "req-123"}]
        self.fake.result_status = 500
        self.fake.result_body = {"error": "result unavailable"}
        response = await self.client.post("/", data=b"{}")
        payload = await response.json()

        self.assertEqual(response.status, 500)
        self.assertEqual(payload["error"]["stage"], "result")
        self.assertEqual(payload["request_id"], "req-123")

    async def test_malformed_result_response_is_typed(self) -> None:
        self.fake.status_bodies = [{"status": "COMPLETED"}]
        self.fake.result_body = b"not-json"
        response = await self.client.post("/", data=b"{}")
        payload = await response.json()

        self.assertEqual(response.status, 502)
        self.assertEqual(payload["error"]["stage"], "result")
        self.assertEqual(payload["request_id"], "req-123")

    async def test_result_transport_failure_keeps_result_stage(self) -> None:
        self.fake.status_bodies = [{"status": "COMPLETED"}]
        original = runner._upstream_response

        async def fail_result(*args: Any, **kwargs: Any) -> tuple[int, bytes, str]:
            if args[1] == "GET" and args[2].endswith("/requests/req-123"):
                raise aiohttp.ClientConnectionError("result transport failed")
            return await original(*args, **kwargs)

        with mock.patch.object(runner, "_upstream_response", side_effect=fail_result):
            response = await self.client.post("/", data=b"{}")
        payload = await response.json()

        self.assertEqual(response.status, 502)
        self.assertEqual(payload["error"]["stage"], "result")
        self.assertEqual(payload["request_id"], "req-123")

    async def test_timeout_does_not_resubmit_or_cancel(self) -> None:
        self.fake.status_bodies = [{"status": "IN_PROGRESS", "request_id": "req-123"}]
        timeout_client = await self._adapter_client(timeout=0.02, poll_interval=0.005)
        try:
            response = await timeout_client.post("/", data=b"{}")
            payload = await response.json()
        finally:
            await timeout_client.close()

        self.assertEqual(response.status, 504)
        self.assertEqual(payload["error"]["stage"], "timeout")
        self.assertEqual(payload["request_id"], "req-123")
        self.assertEqual(self.fake.submissions, 1)
        self.assertEqual(self.fake.result_calls, 0)

    async def test_other_paths_and_methods_are_not_executable(self) -> None:
        other_path = await self.client.post("/fal-ai/flux/dev", data=b"{}")
        wrong_method = await self.client.get("/")

        self.assertEqual(other_path.status, 404)
        self.assertEqual(wrong_method.status, 405)
        self.assertEqual(self.fake.submissions, 0)

    async def test_request_body_limit_is_operator_owned(self) -> None:
        limited_client = await self._adapter_client(max_request_bytes=16)
        try:
            response = await limited_client.post("/", data=b"x" * 17)
            payload = await response.json()
        finally:
            await limited_client.close()

        self.assertEqual(response.status, 413)
        self.assertEqual(payload["error"]["stage"], "submit")
        self.assertEqual(self.fake.submissions, 0)

    async def test_provider_error_body_is_bounded(self) -> None:
        self.fake.submit_status = 422
        self.fake.submit_body = "x" * (runner.MAX_PROVIDER_ERROR_BODY_BYTES + 100)
        response = await self.client.post("/", data=b"{}")
        payload = await response.json()

        upstream_body = payload["error"]["upstream_body"]
        self.assertTrue(upstream_body.endswith("<truncated>"))
        self.assertLessEqual(
            len(upstream_body), runner.MAX_PROVIDER_ERROR_BODY_BYTES + 20
        )

    async def test_adapter_log_excludes_bodies_and_credentials(self) -> None:
        raw = b'{"prompt":"log-secret-prompt"}'
        with self.assertLogs(runner.log, level="INFO") as captured:
            response = await self.client.post(
                "/",
                data=raw,
                headers={"Authorization": "Bearer caller-secret"},
            )
        self.assertEqual(response.status, 200)
        logs = "\n".join(captured.output)
        self.assertNotIn("log-secret-prompt", logs)
        self.assertNotIn("caller-secret", logs)
        self.assertNotIn("operator-secret", logs)


if __name__ == "__main__":
    unittest.main()
