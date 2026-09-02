"""Client-side verification for one pinned Live Runner route model."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp
from livepeer_gateway.live_runner import call_runner

from .contract import (
    DEFAULT_ROUTE_INDEX_PATH,
    artifact_paths,
    load_route_definition,
)
from .route_contract import ROUTE_CONTRACT_FORMAT

API_PROXY_ROOT = Path(__file__).resolve().parents[1]


class RouteContractError(RuntimeError):
    """A route model or discovered Runner failed verification."""


@dataclass(frozen=True, slots=True)
class RunnerRoute:
    capability: str
    app_id: str
    endpoint_id: str
    schema_sha256: str
    document: dict[str, Any]


def _load_object(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RouteContractError(f"could not load {context}: {error}") from error
    if not isinstance(value, dict):
        raise RouteContractError(f"{context} must be a JSON object")
    return value


def load_runner_route(
    capability: str,
    *,
    root: Path = API_PROXY_ROOT,
    route_index_path: Path = DEFAULT_ROUTE_INDEX_PATH,
) -> RunnerRoute:
    route = load_route_definition(capability, route_index_path=route_index_path)
    paths = artifact_paths(root, route)
    document = _load_object(paths.route, f"route contract {capability}")
    if document.get("format") != ROUTE_CONTRACT_FORMAT:
        raise RouteContractError(f"route contract {capability} has bad format")
    credential = document.get("credential")
    identity = document.get("identity")
    runner = document.get("runner")
    schema = document.get("schema")
    if not all(
        isinstance(value, dict) for value in (credential, identity, runner, schema)
    ):
        raise RouteContractError(f"route contract {capability} is incomplete")
    if credential != {
        "authorization_scheme": "Key",
        "environment_variable": "FAL_KEY",
        "owner": "operator",
    }:
        raise RouteContractError(f"route contract {capability} credential drift")
    if (
        identity.get("capability") != route.capability
        or identity.get("app_id") != route.app_id
        or identity.get("endpoint_id") != route.endpoint_id
        or identity.get("queue_app_id") != route.queue_app_id
    ):
        raise RouteContractError(f"route contract {capability} identity drift")
    schema_sha256 = schema.get("sha256")
    if (
        not isinstance(schema_sha256, str)
        or len(schema_sha256) != 64
        or any(character not in "0123456789abcdef" for character in schema_sha256)
    ):
        raise RouteContractError(f"route contract {capability} has bad schema hash")
    schema_bytes = paths.schema.read_bytes()
    if hashlib.sha256(schema_bytes).hexdigest() != schema_sha256:
        raise RouteContractError(f"route contract {capability} schema drift")
    if schema.get("path") != "./schema.json":
        raise RouteContractError(f"route contract {capability} has bad schema path")
    return RunnerRoute(
        capability=route.capability,
        app_id=route.app_id,
        endpoint_id=route.endpoint_id,
        schema_sha256=schema_sha256,
        document=document,
    )


def _runner_url(base_url: str, relative_path: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", relative_path)


async def verify_runner_schema(
    session: aiohttp.ClientSession,
    *,
    runner_url: str,
    route: RunnerRoute,
    ssl: bool | None = None,
) -> None:
    async with session.get(_runner_url(runner_url, "./schema"), ssl=ssl) as response:
        raw = await response.read()
        if response.status != 200:
            raise RouteContractError(f"Runner schema returned HTTP {response.status}")
    if hashlib.sha256(raw).hexdigest() != route.schema_sha256:
        raise RouteContractError("Runner schema hash does not match route model")
    schema = json.loads(raw)
    if not isinstance(schema, dict) or schema.get("endpoint_id") != route.endpoint_id:
        raise RouteContractError("Runner schema endpoint identity drift")


async def run_route_arguments(
    session: aiohttp.ClientSession,
    *,
    runner: Any,
    route: RunnerRoute,
    arguments_bytes: bytes,
    timeout_seconds: float,
    signer_url: str | None = None,
    ssl: bool | None = None,
) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise RouteContractError("timeout_seconds must be positive")
    try:
        arguments = json.loads(arguments_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RouteContractError("arguments are not valid JSON") from error
    if not isinstance(arguments, dict):
        raise RouteContractError("arguments must be a JSON object")
    runner_url = getattr(runner, "url", None)
    if not isinstance(runner_url, str) or not runner_url:
        raise RouteContractError("discovered Runner is missing its URL")
    await verify_runner_schema(session, runner_url=runner_url, route=route, ssl=ssl)
    result = await call_runner(
        runner=runner,
        runner_url=_runner_url(runner_url, "./proxy"),
        payload=arguments,
        signer_url=signer_url,
        timeout=timeout_seconds,
    )
    receipt = result.data
    if not isinstance(receipt, dict):
        raise RouteContractError("Runner receipt must be a JSON object")
    if receipt.get("endpoint_id") != route.endpoint_id:
        raise RouteContractError("Runner receipt endpoint identity drift")
    if receipt.get("schema_sha256") != route.schema_sha256:
        raise RouteContractError("Runner receipt schema hash drift")
    if not isinstance(receipt.get("request_id"), str) or not receipt["request_id"]:
        raise RouteContractError("Runner receipt is missing request_id")
    if not isinstance(receipt.get("output"), dict):
        raise RouteContractError("Runner receipt output must be a JSON object")
    return receipt
