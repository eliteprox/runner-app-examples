"""Generate one self-contained Live Runner route model."""

from __future__ import annotations

import json
from typing import Any

from .contract import RouteDefinition

ROUTE_CONTRACT_FORMAT = "livepeer.fal.runner-route.v1"


def route_contract(
    entry: dict[str, Any], route: RouteDefinition, schema_sha256: str
) -> dict[str, Any]:
    """Project source identity into the exact model an operator can deploy."""

    return {
        "credential": {
            "authorization_scheme": "Key",
            "environment_variable": "FAL_KEY",
            "owner": "operator",
        },
        "format": ROUTE_CONTRACT_FORMAT,
        "identity": {
            "app_id": route.app_id,
            "capability": route.capability,
            "endpoint_id": route.endpoint_id,
            "queue_app_id": route.queue_app_id,
        },
        "input_policy": entry["input_policy"],
        "runner": {
            "mode": "single-shot",
            "routes": {
                "execute": "./",
                "health": "./health",
                "schema": "./schema",
            },
            "transport_profile": route.transport_profile,
        },
        "schema": {
            "path": "./schema.json",
            "sha256": schema_sha256,
        },
    }


def route_contract_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
