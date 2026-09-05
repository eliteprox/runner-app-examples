#!/usr/bin/env python3
"""Pinned fal queue adapter offered as a static Live Runner capability."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

import aiohttp
from aiohttp import web

from fal_adapter import (
    DEFAULT_ROUTE_INDEX_PATH,
    EndpointDescriptor,
    artifact_paths,
    load_endpoint_descriptor,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8989
DEFAULT_ENDPOINT_KEY = "flux-schnell"
DEFAULT_DEADLINE_SECONDS = 180
DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024
FAL_BASE_URL = "https://queue.fal.run"
POLL_INTERVAL_SECONDS = 1.0
MAX_PROVIDER_ERROR_BODY_BYTES = 4096
PROVIDER_REQUEST_ID_HEADER = "Livepeer-Provider-Request-Id"
BILLABLE_UNITS_HEADER = "X-Fal-Billable-Units"
BILLABLE_UNITS_SOURCE = "x-fal-billable-units"

ROOT = Path(__file__).resolve().parent

log = logging.getLogger("live-runner")

SESSION_KEY = web.AppKey("session", aiohttp.ClientSession)
DESCRIPTOR_KEY = web.AppKey("endpoint_descriptor", EndpointDescriptor)
FAL_KEY = web.AppKey("fal_key", str)
FAL_BASE_URL_KEY = web.AppKey("fal_base_url", str)
POLL_INTERVAL_KEY = web.AppKey("poll_interval", float)
SCHEMA_BYTES_KEY = web.AppKey("schema_bytes", bytes)
SCHEMA_SHA256_KEY = web.AppKey("schema_sha256", str)
REQUEST_ID_KEY = web.RequestKey("fal_request_id", str | None)
STAGE_KEY = web.RequestKey("fal_stage", str)


@web.middleware
async def _request_log_middleware(
    request: web.Request, handler: Any
) -> web.StreamResponse:
    started = time.monotonic()
    response = await handler(request)
    if request.path in ("/", ""):
        log.info(
            json.dumps(
                {
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "request_id": request.get(REQUEST_ID_KEY),
                    "stage": request.get(STAGE_KEY, "submit"),
                    "status": response.status,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    return response


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live Runner HTTP app for a pinned fal queue route."
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="bind address (use 0.0.0.0 in containers)"
    )
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    parser.add_argument(
        "--endpoint-key",
        default=DEFAULT_ENDPOINT_KEY,
        help="modeled route capability to pin for this adapter process",
    )
    parser.add_argument(
        "--route-index",
        type=Path,
        default=DEFAULT_ROUTE_INDEX_PATH,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--deadline-seconds",
        type=int,
        default=DEFAULT_DEADLINE_SECONDS,
        help="operator deadline for the selected route",
    )
    parser.add_argument(
        "--max-request-bytes",
        type=int,
        default=DEFAULT_MAX_REQUEST_BYTES,
        help="operator request body limit for the selected route",
    )
    return parser.parse_args()


def _load_contract(
    descriptor: EndpointDescriptor, *, schema_path: Path | None = None
) -> tuple[bytes, str]:
    path = schema_path or artifact_paths(ROOT, descriptor).schema
    schema_bytes = path.read_bytes()
    schema_sha256 = hashlib.sha256(schema_bytes).hexdigest()
    schema = json.loads(schema_bytes)
    if not isinstance(schema, dict):
        raise RuntimeError("schema bundle must be a JSON object")
    if schema.get("endpoint_id") != descriptor.endpoint_id:
        raise RuntimeError("schema endpoint_id does not match endpoint descriptor")
    return schema_bytes, schema_sha256


def _bounded_upstream_body(raw: bytes, content_type: str) -> Any:
    bounded = raw[:MAX_PROVIDER_ERROR_BODY_BYTES]
    truncated = len(raw) > len(bounded)
    if "json" in content_type.lower() and not truncated:
        try:
            return json.loads(bounded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    text = bounded.decode(errors="replace")
    return text + ("…<truncated>" if truncated else "")


def _json_object(raw: bytes, stage: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"fal {stage} response was not valid JSON") from error
    if not isinstance(value, dict):
        raise TypeError(f"fal {stage} response must be a JSON object")
    return value


def _error_payload(
    app: web.Application,
    *,
    stage: str,
    status: int,
    message: str,
    request_id: str | None = None,
    upstream_body: Any | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"message": message, "stage": stage, "status": status}
    if upstream_body is not None:
        error["upstream_body"] = upstream_body
    payload: dict[str, Any] = {
        "endpoint_id": app[DESCRIPTOR_KEY].endpoint_id,
        "error": error,
        "schema_sha256": app[SCHEMA_SHA256_KEY],
    }
    if request_id:
        payload["request_id"] = request_id
    return payload


def _response_headers(request_id: str | None = None) -> dict[str, str]:
    if not request_id:
        return {}
    return {PROVIDER_REQUEST_ID_HEADER: request_id}


def _error_response(
    app: web.Application,
    *,
    stage: str,
    status: int,
    message: str,
    request_id: str | None = None,
    upstream_body: Any | None = None,
) -> web.Response:
    return web.json_response(
        _error_payload(
            app,
            stage=stage,
            status=status,
            message=message,
            request_id=request_id,
            upstream_body=upstream_body,
        ),
        status=status,
        headers=_response_headers(request_id),
    )


async def _begin_result_stream(
    request: web.Request, request_id: str
) -> web.StreamResponse:
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "application/json",
            PROVIDER_REQUEST_ID_HEADER: request_id,
        },
    )
    response.enable_chunked_encoding()
    await response.prepare(request)
    return response


async def _finish_json(
    response: web.StreamResponse, payload: dict[str, Any]
) -> web.StreamResponse:
    await response.write(json.dumps(payload).encode())
    await response.write_eof()
    return response


def _safe_upstream_status(status: int) -> int:
    return status if 400 <= status <= 599 else 502


def _auth_headers(*, has_body: bool) -> dict[str, str]:
    headers = {"Authorization": ""}
    if has_body:
        headers["Content-Type"] = "application/json"
    return headers


def extract_billable_units(headers: Mapping[str, str] | None) -> float | int | None:
    """Parse fal's billable-units header. Never raises; never guesses."""
    if headers is None:
        return None
    raw: Any = None
    getter = getattr(headers, "get", None)
    if getter is not None:
        raw = getter(BILLABLE_UNITS_HEADER)
        if raw is None:
            raw = getter(BILLABLE_UNITS_HEADER.lower())
    if raw is None:
        items = getattr(headers, "items", None)
        if items is None:
            return None
        lowered = BILLABLE_UNITS_HEADER.lower()
        for key, value in items():
            if isinstance(key, str) and key.lower() == lowered:
                raw = value
                break
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return int(value) if value.is_integer() else value


