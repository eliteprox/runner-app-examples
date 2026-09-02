"""Load and validate immutable fal route definitions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROUTE_INDEX_FORMAT = "livepeer.fal.route-index.v1"
TRANSPORT_PROFILE = "fal_queue_blocking_v1"
APP_NAMESPACES = frozenset({"comfy", "workflows"})
CAPABILITY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

API_PROXY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROUTE_INDEX_PATH = API_PROXY_ROOT / "routes" / "index.json"


@dataclass(frozen=True, slots=True)
class RouteDefinition:
    """Provider and Runner identity shared by every instance of one route."""

    capability: str
    app_id: str
    endpoint_id: str
    queue_app_id: str
    transport_profile: str

    @classmethod
    def from_index_entry(cls, entry: dict[str, Any]) -> RouteDefinition:
        capability = _required_string(entry, "capability")
        if not CAPABILITY_PATTERN.fullmatch(capability):
            raise RuntimeError(f"invalid route capability {capability!r}")

        endpoint_id = _required_string(entry, "endpoint_id")
        queue_app_id = _required_string(entry, "queue_app_id")
        _validate_queue_identity(endpoint_id, queue_app_id)

        transport_profile = _required_string(entry, "transport_profile")
        if transport_profile != TRANSPORT_PROFILE:
            raise RuntimeError(
                f"unsupported transport_profile {transport_profile!r} for {capability}"
            )

        policy = entry.get("input_policy")
        if not isinstance(policy, dict):
            raise RuntimeError(f"route {capability} needs input_policy")
        expected_policy = {
            "caller_authorization": "drop",
            "endpoint_selection": "pinned",
            "request_body": "provider_native_json",
        }
        if policy != expected_policy:
            raise RuntimeError(
                f"route {capability} input_policy must be {expected_policy!r}"
            )

        return cls(
            capability=capability,
            app_id=_required_string(entry, "app_id"),
            endpoint_id=endpoint_id,
            queue_app_id=queue_app_id,
            transport_profile=transport_profile,
        )


@dataclass(frozen=True, slots=True)
class EndpointDescriptor(RouteDefinition):
    """One configured adapter instance for a pinned route."""

    deadline_seconds: float
    max_request_bytes: int

    @classmethod
    def from_route(
        cls,
        route: RouteDefinition,
        *,
        deadline_seconds: float,
        max_request_bytes: int,
    ) -> EndpointDescriptor:
        if isinstance(deadline_seconds, bool) or not isinstance(
            deadline_seconds, (int, float)
        ):
            raise RuntimeError("deadline_seconds must be numeric")
        if deadline_seconds <= 0:
            raise RuntimeError("deadline_seconds must be positive")
        if isinstance(max_request_bytes, bool) or not isinstance(
            max_request_bytes, int
        ):
            raise RuntimeError("max_request_bytes must be an integer")
        if max_request_bytes <= 0:
            raise RuntimeError("max_request_bytes must be positive")
        return cls(
            capability=route.capability,
            app_id=route.app_id,
            endpoint_id=route.endpoint_id,
            queue_app_id=route.queue_app_id,
            transport_profile=route.transport_profile,
            deadline_seconds=float(deadline_seconds),
            max_request_bytes=max_request_bytes,
        )


@dataclass(frozen=True, slots=True)
class ArtifactPaths:
    snapshot: Path
    schema: Path
    route: Path


def _required_string(value: dict[str, Any], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate:
        raise RuntimeError(f"route entry needs non-empty {key}")
    return candidate


def _expected_queue_app_id(endpoint_id: str) -> str:
    parts = endpoint_id.split("/")
    app_segments = 3 if parts and parts[0] in APP_NAMESPACES else 2
    if len(parts) < app_segments or any(not part for part in parts):
        raise RuntimeError(f"invalid fal endpoint_id {endpoint_id!r}")
    return "/".join(parts[:app_segments])


def _validate_queue_identity(endpoint_id: str, queue_app_id: str) -> None:
    expected = _expected_queue_app_id(endpoint_id)
    if queue_app_id != expected:
        raise RuntimeError(
            f"queue_app_id {queue_app_id!r} does not match fal app {expected!r} "
            f"for endpoint {endpoint_id!r}"
        )


def load_route_index(path: Path = DEFAULT_ROUTE_INDEX_PATH) -> list[dict[str, Any]]:
    try:
        index = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"could not load fal route index: {error}") from error
    if not isinstance(index, dict) or index.get("format") != ROUTE_INDEX_FORMAT:
        raise RuntimeError(f"fal route index must use {ROUTE_INDEX_FORMAT}")
    routes = index.get("routes")
    if not isinstance(routes, list) or not routes:
        raise RuntimeError("fal route index routes must be a non-empty list")
    return routes


def load_route_definition(
    capability: str,
    *,
    route_index_path: Path = DEFAULT_ROUTE_INDEX_PATH,
) -> RouteDefinition:
    """Load one pinned route identity from the source index."""

    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in load_route_index(route_index_path):
        if not isinstance(entry, dict):
            raise RuntimeError("fal route index entries must be objects")
        entry_capability = entry.get("capability")
        if isinstance(entry_capability, str):
            if entry_capability in seen:
                raise RuntimeError(
                    f"fal route index repeats capability {entry_capability!r}"
                )
            seen.add(entry_capability)
        if entry_capability == capability:
            matches.append(entry)
    if not matches:
        raise RuntimeError(f"fal route index has no capability {capability!r}")
    return RouteDefinition.from_index_entry(matches[0])


def load_endpoint_descriptor(
    capability: str,
    *,
    deadline_seconds: float,
    max_request_bytes: int,
    route_index_path: Path = DEFAULT_ROUTE_INDEX_PATH,
) -> EndpointDescriptor:
    """Configure one adapter instance from a route plus operator limits."""

    route = load_route_definition(capability, route_index_path=route_index_path)
    return EndpointDescriptor.from_route(
        route,
        deadline_seconds=deadline_seconds,
        max_request_bytes=max_request_bytes,
    )


def artifact_paths(root: Path, route: RouteDefinition) -> ArtifactPaths:
    """Return the self-contained generated pack for one pinned route."""

    contract_root = root / "contracts" / route.capability
    return ArtifactPaths(
        snapshot=contract_root / "openapi.json",
        route=contract_root / "route.json",
        schema=contract_root / "schema.json",
    )
