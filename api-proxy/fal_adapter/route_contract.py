"""Generate one self-contained Live Runner route model."""

from __future__ import annotations

import json
from typing import Any

from .contract import RouteDefinition

ROUTE_CONTRACT_FORMAT = "livepeer.fal.runner-route.v1"
_COMPONENT_PREFIX = "#/components/schemas/"
_SCALAR_TYPES = {"string", "integer", "number", "boolean", "object"}


def _component_name(ref: str) -> str:
    if not ref.startswith(_COMPONENT_PREFIX) or not ref[len(_COMPONENT_PREFIX) :]:
        return ""
    return ref[len(_COMPONENT_PREFIX) :]


def _resolve_schema(schema: dict[str, Any], components: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    name = _component_name(ref)
    resolved = components.get(name)
    if not isinstance(resolved, dict):
        return schema
    return resolved


def _compact_field(schema: dict[str, Any], components: dict[str, Any]) -> dict[str, str]:
    resolved = _resolve_schema(schema, components)
    options = resolved.get("anyOf") or resolved.get("oneOf")
    if isinstance(options, list):
        non_null = [
            option
            for option in options
            if isinstance(option, dict) and option.get("type") != "null"
        ]
        if len(non_null) == 1:
            return _compact_field(non_null[0], components)
        return {"type": "object"}
    json_type = resolved.get("type")
    if json_type == "array":
        items = resolved.get("items")
        if isinstance(items, dict):
            return {"items": _compact_field(items, components)["type"], "type": "array"}
        return {"type": "array"}
    if json_type in _SCALAR_TYPES:
        return {"type": json_type}
    if "$ref" in resolved:
        return {"type": "object"}
    return {"type": "object"}


def compact_input_summary(schema_document: dict[str, Any]) -> dict[str, Any]:
    """Project top-level input required/fields without nested component dumps."""

    components_root = schema_document.get("components")
    schemas = {}
    if isinstance(components_root, dict):
        raw_schemas = components_root.get("schemas")
        if isinstance(raw_schemas, dict):
            schemas = raw_schemas
    input_schema = schema_document.get("input_schema")
    if not isinstance(input_schema, dict):
        return {"fields": {}, "required": []}
    resolved = _resolve_schema(input_schema, schemas)
    properties = resolved.get("properties")
    fields: dict[str, dict[str, str]] = {}
    if isinstance(properties, dict):
        for name, spec in properties.items():
            if isinstance(name, str) and isinstance(spec, dict):
                fields[name] = _compact_field(spec, schemas)
    required_raw = resolved.get("required")
    required = [
        name for name in required_raw if isinstance(name, str)
    ] if isinstance(required_raw, list) else []
    return {"fields": fields, "required": required}


def route_contract(
    entry: dict[str, Any],
    route: RouteDefinition,
    schema_sha256: str,
    inputs: dict[str, Any] | None = None,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project source identity into the exact model an operator can deploy."""

    summary = inputs if isinstance(inputs, dict) else {"fields": {}, "required": []}
    fields = summary.get("fields")
    required = summary.get("required")
    document: dict[str, Any] = {
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
        "inputs": {
            "fields": fields if isinstance(fields, dict) else {},
            "required": required if isinstance(required, list) else [],
        },
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
    if pricing is not None:
        document["pricing"] = pricing
    return document


def route_contract_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
