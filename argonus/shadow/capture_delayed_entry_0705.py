#!/usr/bin/env python3
"""Reserve and capture one isolated 07:05 order-book shadow observation.

The only remote method in this program is the read-only MarketDataService
GetOrderBook call.  It never imports the production engine, takes its process
mutex, or uses an account/order service.  The immutable plan is created first; if that
date is already reserved, the program exits without another API request.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from argonus.shadow import delayed_entry_shadow as registry
from argonus.shadow import forward_shadow_research as exit_registry
from argonus.market_data.tbank_market_data import TBankInvestClient


MOSCOW = ZoneInfo("Europe/Moscow")
from argonus.paths import PROJECT_ROOT as PROJECT_DIR
GET_ORDER_BOOK_METHOD = (
    "tinkoff.public.invest.api.contract.v1.MarketDataService/GetOrderBook"
)


def _read_object(path: str | os.PathLike[str], label: str) -> dict[str, Any]:
    artifact = Path(path)
    try:
        value = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read {label} {artifact}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} must be a JSON object")
    return dict(value)


def _engine_a_state(document: Mapping[str, Any], trade_date: str) -> dict[str, Any]:
    if isinstance(document.get("trades"), list):
        matches = [
            row for row in document["trades"]
            if isinstance(row, Mapping) and row.get("engine") == "A"
        ]
        if len(matches) != 1:
            raise RuntimeError("bot_state must contain exactly one Engine A trade")
        state = dict(matches[0])
    else:
        state = dict(document)
    if str(state.get("date")) != trade_date:
        raise RuntimeError("bot_state Engine A trade is not for the capture date")
    return state


def default_exit_plan_path(forward_dir: Path, trade_date: str) -> Path:
    return (
        forward_dir
        / exit_registry.EXIT_POLICY
        / "exit_shadow_plan"
        / f"{trade_date}.json"
    )


def get_order_book_once(client: TBankInvestClient, uid: str) -> dict[str, Any]:
    """One allowlisted unary market-data request; retry_count must remain one."""
    if client.retry_count != 1:
        raise RuntimeError("delayed-entry recorder requires retry_count=1")
    response = client._post(
        GET_ORDER_BOOK_METHOD,
        {"instrumentId": uid, "depth": registry.BOOK_DEPTH},
    )
    if not isinstance(response, Mapping):
        raise RuntimeError("GetOrderBook returned a non-object response")
    return dict(response)


def capture_once(
    *,
    now: datetime,
    exit_plan_path: Path,
    state_path: Path,
    output_dir: Path,
    manifest_path: Path,
    client_factory=TBankInvestClient,
    clock=lambda: datetime.now(MOSCOW),
) -> tuple[Path, bool]:
    local_now = now.astimezone(MOSCOW) if now.tzinfo is not None else now.replace(tzinfo=MOSCOW)
    trade_date = local_now.date().isoformat()
    manifest = registry.load_manifest(manifest_path)
    exit_plan = _read_object(exit_plan_path, "protected exit-shadow plan")
    state_document = _read_object(state_path, "bot state")
    state = _engine_a_state(state_document, trade_date)
    plan = registry.build_plan(
        exit_plan,
        state,
        created_at=local_now,
        manifest=manifest,
    )
    plan_path, created = registry.write_first_writer_wins(
        plan,
        output_dir,
        current_date=local_now.date(),
        manifest=manifest,
    )
    if not created:
        # This is the key no-retry boundary: never construct a client and never
        # make a second point-in-time request for an already reserved date.
        return plan_path, False

    client = client_factory(
        timeout=3,
        retry_count=1,
        user_agent="argonus-delayed-entry-shadow/1.0",
    )
    # Token lookup/client construction is local but can take time; timestamp
    # immediately before the sole remote request, then fail closed if late.
    request_started = clock()
    registry.require_capture_window(request_started, trade_date)
    raw_book = get_order_book_once(client, str(plan["uid"]))
    response_received = clock()
    capture = registry.build_capture(
        plan,
        raw_book,
        request_started_at=request_started,
        response_received_at=response_received,
        manifest=manifest,
    )
    capture_path, capture_created = registry.write_first_writer_wins(
        capture,
        output_dir,
        current_date=local_now.date(),
        manifest=manifest,
    )
    if not capture_created:
        raise RuntimeError("new plan unexpectedly collided with an existing capture")
    return capture_path, bool(capture["promotion_eligible"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture one 07:05 read-only delayed-entry order-book shadow."
    )
    parser.add_argument("--manifest", default=str(PROJECT_DIR / "config/delayed_entry_shadow_manifest.json"))
    parser.add_argument("--forward-shadow-dir", default=str(PROJECT_DIR / "runtime/forward_shadow"))
    parser.add_argument("--state", default=str(PROJECT_DIR / "runtime/bot_state.json"))
    parser.add_argument("--output-dir", default=str(PROJECT_DIR / "runtime/delayed_entry_shadow"))
    parser.add_argument("--exit-plan", help="Override protected same-day exit-shadow plan path.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    now = datetime.now(MOSCOW)
    exit_path = (
        Path(args.exit_plan)
        if args.exit_plan
        else default_exit_plan_path(Path(args.forward_shadow_dir), now.date().isoformat())
    )
    try:
        path, eligible = capture_once(
            now=now,
            exit_plan_path=exit_path,
            state_path=Path(args.state),
            output_dir=Path(args.output_dir),
            manifest_path=Path(args.manifest),
        )
        status = "eligible" if eligible else "reserved_or_diagnostic_only"
        print(f"delayed-entry shadow: {status}; {path}")
        return 0
    except Exception as exc:  # fail closed for evidence; production is separate
        print(f"delayed-entry shadow capture failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
