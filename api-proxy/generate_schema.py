#!/usr/bin/env python3
"""Generate one pinned fal endpoint schema from a committed OpenAPI snapshot.

The network is touched only with ``--refresh``. Normal generation and ``--check``
read the committed OpenAPI snapshot so CI and local verification are hermetic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

from fal_adapter import (
    DEFAULT_ROUTE_INDEX_PATH,
    RouteDefinition,
    artifact_paths,
    load_route_definition,
)

DEFAULT_ENDPOINT_KEY = "flux-schnell"
ROOT = Path(__file__).resolve().parent


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a pinned fal endpoint schema bundle."
    )
    parser.add_argument(
        "--endpoint-key",
        default=DEFAULT_ENDPOINT_KEY,
        help="route capability whose artifacts should be generated",
    )
    parser.add_argument(
        "--route-index",
        type=Path,
        default=DEFAULT_ROUTE_INDEX_PATH,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="fetch and replace the committed fal OpenAPI snapshot",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; fail if generated files differ from committed files",
    )
    return parser.parse_args()


def _pretty_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def _openapi_url(descriptor: RouteDefinition) -> str:
    return (
        "https://fal.ai/api/openapi/queue/openapi.json?endpoint_id="
        f"{descriptor.endpoint_id}"
    )


def _load_snapshot(
    refresh: bool, descriptor: RouteDefinition, snapshot_path: Path
) -> dict[str, Any]:
    if refresh:
        request = urllib.request.Request(
            _openapi_url(descriptor),
            headers={
                "Accept": "application/json",
                "User-Agent": "livepeer-runner-example",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
        document = json.loads(raw)
    else:
        if not snapshot_path.exists():
            raise RuntimeError(
                f"missing {snapshot_path}; run generate_schema.py "
                f"--endpoint-key {descriptor.capability} --refresh"
            )
        document = json.loads(snapshot_path.read_bytes())

    if not isinstance(document, dict):
        raise TypeError("fal OpenAPI document must be a JSON object")
    return document


def _schema_at(document: dict[str, Any], *path: str) -> dict[str, Any]:
    current: Any = document
    for key in path:
        if not isinstance(current, dict) or key not in current:
            raise RuntimeError(f"fal OpenAPI missing {'/'.join(path)}")
        current = current[key]
    if not isinstance(current, dict):
        raise TypeError(f"fal OpenAPI {'/'.join(path)} must be an object")
    return current


def _schema_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str):
                refs.add(child)
            else:
                refs.update(_schema_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.update(_schema_refs(child))
    return refs


def _component_name(ref: str) -> str:
    prefix = "#/components/schemas/"
    if not ref.startswith(prefix) or not ref[len(prefix) :]:
        raise RuntimeError(f"unsupported OpenAPI reference {ref!r}")
    return ref[len(prefix) :]


def _transitive_components(
    roots: list[dict[str, Any]], all_schemas: dict[str, Any]
) -> dict[str, Any]:
    pending = sorted({ref for root in roots for ref in _schema_refs(root)})
    selected: dict[str, Any] = {}

    while pending:
        ref = pending.pop(0)
        name = _component_name(ref)
        if name in selected:
            continue
        schema = all_schemas.get(name)
        if not isinstance(schema, dict):
            raise TypeError(f"fal OpenAPI reference {ref!r} is missing")
        selected[name] = schema
        pending.extend(sorted(_schema_refs(schema)))

    return {name: selected[name] for name in sorted(selected)}


def _validate_queue_contract(
    document: dict[str, Any], descriptor: RouteDefinition
) -> None:
    """Require the complete queue surface before projecting any schemas."""

    metadata = _schema_at(document, "info", "x-fal-metadata")
    advertised_endpoint = metadata.get("endpointId")
    if advertised_endpoint != descriptor.endpoint_id:
        raise RuntimeError(
            f"fal OpenAPI endpoint mismatch: {advertised_endpoint!r} != "
            f"{descriptor.endpoint_id!r}"
        )

    endpoint_path = f"/{descriptor.endpoint_id}"
    lifecycle = (
        (endpoint_path, "post", "submit"),
        (
            f"{endpoint_path}/requests/{{request_id}}/status",
            "get",
            "status",
        ),
        (f"{endpoint_path}/requests/{{request_id}}", "get", "result"),
        (
            f"{endpoint_path}/requests/{{request_id}}/cancel",
            "put",
            "cancel",
        ),
    )
    for path, method, stage in lifecycle:
        try:
            _schema_at(document, "paths", path, method)
        except (RuntimeError, TypeError) as error:
            raise RuntimeError(
                f"fal OpenAPI missing {stage} operation {method.upper()} {path}"
            ) from error


def _build_schema_bundle(
    document: dict[str, Any], descriptor: RouteDefinition
) -> dict[str, Any]:
    _validate_queue_contract(document, descriptor)
    endpoint_path = f"/{descriptor.endpoint_id}"
    result_path = f"/{descriptor.endpoint_id}/requests/{{request_id}}"
    input_schema = _schema_at(
        document,
        "paths",
        endpoint_path,
        "post",
        "requestBody",
        "content",
        "application/json",
        "schema",
    )
    output_schema = _schema_at(
        document,
        "paths",
        result_path,
        "get",
        "responses",
        "200",
        "content",
        "application/json",
        "schema",
    )
    all_schemas = _schema_at(document, "components", "schemas")

    return {
        "components": {
            "schemas": _transitive_components(
                [input_schema, output_schema], all_schemas
            )
        },
        "endpoint_id": descriptor.endpoint_id,
        "format": "livepeer.fal.endpoint-schema.v1",
        "input_schema": input_schema,
        "output_schema": output_schema,
        "source": {
            "openapi_url": _openapi_url(descriptor),
            "openapi_version": document.get("openapi", ""),
        },
    }


def _check_file(path: Path, expected: bytes) -> bool:
    actual = path.read_bytes() if path.exists() else b""
    if actual == expected:
        return True
    print(f"drift: {path}", file=sys.stderr)
    return False


def main() -> int:
    args = _parse_args()
    if args.refresh and args.check:
        raise RuntimeError("--refresh and --check cannot be used together")

    descriptor = load_route_definition(
        args.endpoint_key, route_index_path=args.route_index
    )
    paths = artifact_paths(ROOT, descriptor)
    document = _load_snapshot(args.refresh, descriptor, paths.snapshot)
    snapshot_bytes = _pretty_json(document)
    schema_bytes = _canonical_json(_build_schema_bundle(document, descriptor))
    schema_sha256 = hashlib.sha256(schema_bytes).hexdigest()

    if args.check:
        ok = _check_file(paths.schema, schema_bytes)
        if not ok:
            print(
                f"run: python api-proxy/generate_schema.py "
                f"--endpoint-key {descriptor.capability}",
                file=sys.stderr,
            )
            return 1
    else:
        paths.snapshot.parent.mkdir(parents=True, exist_ok=True)
        paths.schema.parent.mkdir(parents=True, exist_ok=True)
        if args.refresh:
            paths.snapshot.write_bytes(snapshot_bytes)
        paths.schema.write_bytes(schema_bytes)

    print(
        f"endpoint={descriptor.capability} schema_sha256={schema_sha256} components="
        f"{len(json.loads(schema_bytes)['components']['schemas'])}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
