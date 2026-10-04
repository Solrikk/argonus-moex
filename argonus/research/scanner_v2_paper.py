"""Replay a continuous paper portfolio on one cached market day.

This command cannot place broker orders. It reads only closed historical bars
and writes a separate paper ledger, without touching production bot state.
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

from argonus.strategies import continuous_intraday as engine

from argonus.paths import PROJECT_ROOT as ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/backtests/moex_new_period_2026-09-10")
    parser.add_argument("--strategy", choices=["rolling_breakout", "rolling_pullback", "rolling_reversion", "regime_router", "single_snapshot_router"], default="regime_router")
    parser.add_argument("--output", type=Path, default=ROOT / "data/backtests/scanner_v2_2026-10-03/paper_preview.json")
    args = parser.parse_args()
    manifest = json.loads((args.data_dir / "manifest.json").read_text())
    sessions, contexts, timeline = {}, {}, {}
    for symbol in manifest["selected"]:
        with gzip.open(args.data_dir / "five_min" / f"{symbol}.json.gz", "rt") as handle:
            book = json.load(handle)
        if args.date not in book:
            continue
        sessions[symbol] = book[args.date]
        daily = json.loads((args.data_dir / "dailies" / f"{symbol}.json").read_text())
        context = engine.prior_context(daily, args.date)
        if context:
            contexts[symbol] = context
        for row in sessions[symbol]:
            timeline.setdefault(row[0], {})[("scanner", symbol)] = row
    if not sessions:
        raise SystemExit("The requested day is absent from the cache; no prices were invented")
    portfolio = engine.EventPortfolio()
    portfolio.begin_day(args.date)
    candidates = 0
    for minute in range(590, 1116, 5):
        tm, decision = engine.clock(minute), engine.clock(minute + 5)
        portfolio.step(tm, timeline.get(tm, {}))
        items, market = engine.scan_opportunities(args.date, decision, sessions, contexts)
        candidates += len(items)
        for item in engine.route_opportunities(items, market, args.strategy):
            portfolio.submit(item, contexts)
    portfolio.end_day()
    result = portfolio.result() | {"mode": "cached_day_paper_replay", "orders_allowed": False,
                                   "date": args.date, "strategy": args.strategy,
                                   "source_directory": str(args.data_dir), "scanned_symbols": len(sessions),
                                   "setup_candidates": candidates}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in ("ledger", "daily")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