async def _upstream_response(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    fal_key: str,
    body: bytes | None = None,
) -> tuple[int, bytes, str, Mapping[str, str]]:
    headers = _auth_headers(has_body=body is not None)
    headers["Authorization"] = f"Key {fal_key}"
    async with session.request(method, url, data=body, headers=headers) as response:
        return (
            response.status,
            await response.read(),
            response.headers.get("Content-Type", ""),
            response.headers,
        )


def _request_base(
    base_url: str, descriptor: EndpointDescriptor, request_id: str
) -> str:
    encoded_id = quote(request_id, safe="")
    return f"{base_url.rstrip('/')}/{descriptor.queue_app_id}/requests/{encoded_id}"


async def _handle_health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "endpoint_id": request.app[DESCRIPTOR_KEY].endpoint_id,
            "schema_sha256": request.app[SCHEMA_SHA256_KEY],
            "status": "ok",
        }
    )


async def _handle_schema(request: web.Request) -> web.Response:
    return web.Response(
        body=request.app[SCHEMA_BYTES_KEY], content_type="application/json"
    )


async def _fail_execute(
    request: web.Request,
    stream: web.StreamResponse | None,
    *,
    stage: str,
    status: int,
    message: str,
    request_id: str | None = None,
    upstream_body: Any | None = None,
) -> web.StreamResponse:
    if stream is not None and stream.prepared:
        return await _finish_json(
            stream,
            _error_payload(
                request.app,
                stage=stage,
                status=status,
                message=message,
                request_id=request_id,
                upstream_body=upstream_body,
            ),
        )
    return _error_response(
        request.app,
        stage=stage,
        status=status,
        message=message,
        request_id=request_id,
        upstream_body=upstream_body,
    )


