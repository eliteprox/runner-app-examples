#!/usr/bin/env python3
"""Generate, refresh, or verify 73 deterministic fal Runner route models.

``--refresh`` is the only mode that performs network I/O. ``--generate`` and
``--check`` read only the route index and committed OpenAPI snapshots. Route
models describe how an adapter instance is pinned; deployment policy remains in
an explicit deployment selection.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import generate_schema
from fal_adapter import (
    DEFAULT_ROUTE_INDEX_PATH,
    ROUTE_INDEX_FORMAT,
    RouteDefinition,
    artifact_paths,
    compact_input_summary,
    route_contract,
    route_contract_bytes,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_REPORT_PATH = ROOT / "contracts" / "report.json"
INVENTORY_FILENAME = "INVENTORY.md"
REPORT_FORMAT = "livepeer.fal.contract-report.v3"
CONTRACT_AUTHORITY = "fal"
CONTRACT_KIND = "queue_openapi"
DESCRIPTOR_SELECTION = "livepeer_curated"
OFFICIAL_SOURCE_URL_TEMPLATE = (
    "https://fal.ai/api/openapi/queue/openapi.json?endpoint_id={endpoint_id}"
)
FAL_PRICING_API = "https://api.fal.ai/v1/models/pricing"
MAX_OPENAPI_BYTES = 10 * 1024 * 1024
MAX_PRICING_BYTES = 1 * 1024 * 1024
REFRESH_RETRIES = 3


class FleetError(RuntimeError):
    """Raised for an invalid route index or fleet-level operation."""


@dataclass(frozen=True, slots=True)
class BuiltPack:
    """Validated deterministic bytes for one route model."""

    route_bytes: bytes
    snapshot_bytes: bytes
    schema_bytes: bytes
    schema_sha256: str
    components: int


@dataclass(frozen=True, slots=True)
class RouteItem:
    """Source index entry paired with its validated route identity."""

    entry: dict[str, Any]
    route: RouteDefinition


FetchDocument = Callable[[RouteDefinition], dict[str, Any]]
FetchPricing = Callable[[RouteDefinition], dict[str, Any] | None]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate, refresh, or check fal Runner route models."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--refresh",
        action="store_true",
        help="fetch official OpenAPI and rewrite selected route models",
    )
    mode.add_argument(
        "--generate",
        action="store_true",
        help="offline-regenerate every model from committed snapshots",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="perform a fully offline drift and integrity check",
    )
    parser.add_argument("--route-index", type=Path, default=DEFAULT_ROUTE_INDEX_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument(
        "--route",
        action="append",
        dest="routes",
        help="route capability to refresh; repeatable (default: all routes)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=6,
        help="parallel official OpenAPI requests in --refresh mode (default: 6)",
    )
    return parser.parse_args()


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except FileNotFoundError as error:
        raise FleetError(f"missing {label}: {path}") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FleetError(f"could not read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise FleetError(f"{label} must be a JSON object")
    return value


def _route_items(route_index_path: Path) -> tuple[dict[str, Any], list[RouteItem]]:
    index = _read_object(route_index_path, "route index")
    if index.get("format") != ROUTE_INDEX_FORMAT:
        raise FleetError(f"route index must use {ROUTE_INDEX_FORMAT}")
    entries = index.get("routes")
    if not isinstance(entries, list) or not entries:
        raise FleetError("route index routes must be a non-empty array")

    items: list[RouteItem] = []
    capabilities: set[str] = set()
    endpoints: set[str] = set()
    app_ids: set[str] = set()
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise FleetError(f"route index routes[{position}] must be an object")
        route = RouteDefinition.from_index_entry(entry)
        identities = (
            (capabilities, route.capability, "capability"),
            (endpoints, route.endpoint_id, "endpoint_id"),
            (app_ids, route.app_id, "app_id"),
        )
        for seen, value, label in identities:
            if value in seen:
                raise FleetError(f"route index repeats {label} {value!r}")
            seen.add(value)
        items.append(RouteItem(entry=entry, route=route))
    return index, items


def _inventory_path(root: Path) -> Path:
    return root / "routes" / INVENTORY_FILENAME


def _markdown_code(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`")
    return f"`{escaped}`"


def _inventory_bytes(items: list[RouteItem]) -> bytes:
    """Render a deterministic reviewer view of the canonical route index."""

    lines = [
        "# Route inventory",
        "",
        "> Generated from [`index.json`](index.json) by `generate_contracts.py`.",
        "> Regenerate with `uv run python generate_contracts.py --generate`.",
        "",
        "This index lists 73 modeled route contracts. Each capability links to its",
        "generated `route.json`; the schema and OpenAPI snapshot are stored beside it.",
        "",
    ]
    for position, item in enumerate(items, start=1):
        capability = item.route.capability
        contract_root = f"../contracts/{capability}"
        lines.append(
            f"{position}. [{_markdown_code(capability)}]({contract_root}/route.json)"
        )
    return ("\n".join(lines) + "\n").encode()


def _report_bytes(report: dict[str, Any]) -> bytes:
    return (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
    try:
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _fetch_official_openapi(route: RouteDefinition) -> dict[str, Any]:
    """Fetch one official document with bounded retries and response size."""

    request = urllib.request.Request(
        generate_schema._openapi_url(route),
        headers={
            "Accept": "application/json",
            "User-Agent": "livepeer-fal-runner-routes/1",
        },
    )
    last_error: Exception | None = None
    for attempt in range(REFRESH_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise FleetError(f"official OpenAPI returned HTTP {status}")
                raw = response.read(MAX_OPENAPI_BYTES + 1)
            if len(raw) > MAX_OPENAPI_BYTES:
                raise FleetError(f"official OpenAPI exceeds {MAX_OPENAPI_BYTES} bytes")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise FleetError("official OpenAPI must be a JSON object")
            return value
        except (
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            urllib.error.HTTPError,
            urllib.error.URLError,
            FleetError,
        ) as error:
            last_error = error
            retryable = not isinstance(error, urllib.error.HTTPError) or (
                error.code == 429 or 500 <= error.code <= 599
            )
            if not retryable or attempt + 1 == REFRESH_RETRIES:
                break
            time.sleep(0.5 * (2**attempt))
    raise FleetError(f"could not fetch official OpenAPI: {last_error}")


def _decimal_string(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        raise FleetError("unit_price must be a positive decimal")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as error:
        raise FleetError("unit_price must be a positive decimal") from error
    if number <= 0:
        raise FleetError("unit_price must be a positive decimal")
    formatted = format(number, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted or "0"


def normalize_pricing(value: Any, endpoint_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FleetError("pricing must be an object")
    unit = value.get("unit")
    currency = value.get("currency")
    fetched_at = value.get("fetched_at")
    provider = value.get("provider", "fal")
    priced_endpoint = value.get("endpoint_id", endpoint_id)
    if not isinstance(unit, str) or not unit.strip():
        raise FleetError("pricing.unit is required")
    if not isinstance(currency, str) or not currency.strip():
        raise FleetError("pricing.currency is required")
    if not isinstance(fetched_at, str) or not fetched_at.strip():
        raise FleetError("pricing.fetched_at is required")
    if not isinstance(provider, str) or not provider.strip():
        raise FleetError("pricing.provider is required")
    if not isinstance(priced_endpoint, str) or not priced_endpoint.strip():
        raise FleetError("pricing.endpoint_id is required")
    if priced_endpoint.strip() != endpoint_id:
        raise FleetError(
            f"pricing.endpoint_id {priced_endpoint!r} does not match {endpoint_id!r}"
        )
    return {
        "currency": currency.strip().lower(),
        "endpoint_id": endpoint_id,
        "fetched_at": fetched_at.strip(),
        "provider": provider.strip(),
        "unit": unit.strip(),
        "unit_price": _decimal_string(value.get("unit_price")),
    }


def parse_fal_pricing(endpoint_id: str, body: bytes, *, fetched_at: str) -> dict[str, Any]:
    try:
        raw = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FleetError(f"fal pricing was not valid JSON: {error}") from error
    if not isinstance(raw, dict):
        raise FleetError("fal pricing must be a JSON object")
    prices = raw.get("prices")
    if isinstance(prices, list):
        match = None
        for row in prices:
            if isinstance(row, dict) and row.get("endpoint_id") == endpoint_id:
                match = row
                break
        if match is None and len(prices) == 1 and isinstance(prices[0], dict):
            match = prices[0]
        if match is None:
            raise FleetError(f"fal pricing missing endpoint {endpoint_id!r}")
        raw = match
    unit = raw.get("unit") or raw.get("unit_type")
    price = raw.get("unit_price", raw.get("price", raw.get("unitPrice")))
    currency = raw.get("currency") or "usd"
    priced_endpoint = raw.get("endpoint_id") or endpoint_id
    return normalize_pricing(
        {
            "currency": currency,
            "endpoint_id": priced_endpoint,
            "fetched_at": fetched_at,
            "provider": "fal",
            "unit": unit,
            "unit_price": price,
        },
        endpoint_id,
    )


def _fal_api_key() -> str:
    return os.environ.get("FAL_KEY", "").strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_fal_pricing_body(endpoint_ids: list[str]) -> bytes:
    key = _fal_api_key()
    if not key:
        raise FleetError("FAL_KEY is required to fetch fal pricing")
    query = urllib.parse.urlencode({"endpoint_id": ",".join(endpoint_ids)})
    request = urllib.request.Request(
        f"{FAL_PRICING_API}?{query}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Key {key}",
            "User-Agent": "livepeer-fal-runner-routes/1",
        },
    )
    last_error: Exception | None = None
    for attempt in range(REFRESH_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise FleetError(f"fal pricing returned HTTP {status}")
                raw = response.read(MAX_PRICING_BYTES + 1)
            if len(raw) > MAX_PRICING_BYTES:
                raise FleetError(f"fal pricing exceeds {MAX_PRICING_BYTES} bytes")
            return raw
        except (
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            urllib.error.HTTPError,
            urllib.error.URLError,
            FleetError,
        ) as error:
            last_error = error
            retryable = not isinstance(error, urllib.error.HTTPError) or (
                error.code == 429 or 500 <= error.code <= 599
            )
            if not retryable or attempt + 1 == REFRESH_RETRIES:
                break
            time.sleep(2.0 * (2**attempt))
    raise FleetError(f"could not fetch fal pricing: {last_error}")


def _fetch_fal_pricing(route: RouteDefinition) -> dict[str, Any] | None:
    if not _fal_api_key():
        return None
    return parse_fal_pricing(
        route.endpoint_id,
        _fetch_fal_pricing_body([route.endpoint_id]),
        fetched_at=_utc_now(),
    )


def fetch_fal_pricing_map(items: list[RouteItem], *, batch_size: int = 50) -> dict[str, Any]:
    """Fetch catalog prices in batches of up to 50 endpoint IDs."""

    fetched_at = _utc_now()
    priced: dict[str, Any] = {}
    pending = list(items)
    while pending:
        batch = pending[:batch_size]
        pending = pending[batch_size:]
        try:
            raw = _fetch_fal_pricing_body([item.route.endpoint_id for item in batch])
        except Exception as error:
            for item in batch:
                priced[item.route.capability] = error
            continue
        for item in batch:
            try:
                priced[item.route.capability] = parse_fal_pricing(
                    item.route.endpoint_id, raw, fetched_at=fetched_at
                )
            except Exception as error:
                priced[item.route.capability] = error
    return priced


def _committed_pricing(root: Path, item: RouteItem) -> dict[str, Any] | None:
    path = artifact_paths(root, item.route).route
    try:
        document = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or "pricing" not in document:
        return None
    return normalize_pricing(document["pricing"], item.route.endpoint_id)


def _resolve_pricing(
    root: Path,
    item: RouteItem,
    fetched: dict[str, Any] | Exception | None,
) -> dict[str, Any] | None:
    if isinstance(fetched, Exception):
        return _committed_pricing(root, item)
    if isinstance(fetched, dict):
        return normalize_pricing(fetched, item.route.endpoint_id)
    return _committed_pricing(root, item)


def _build_pack(
    document: dict[str, Any],
    item: RouteItem,
    pricing: dict[str, Any] | None = None,
) -> BuiltPack:
    bundle = generate_schema._build_schema_bundle(document, item.route)
    snapshot_bytes = generate_schema._pretty_json(document)
    schema_bytes = generate_schema._canonical_json(bundle)
    schema_sha256 = hashlib.sha256(schema_bytes).hexdigest()
    return BuiltPack(
        route_bytes=route_contract_bytes(
            route_contract(
                item.entry,
                item.route,
                schema_sha256,
                compact_input_summary(bundle),
                pricing,
            )
        ),
        snapshot_bytes=snapshot_bytes,
        schema_bytes=schema_bytes,
        schema_sha256=schema_sha256,
        components=len(bundle["components"]["schemas"]),
    )


def _paths_for_report(root: Path, route: RouteDefinition) -> tuple[str, str, str]:
    paths = artifact_paths(root, route)
    return (
        paths.snapshot.relative_to(root).as_posix(),
        paths.route.relative_to(root).as_posix(),
        paths.schema.relative_to(root).as_posix(),
    )


def _verified_result(root: Path, item: RouteItem, pack: BuiltPack) -> dict[str, Any]:
    snapshot_path, route_path, schema_path = _paths_for_report(root, item.route)
    return {
        "artifacts": {
            "openapi_bytes": len(pack.snapshot_bytes),
            "openapi_path": snapshot_path,
            "openapi_sha256": hashlib.sha256(pack.snapshot_bytes).hexdigest(),
            "route_bytes": len(pack.route_bytes),
            "route_path": route_path,
            "route_sha256": hashlib.sha256(pack.route_bytes).hexdigest(),
            "schema_bytes": len(pack.schema_bytes),
            "schema_path": schema_path,
            "schema_sha256": pack.schema_sha256,
        },
        "capability": item.route.capability,
        "components": pack.components,
        "endpoint_id": item.route.endpoint_id,
        "status": "snapshot_verified",
    }


def _failed_result(item: RouteItem, error: Exception) -> dict[str, Any]:
    return {
        "capability": item.route.capability,
        "endpoint_id": item.route.endpoint_id,
        "error": {
            "message": str(error),
            "type": type(error).__name__,
        },
        "status": "failed",
    }


def _report(index: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = sum(result["status"] == "failed" for result in results)
    return {
        "format": REPORT_FORMAT,
        "provenance": {
            "contract_authority": CONTRACT_AUTHORITY,
            "contract_kind": CONTRACT_KIND,
            "descriptor_selection": DESCRIPTOR_SELECTION,
            "source_url_template": OFFICIAL_SOURCE_URL_TEMPLATE,
            "verification_scope": (
                "committed OpenAPI identity, queue operations, schema projection, "
                "and generated route bytes; not provider execution or availability"
            ),
            "verified_contract_fields": [
                "endpoint_identity",
                "submit_path",
                "status_path",
                "result_path",
                "cancel_path",
                "input_schema",
                "output_schema",
                "transitive_components",
            ],
        },
        "results": results,
        "route_index_format": index["format"],
        "summary": {
            "failed": failed,
            "snapshots_verified": len(results) - failed,
            "total": len(results),
        },
    }


def _write_pack(root: Path, item: RouteItem, pack: BuiltPack) -> None:
    paths = artifact_paths(root, item.route)
    _atomic_write(paths.snapshot, pack.snapshot_bytes)
    _atomic_write(paths.route, pack.route_bytes)
    _atomic_write(paths.schema, pack.schema_bytes)
    if paths.snapshot.read_bytes() != pack.snapshot_bytes:
        raise FleetError(f"snapshot write verification failed: {paths.snapshot}")
    if paths.schema.read_bytes() != pack.schema_bytes:
        raise FleetError(f"schema write verification failed: {paths.schema}")
    if paths.route.read_bytes() != pack.route_bytes:
        raise FleetError(f"route model write verification failed: {paths.route}")


def _pack_from_disk(root: Path, item: RouteItem) -> BuiltPack:
    paths = artifact_paths(root, item.route)
    try:
        snapshot_bytes = paths.snapshot.read_bytes()
        route_bytes = paths.route.read_bytes()
        schema_bytes = paths.schema.read_bytes()
    except OSError as error:
        raise FleetError(f"missing route pack for {item.route.capability}") from error
    try:
        document = json.loads(snapshot_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FleetError(
            f"invalid OpenAPI snapshot {paths.snapshot}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise FleetError(f"OpenAPI snapshot {paths.snapshot} must be an object")
    expected = _build_pack(document, item, pricing=_committed_pricing(root, item))
    if snapshot_bytes != expected.snapshot_bytes:
        raise FleetError(f"snapshot drift: {paths.snapshot}")
    if schema_bytes != expected.schema_bytes:
        raise FleetError(f"schema drift: {paths.schema}")
    if route_bytes != expected.route_bytes:
        raise FleetError(f"route model drift: {paths.route}")
    return expected


def _fetch_selected(
    items: list[RouteItem], fetch: Callable[[RouteDefinition], Any], jobs: int
) -> dict[str, Any]:
    if jobs <= 0:
        raise FleetError("--jobs must be positive")
    fetched: dict[str, Any] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        pending = {
            executor.submit(fetch, item.route): item.route.capability
            for item in items
        }
        for future in concurrent.futures.as_completed(pending):
            capability = pending[future]
            try:
                fetched[capability] = future.result()
            except Exception as error:  # result is recorded, not discarded
                fetched[capability] = error
    return fetched


def refresh_routes(
    *,
    route_index_path: Path = DEFAULT_ROUTE_INDEX_PATH,
    report_path: Path = DEFAULT_REPORT_PATH,
    root: Path = ROOT,
    fetch_document: FetchDocument = _fetch_official_openapi,
    fetch_pricing: FetchPricing | None = None,
    selected_capabilities: list[str] | None = None,
    jobs: int = 6,
) -> int:
    """Refresh selected official snapshots and verify every committed route."""

    index, items = _route_items(route_index_path)
    known = {item.route.capability for item in items}
    selected = set(selected_capabilities or known)
    unknown = sorted(selected - known)
    if unknown:
        raise FleetError(f"unknown --route values: {', '.join(unknown)}")
    selected_items = [item for item in items if item.route.capability in selected]
    fetched = _fetch_selected(selected_items, fetch_document, jobs)
    if fetch_pricing is None:
        priced = {item.route.capability: None for item in selected_items}
    elif fetch_pricing is _fetch_fal_pricing:
        priced = fetch_fal_pricing_map(selected_items)
    else:
        priced = _fetch_selected(selected_items, fetch_pricing, jobs)
    results: list[dict[str, Any]] = []

    for item in items:
        if item.route.capability not in selected:
            try:
                results.append(
                    _verified_result(root, item, _pack_from_disk(root, item))
                )
            except Exception as error:
                results.append(_failed_result(item, error))
            continue
        fetched_value = fetched[item.route.capability]
        if isinstance(fetched_value, Exception):
            results.append(_failed_result(item, fetched_value))
            continue
        try:
            pricing = _resolve_pricing(root, item, priced.get(item.route.capability))
            pack = _build_pack(fetched_value, item, pricing=pricing)
            _write_pack(root, item, pack)
            results.append(_verified_result(root, item, _pack_from_disk(root, item)))
        except Exception as error:
            results.append(_failed_result(item, error))

    report = _report(index, results)
    _atomic_write(report_path, _report_bytes(report))
    _atomic_write(_inventory_path(root), _inventory_bytes(items))
    summary = report["summary"]
    print(
        f"routes total={summary['total']} "
        f"snapshots_verified={summary['snapshots_verified']} "
        f"failed={summary['failed']} refreshed={len(selected_items)}"
    )
    return 1 if summary["failed"] else 0


def stamp_committed_pricing(
    *,
    route_index_path: Path = DEFAULT_ROUTE_INDEX_PATH,
    root: Path = ROOT,
    fetch_pricing: FetchPricing,
    jobs: int = 6,
) -> dict[str, int]:
    """Write fetched pricing onto committed route models without touching OpenAPI."""

    _, items = _route_items(route_index_path)
    if fetch_pricing is _fetch_fal_pricing:
        priced = fetch_fal_pricing_map(items)
    else:
        priced = _fetch_selected(items, fetch_pricing, jobs)
    written = 0
    missing = 0
    failed = 0
    for item in items:
        fetched = priced.get(item.route.capability)
        if isinstance(fetched, Exception):
            failed += 1
            continue
        pricing = _resolve_pricing(root, item, fetched)
        if pricing is None:
            missing += 1
            continue
        paths = artifact_paths(root, item.route)
        try:
            document = json.loads(paths.snapshot.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            failed += 1
            continue
        if not isinstance(document, dict):
            failed += 1
            continue
        pack = _build_pack(document, item, pricing=pricing)
        _write_pack(root, item, pack)
        written += 1
    return {
        "failed": failed,
        "missing": missing,
        "total": len(items),
        "written": written,
    }


def generate_routes(
    *,
    route_index_path: Path = DEFAULT_ROUTE_INDEX_PATH,
    report_path: Path = DEFAULT_REPORT_PATH,
    root: Path = ROOT,
) -> int:
    """Offline-regenerate complete route packs from committed snapshots."""

    index, items = _route_items(route_index_path)
    results: list[dict[str, Any]] = []
    for item in items:
        paths = artifact_paths(root, item.route)
        try:
            document = json.loads(paths.snapshot.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FleetError(
                f"could not read snapshot for {item.route.capability}: {error}"
            ) from error
        if not isinstance(document, dict):
            raise FleetError(f"snapshot {paths.snapshot} must be a JSON object")
        pack = _build_pack(document, item, pricing=_committed_pricing(root, item))
        _write_pack(root, item, pack)
        results.append(_verified_result(root, item, _pack_from_disk(root, item)))

    report = _report(index, results)
    _atomic_write(report_path, _report_bytes(report))
    _atomic_write(_inventory_path(root), _inventory_bytes(items))
    summary = report["summary"]
    print(
        f"routes total={summary['total']} "
        f"snapshots_verified={summary['snapshots_verified']} "
        f"failed={summary['failed']} generated=ok"
    )
    return 0


def check_routes(
    *,
    route_index_path: Path = DEFAULT_ROUTE_INDEX_PATH,
    report_path: Path = DEFAULT_REPORT_PATH,
    root: Path = ROOT,
) -> int:
    """Regenerate every route model and report without network access."""

    index, items = _route_items(route_index_path)
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in items:
        try:
            results.append(_verified_result(root, item, _pack_from_disk(root, item)))
        except Exception as error:
            errors.append(str(error))
    if errors:
        for error in errors:
            print(f"drift: {error}", file=sys.stderr)
        return 1

    expected = _report_bytes(_report(index, results))
    try:
        actual = report_path.read_bytes()
    except OSError:
        actual = b""
    if actual != expected:
        print(f"drift: {report_path}", file=sys.stderr)
        return 1
    inventory_path = _inventory_path(root)
    try:
        actual_inventory = inventory_path.read_bytes()
    except OSError:
        actual_inventory = b""
    if actual_inventory != _inventory_bytes(items):
        print(f"drift: {inventory_path}", file=sys.stderr)
        return 1
    summary = json.loads(expected)["summary"]
    print(
        f"routes total={summary['total']} "
        f"snapshots_verified={summary['snapshots_verified']} "
        f"failed={summary['failed']} offline_check=ok"
    )
    return 0


def main() -> int:
    args = _parse_args()
    if args.refresh:
        return refresh_routes(
            route_index_path=args.route_index,
            report_path=args.report,
            selected_capabilities=args.routes,
            jobs=args.jobs,
            fetch_pricing=_fetch_fal_pricing,
        )
    if args.routes:
        raise FleetError("--route is valid only with --refresh")
    if args.generate:
        return generate_routes(
            route_index_path=args.route_index,
            report_path=args.report,
        )
    return check_routes(route_index_path=args.route_index, report_path=args.report)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, ValueError, FleetError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
