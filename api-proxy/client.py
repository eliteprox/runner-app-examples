#!/usr/bin/env python3
"""Inspect or call one modeled fal route through Livepeer discovery."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.selection import runner_selector

from fal_adapter.route_client import (
    RouteContractError,
    load_runner_route,
    run_route_arguments,
)

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect or call a pinned fal Live Runner route."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    describe = commands.add_parser("describe", help="print one generated route model")
    describe.add_argument("capability")

    run = commands.add_parser("run", help="discover and call one modeled route")
    run.add_argument("capability")
    run.add_argument("--input-json", type=Path, required=True)
    run.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    run.add_argument("--signer", default="", help="remote signer base URL")
    run.add_argument(
        "--timeout",
        type=float,
        default=210.0,
        help="client deadline in seconds (default: 210)",
    )
    run.add_argument("--receipt-output", type=Path)
    run.add_argument(
        "--insecure",
        action="store_true",
        help="disable TLS certificate checks for the local example only",
    )
    return parser.parse_args()


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


async def _run(args: argparse.Namespace) -> None:
    route = load_runner_route(args.capability)
    try:
        arguments_bytes = args.input_json.expanduser().read_bytes()
    except OSError as error:
        raise RouteContractError(f"could not read --input-json: {error}") from error

    cursor = await runner_selector(
        discovery_url=args.discovery,
        app=route.app_id,
    )
    try:
        discovered = cursor.candidates[0]
    except IndexError as error:
        raise RouteContractError(
            f"discovery returned no Runner for {route.app_id}"
        ) from error

    ssl = False if args.insecure else None
    async with aiohttp.ClientSession() as session:
        receipt = await run_route_arguments(
            session,
            runner=discovered,
            route=route,
            arguments_bytes=arguments_bytes,
            timeout_seconds=args.timeout,
            signer_url=args.signer.strip() or None,
            ssl=ssl,
        )
    _print_json(receipt)
    if args.receipt_output:
        args.receipt_output.expanduser().write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


async def main() -> None:
    args = _parse_args()
    try:
        if args.command == "describe":
            _print_json(load_runner_route(args.capability).document)
            return
        await _run(args)
    except (RouteContractError, LivepeerGatewayError, aiohttp.ClientError) as error:
        raise SystemExit(f"ERROR: {error}") from error


if __name__ == "__main__":
    asyncio.run(main())