async def _handle_execute(request: web.Request) -> web.StreamResponse:
    request_id: str | None = None
    stream: web.StreamResponse | None = None
    request[REQUEST_ID_KEY] = None
    request[STAGE_KEY] = "submit"
    try:
        body = await request.read()
    except web.HTTPRequestEntityTooLarge:
        return _error_response(
            request.app,
            stage="submit",
            status=413,
            message="request body exceeds endpoint max_request_bytes",
        )

    descriptor = request.app[DESCRIPTOR_KEY]
    session = request.app[SESSION_KEY]
    base_url = request.app[FAL_BASE_URL_KEY].rstrip("/")
    submit_url = f"{base_url}/{descriptor.endpoint_id}"

    try:
        async with asyncio.timeout(descriptor.deadline_seconds):
            status, raw, upstream_content_type, _submit_headers = await _upstream_response(
                session,
                "POST",
                submit_url,
                fal_key=request.app[FAL_KEY],
                body=body,
            )
            if status < 200 or status >= 300:
                return _error_response(
                    request.app,
                    stage="submit",
                    status=_safe_upstream_status(status),
                    message=f"fal submit returned HTTP {status}",
                    upstream_body=_bounded_upstream_body(raw, upstream_content_type),
                )
            try:
                submitted = _json_object(raw, "submit")
            except (TypeError, ValueError) as error:
                return _error_response(
                    request.app, stage="submit", status=502, message=str(error)
                )
            candidate_id = submitted.get("request_id")
            if not isinstance(candidate_id, str) or not candidate_id:
                return _error_response(
                    request.app,
                    stage="submit",
                    status=502,
                    message="fal submit response missing request_id",
                )
            request_id = candidate_id
            request[REQUEST_ID_KEY] = request_id
            stream = await _begin_result_stream(request, request_id)
            request_base = _request_base(base_url, descriptor, request_id)

            request[STAGE_KEY] = "status"
            while True:
                status, raw, upstream_content_type, _status_headers = await _upstream_response(
                    session,
                    "GET",
                    f"{request_base}/status?logs=0",
                    fal_key=request.app[FAL_KEY],
                )
                if status < 200 or status >= 300:
                    return await _fail_execute(
                        request,
                        stream,
                        stage="status",
                        status=_safe_upstream_status(status),
                        message=f"fal status returned HTTP {status}",
                        request_id=request_id,
                        upstream_body=_bounded_upstream_body(
                            raw, upstream_content_type
                        ),
                    )
                try:
                    queue_status = _json_object(raw, "status")
                except (TypeError, ValueError) as error:
                    return await _fail_execute(
                        request,
                        stream,
                        stage="status",
                        status=502,
                        message=str(error),
                        request_id=request_id,
                    )

                state = queue_status.get("status")
                if state == "COMPLETED":
                    if queue_status.get("error"):
                        return await _fail_execute(
                            request,
                            stream,
                            stage="status",
                            status=502,
                            message="fal request completed with an error",
                            request_id=request_id,
                            upstream_body=_bounded_upstream_body(
                                json.dumps(queue_status).encode(), "application/json"
                            ),
                        )
                    break
                if state not in {"IN_QUEUE", "IN_PROGRESS"}:
                    return await _fail_execute(
                        request,
                        stream,
                        stage="status",
                        status=502,
                        message=f"fal status returned unknown state {state!r}",
                        request_id=request_id,
                    )
                await asyncio.sleep(request.app[POLL_INTERVAL_KEY])

            request[STAGE_KEY] = "result"
            status, raw, upstream_content_type, result_headers = await _upstream_response(
                session,
                "GET",
                request_base,
                fal_key=request.app[FAL_KEY],
            )
            if status < 200 or status >= 300:
                return await _fail_execute(
                    request,
                    stream,
                    stage="result",
                    status=_safe_upstream_status(status),
                    message=f"fal result returned HTTP {status}",
                    request_id=request_id,
                    upstream_body=_bounded_upstream_body(raw, upstream_content_type),
                )
            try:
                output = _json_object(raw, "result")
            except (TypeError, ValueError) as error:
                return await _fail_execute(
                    request,
                    stream,
                    stage="result",
                    status=502,
                    message=str(error),
                    request_id=request_id,
                )

            return await _finish_json(
                stream,
                {
                    "billable_units": extract_billable_units(result_headers),
                    "billable_units_source": BILLABLE_UNITS_SOURCE,
                    "endpoint_id": descriptor.endpoint_id,
                    "output": output,
                    "request_id": request_id,
                    "schema_sha256": request.app[SCHEMA_SHA256_KEY],
                },
            )
    except TimeoutError:
        request[STAGE_KEY] = "timeout"
        return await _fail_execute(
            request,
            stream,
            stage="timeout",
            status=504,
            message="adapter deadline exceeded; fal may still complete the request",
            request_id=request_id,
        )
    except aiohttp.ClientError as error:
        stage = request.get(STAGE_KEY, "submit")
        return await _fail_execute(
            request,
            stream,
            stage=stage,
            status=502,
            message=f"fal transport failed: {type(error).__name__}",
            request_id=request_id,
        )


