#!/usr/bin/env python3
"""Render selected fal route models as static Live Runner example config.

Every selected route becomes one adapter service and one ``runners.json`` entry.
The route model supplies immutable provider identity and schema bytes; the
deployment selection supplies operator policy such as limits, price and capacity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from fal_adapter import (
    DEFAULT_ROUTE_INDEX_PATH,
    ROUTE_CONTRACT_FORMAT,
    ROUTE_INDEX_FORMAT,
    RouteDefinition,
    artifact_paths,
)

ROOT = Path(__file__).resolve().parent
METADATA_LIMIT_BYTES = 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}")
NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
PRICE_UNITS = {"fixed", "hour", "720p"}
PRICING_SCOPES = {"local_demo_only", "committed_fal_catalog"}
DEFAULT_UPCHARGE_BPS = 0
DEFAULT_EXPECTED_UNITS = 1
SELL_PRICE_QUANTUM = Decimal("0.000000000001")


class DeploymentError(RuntimeError):
    """Raised when a deployment selection is unsafe or incomplete."""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate pinned local offchain fal Runner services."
    )
    parser.add_argument("deployment", type=Path, help="deployment selection JSON")
    parser.add_argument("--route-index", type=Path, default=DEFAULT_ROUTE_INDEX_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; fail when generated artifacts differ",
    )
    return parser.parse_args()


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except FileNotFoundError as error:
        raise DeploymentError(f"missing {label}: {path}") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeploymentError(f"could not read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise DeploymentError(f"{label} must be a JSON object")
    return value


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise DeploymentError(f"{field} must be a non-empty trimmed string")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DeploymentError(f"{field} must be a positive integer")
    return value


def _route_entries(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if index.get("format") != ROUTE_INDEX_FORMAT:
        raise DeploymentError(f"route index must use {ROUTE_INDEX_FORMAT}")
    raw = index.get("routes")
    if not isinstance(raw, list) or not raw:
        raise DeploymentError("route index routes must be a non-empty array")
    entries: dict[str, dict[str, Any]] = {}
    for position, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise DeploymentError(f"route index routes[{position}] must be an object")
        try:
            route = RouteDefinition.from_index_entry(entry)
        except RuntimeError as error:
            raise DeploymentError(str(error)) from error
        if route.capability in entries:
            raise DeploymentError(f"duplicate route capability {route.capability!r}")
        entries[route.capability] = entry
    return entries


def _selection_name(value: Any, field: str) -> str:
    name = _required_string(value, field)
    if not NAME_RE.fullmatch(name):
        raise DeploymentError(f"{field} must be a lowercase DNS-style name")
    if len(name) > 63:
        raise DeploymentError(f"{field} must not exceed 63 characters")
    return name


def _price_info(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DeploymentError(f"{field} is unresolved")
    price = value.get("price")
    if (
        isinstance(price, bool)
        or not isinstance(price, (int, float))
        or not math.isfinite(price)
        or price <= 0
    ):
        raise DeploymentError(f"{field}.price must be positive")
    currency = value.get("currency")
    unit = value.get("unit")
    if currency != "usd":
        raise DeploymentError(f"{field}.currency must be usd")
    if unit not in PRICE_UNITS:
        raise DeploymentError(
            f"{field}.unit must be one of {', '.join(sorted(PRICE_UNITS))}"
        )
    return {"currency": currency, "price": price, "unit": unit}


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DeploymentError(f"{field} must be a non-negative integer")
    return value


def _positive_quantity(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise DeploymentError(f"{field} must be a positive number")
    try:
        quantity = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise DeploymentError(f"{field} must be a positive number") from error
    if quantity <= 0:
        raise DeploymentError(f"{field} must be a positive number")
    return quantity


def _route_pricing(document: dict[str, Any], capability: str) -> dict[str, Any] | None:
    raw = document.get("pricing")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DeploymentError(f"route model {capability} pricing must be an object")
    try:
        unit_price = Decimal(str(raw.get("unit_price", "")).strip())
    except (InvalidOperation, ValueError) as error:
        raise DeploymentError(
            f"route model {capability} pricing.unit_price is invalid"
        ) from error
    if unit_price <= 0:
        raise DeploymentError(
            f"route model {capability} pricing.unit_price must be positive"
        )
    unit = raw.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        raise DeploymentError(f"route model {capability} pricing.unit is required")
    return {
        "unit": unit.strip(),
        "unit_price": unit_price,
    }


def _derived_price_info(
    *,
    unit_price: Decimal,
    expected_units: Decimal,
    upcharge_bps: int,
) -> dict[str, Any]:
    sell = (unit_price * expected_units * Decimal(10000 + upcharge_bps)) / Decimal(10000)
    quantized = sell.quantize(SELL_PRICE_QUANTUM, rounding=ROUND_HALF_UP)
    price = float(quantized)
    if not math.isfinite(price) or price <= 0:
        raise DeploymentError("derived sell price must be a positive finite number")
    return {"currency": "usd", "price": price, "unit": "fixed"}


def _selection_price_info(
    selection: dict[str, Any],
    field: str,
    pricing: dict[str, Any] | None,
    default_upcharge_bps: int,
    default_expected_units: Decimal,
) -> dict[str, Any]:
    explicit = selection.get("price_info")
    if isinstance(explicit, dict) and explicit.get("price") is not None:
        return _price_info(explicit, f"{field}.price_info")
    if pricing is None:
        raise DeploymentError(f"{field}.price_info is unresolved")
    upcharge_bps = default_upcharge_bps
    if "upcharge_bps" in selection:
        upcharge_bps = _non_negative_int(
            selection.get("upcharge_bps"), f"{field}.upcharge_bps"
        )
    expected_units = default_expected_units
    if "expected_units" in selection:
        expected_units = _positive_quantity(
            selection.get("expected_units"), f"{field}.expected_units"
        )
    return _derived_price_info(
        unit_price=pricing["unit_price"],
        expected_units=expected_units,
        upcharge_bps=upcharge_bps,
    )


def _route_model(
    root: Path, entry: dict[str, Any]
) -> tuple[RouteDefinition, str, str, dict[str, Any] | None]:
    try:
        route = RouteDefinition.from_index_entry(entry)
    except RuntimeError as error:
        raise DeploymentError(str(error)) from error
    paths = artifact_paths(root, route)
    document = _read_object(paths.route, f"route model {route.capability}")
    if document.get("format") != ROUTE_CONTRACT_FORMAT:
        raise DeploymentError(
            f"route model {route.capability} must use {ROUTE_CONTRACT_FORMAT}"
        )
    identity = document.get("identity")
    schema = document.get("schema")
    if not isinstance(identity, dict) or not isinstance(schema, dict):
        raise DeploymentError(f"route model {route.capability} is incomplete")
    expected_identity = {
        "app_id": route.app_id,
        "capability": route.capability,
        "endpoint_id": route.endpoint_id,
        "queue_app_id": route.queue_app_id,
    }
    if identity != expected_identity:
        raise DeploymentError(f"route model {route.capability} identity drift")
    digest = schema.get("sha256")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise DeploymentError(f"route model {route.capability} schema hash is invalid")
    if schema.get("path") != "./schema.json":
        raise DeploymentError(f"route model {route.capability} schema path is invalid")
    try:
        schema_bytes = paths.schema.read_bytes()
    except OSError as error:
        raise DeploymentError(
            f"could not read schema {paths.schema}: {error}"
        ) from error
    actual = hashlib.sha256(schema_bytes).hexdigest()
    if actual != digest:
        raise DeploymentError(
            f"route model {route.capability} schema hash does not match {paths.schema}"
        )
    try:
        schema_document = json.loads(schema_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeploymentError(f"schema {paths.schema} is not valid JSON") from error
    if (
        not isinstance(schema_document, dict)
        or schema_document.get("endpoint_id") != route.endpoint_id
    ):
        raise DeploymentError(
            f"schema {paths.schema} does not describe {route.endpoint_id!r}"
        )
    route_path = paths.route.relative_to(root).as_posix()
    return route, digest, route_path, _route_pricing(document, route.capability)


def _metadata(
    route: RouteDefinition, schema_sha256: str, deadline_seconds: int
) -> str:
    metadata = json.dumps(
        {
            "deadline_seconds": deadline_seconds,
            "endpoint_id": route.endpoint_id,
            "provider": "fal",
            "schema_sha256": schema_sha256,
            "schema_url": "./schema",
            "transport": "queue",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    size = len(metadata.encode())
    if size > METADATA_LIMIT_BYTES:
        raise DeploymentError(
            f"metadata for {route.capability!r} is {size} bytes; limit is "
            f"{METADATA_LIMIT_BYTES}"
        )
    return metadata


def build_artifacts(
    route_index_path: Path, deployment_path: Path
) -> dict[str, dict[str, Any]]:
    """Validate inputs and return deterministic deployment artifacts."""

    route_index_path = route_index_path.resolve()
    deployment_path = deployment_path.resolve()
    route_root = route_index_path.parent.parent
    index = _read_object(route_index_path, "route index")
    deployment = _read_object(deployment_path, "deployment")

    if deployment.get("format") != "livepeer.fal.deployment.v2":
        raise DeploymentError("deployment format must be livepeer.fal.deployment.v2")
    deployment_id = _selection_name(
        deployment.get("deployment_id"), "deployment.deployment_id"
    )
    if deployment.get("network") != "offchain":
        raise DeploymentError(
            "V2 generates local offchain deployments only; onchain pricing is unresolved"
        )
    pricing_scope = deployment.get("pricing_scope")
    if pricing_scope not in PRICING_SCOPES:
        raise DeploymentError(
            "deployment.pricing_scope must be one of "
            + ", ".join(sorted(PRICING_SCOPES))
        )
    default_upcharge_bps = DEFAULT_UPCHARGE_BPS
    if "upcharge_bps" in deployment:
        default_upcharge_bps = _non_negative_int(
            deployment.get("upcharge_bps"), "deployment.upcharge_bps"
        )
    default_expected_units = Decimal(DEFAULT_EXPECTED_UNITS)
    if "expected_units" in deployment:
        default_expected_units = _positive_quantity(
            deployment.get("expected_units"), "deployment.expected_units"
        )
    adapter_image = _required_string(
        deployment.get("adapter_image"), "deployment.adapter_image"
    )
    selections = deployment.get("routes")
    if not isinstance(selections, list) or not selections:
        raise DeploymentError("deployment.routes must be a non-empty array")
    entries = _route_entries(index)

    selected_capabilities: set[str] = set()
    app_ids: set[str] = set()
    labels: set[str] = set()
    services: set[str] = set()
    compose_services: dict[str, Any] = {}
    runners: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []

    for position, selection in enumerate(selections):
        field = f"deployment.routes[{position}]"
        if not isinstance(selection, dict):
            raise DeploymentError(f"{field} must be an object")
        capability = _required_string(
            selection.get("capability"), f"{field}.capability"
        )
        if capability in selected_capabilities:
            raise DeploymentError(f"duplicate selected capability {capability!r}")
        selected_capabilities.add(capability)
        entry = entries.get(capability)
        if entry is None:
            raise DeploymentError(f"selected capability {capability!r} is not modeled")

        route, schema_sha256, route_path, pricing = _route_model(route_root, entry)
        deadline_seconds = _positive_int(
            selection.get("deadline_seconds"), f"{field}.deadline_seconds"
        )
        max_request_bytes = _positive_int(
            selection.get("max_request_bytes"), f"{field}.max_request_bytes"
        )
        capacity = _positive_int(selection.get("capacity"), f"{field}.capacity")
        price_info = _selection_price_info(
            selection,
            field,
            pricing,
            default_upcharge_bps,
            default_expected_units,
        )
        service = _selection_name(selection.get("service"), f"{field}.service")
        label = _selection_name(selection.get("label"), f"{field}.label")

        if route.app_id in app_ids:
            raise DeploymentError(f"duplicate selected app_id {route.app_id!r}")
        if label in labels:
            raise DeploymentError(f"duplicate selected label {label!r}")
        if service in services:
            raise DeploymentError(f"duplicate selected service {service!r}")
        app_ids.add(route.app_id)
        labels.add(label)
        services.add(service)

        runner_url = f"http://{service}:8989"
        runners.append(
            {
                "app": route.app_id,
                "capacity": capacity,
                "health_url": f"{runner_url}/health",
                "healthy_status_code": 200,
                "label": label,
                "metadata": _metadata(route, schema_sha256, deadline_seconds),
                "mode": "single-shot",
                "price_info": price_info,
                "routing": "label",
                "runner_url": runner_url,
                "version": ROUTE_CONTRACT_FORMAT,
            }
        )
        compose_services[service] = {
            "build": {"context": "../../.."},
            "command": [
                f"--endpoint-key={capability}",
                f"--deadline-seconds={deadline_seconds}",
                f"--max-request-bytes={max_request_bytes}",
            ],
            "environment": ["FAL_KEY=${FAL_KEY:?set FAL_KEY in an ignored .env.local}"],
            "expose": ["8989"],
            "healthcheck": {
                "interval": "3s",
                "retries": 40,
                "start_period": "5s",
                "test": [
                    "CMD",
                    "python",
                    "-c",
                    (
                        "import urllib.request; "
                        "urllib.request.urlopen('http://127.0.0.1:8989/health', "
                        "timeout=2).read()"
                    ),
                ],
                "timeout": "3s",
            },
            "image": adapter_image,
            "pull_policy": "build",
        }
        resolved.append(
            {
                "app_id": route.app_id,
                "capability": capability,
                "capacity": capacity,
                "deadline_seconds": deadline_seconds,
                "endpoint_id": route.endpoint_id,
                "label": label,
                "max_request_bytes": max_request_bytes,
                "price_info": price_info,
                "route_model": route_path,
                "schema_sha256": schema_sha256,
                "service": service,
            }
        )

    compose_services["orchestrator"] = {
        "depends_on": {
            service: {"condition": "service_healthy"} for service in sorted(services)
        },
        "extends": {
            "file": "../../../../compose.orchestrator.static.yml",
            "service": "orchestrator",
        },
        "volumes": ["./runners.json:/config/runners.json:ro"],
    }

    return {
        "compose.json": {
            "name": f"livepeer-{deployment_id}",
            "services": dict(sorted(compose_services.items())),
        },
        "deployment.json": {
            "deployment_id": deployment_id,
            "format": "livepeer.fal.generated-deployment.v2",
            "network": "offchain",
            "pricing_scope": pricing_scope,
            "route_index_format": ROUTE_INDEX_FORMAT,
            "routes": resolved,
        },
        "runners.json": {"runners": runners},
    }


def _write_or_check(
    artifacts: dict[str, dict[str, Any]], output_dir: Path, *, check: bool
) -> bool:
    ok = True
    for filename, artifact in sorted(artifacts.items()):
        path = output_dir / filename
        expected = _json_bytes(artifact)
        if check:
            try:
                actual = path.read_bytes()
            except FileNotFoundError:
                actual = b""
            if actual != expected:
                print(f"drift: {path}", file=sys.stderr)
                ok = False
        else:
            output_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(expected)
    return ok


def main() -> int:
    args = _parse_args()
    artifacts = build_artifacts(args.route_index, args.deployment)
    output_dir = args.deployment.resolve().parent / "generated"
    if not _write_or_check(artifacts, output_dir, check=args.check):
        print(f"run: python {Path(__file__).name} {args.deployment}", file=sys.stderr)
        return 1
    print(
        f"deployment={artifacts['deployment.json']['deployment_id']} "
        f"routes={len(artifacts['deployment.json']['routes'])}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, ValueError, DeploymentError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