def create_app(
    *,
    fal_key: str | None = None,
    fal_base_url: str = FAL_BASE_URL,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    endpoint_key: str = DEFAULT_ENDPOINT_KEY,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
    route_index_path: Path = DEFAULT_ROUTE_INDEX_PATH,
    descriptor: EndpointDescriptor | None = None,
    schema_path: Path | None = None,
) -> web.Application:
    key = fal_key if fal_key is not None else os.environ.get("FAL_KEY", "")
    if not key:
        raise RuntimeError("FAL_KEY is required")
    selected = descriptor or load_endpoint_descriptor(
        endpoint_key,
        deadline_seconds=deadline_seconds,
        max_request_bytes=max_request_bytes,
        route_index_path=route_index_path,
    )
    schema_bytes, schema_sha256 = _load_contract(selected, schema_path=schema_path)

    app = web.Application(
        middlewares=[_request_log_middleware],
        client_max_size=selected.max_request_bytes,
    )
    app[DESCRIPTOR_KEY] = selected
    app[FAL_KEY] = key
    app[FAL_BASE_URL_KEY] = fal_base_url
    app[POLL_INTERVAL_KEY] = poll_interval
    app[SCHEMA_BYTES_KEY] = schema_bytes
    app[SCHEMA_SHA256_KEY] = schema_sha256

    async def _on_startup(application: web.Application) -> None:
        application[SESSION_KEY] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None)
        )

    async def _on_cleanup(application: web.Application) -> None:
        with suppress(Exception):
            await application[SESSION_KEY].close()

    app.router.add_get("/health", _handle_health)
    app.router.add_get("/schema", _handle_schema)
    app.router.add_post("/", _handle_execute)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    app = create_app(
        endpoint_key=args.endpoint_key,
        deadline_seconds=args.deadline_seconds,
        max_request_bytes=args.max_request_bytes,
        route_index_path=args.route_index,
    )
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
