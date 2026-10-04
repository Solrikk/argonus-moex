"""Registered contrarian continuation-signal study; offline orders only.

The previously unprofitable continuation scanners motivate an explicit new
hypothesis: liquidity surges may mark exhaustion rather than continuation.
Selection cannot inspect any day after March 31. Earlier archive reuse is
disclosed; these are historical development results, never forward proof.
"""
from __future__ import annotations
from argonus.serialization import load_pickle_bytes

import argparse
import hashlib
import json
import pickle
from dataclasses import replace

from argonus.research import research_scanner_v2 as study

OUTPUT = study.ROOT / "data/backtests/signal_direction_2026-10-03"
NAMES = ("current_target125", "fade_breakout", "fade_pullback", "fade_both",
         "target125_then_fade_breakout", "target125_then_fade_pullback", "target125_then_fade_both", "cash")


def gate(name):
    setups = {"rolling_breakout", "rolling_pullback"} if name.endswith("both") else {
        "rolling_breakout" if name.endswith("breakout") else "rolling_pullback"}

    def choose(items, market, contexts):
        return [replace(item, direction="short" if item.direction == "long" else "long",
                        setup="fade_" + item.setup.removeprefix("rolling_"))
                for item in items if item.setup in setups]
    return choose


def evaluate(prepared, name, first=None, last=None, bps=5):
    return replay_events(prepared, name, first=first, last=last, bps=bps,
                         choose=None if name in ("cash", "current_target125") else gate(name))


def replay_events(prepared, name, *, first=None, last=None, bps=5, choose=None):
    """A missing 18:35 print delays liquidation until the next actual print.

    No future missing-bar filter, synthetic close or favorable sample deletion.
    Signals cease at their registered cutoff. End-of-session exposure is still
    an explicit error if the full day's archive has no subsequent real print.
    """
    engine = study.engine
    portfolio = engine.EventPortfolio(bps=bps)
    for ds, day in prepared.items():
        if (first and ds < first) or (last and ds > last):
            continue
        portfolio.begin_day(ds)
        trade = day["legacy"]
        if trade and (name == "current_target125" or name.startswith("target125_then_") or name.startswith("A_plus_")):
            portfolio.submit(engine.Opportunity(ds, trade.symbol, trade.direction, "current_target125",
                                                "07:00", trade.target_price, 0, 1, 2, 0,
                                                "legacy", trade.target_price), day["contexts"])
        for minute in range(420, 1426, 5):
            tm = engine.clock(minute)
            portfolio.step(tm, day["rows"].get(tm, {}))
            decision = engine.clock(minute + 5)
            if choose and decision in day["timeline"]:
                items, market = day["timeline"][decision]
                for item in choose(items, market, day["contexts"]):
                    portfolio.submit(item, day["contexts"])
            if minute >= 1115 and not portfolio.positions and not portfolio.pending:
                break
        portfolio.end_day()
    return portfolio.result()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection-only", action="store_true")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    contract_path = OUTPUT / "study_manifest.json"
    if not contract_path.exists():
        study.write(contract_path, {
            "candidates": list(NAMES), "selection_cutoff": "2026-03-31",
            "selection_windows": {"development": ["2025-11-03", "2026-01-31"],
                                  "confirmation": ["2026-02-01", "2026-03-31"]},
            "selection_rule": "Both early windows profitable, drawdown >= -15%; hybrid must improve control in both; maximize sum of early net profits.",
            "execution": "Exactly the original continuous event portfolio and costs, no exit optimization.",
            "hypothesis": "Fade closed-candle continuation signals: volume and relative strength may identify exhaustion.",
            "archive_reused": True, "live_orders": False,
        })
    manifest = json.loads(contract_path.read_text())
    if manifest["candidates"] != list(NAMES):
        raise ValueError("Registered family changed")
    cache_path = OUTPUT / ("prepared_fit.pkl" if args.selection_only else "prepared_full.pkl")
    if cache_path.exists():
        # This cache is created by this local program, never an external upload.
        prepared, audit = load_pickle_bytes(cache_path.read_bytes())
    else:
        prepared, audit = study.load_data(include_later=not args.selection_only)
        cache_path.write_bytes(pickle.dumps((prepared, audit), protocol=5))
    selected_path = OUTPUT / "selected_architecture.json"
    if args.selection_only:
        if selected_path.exists():
            raise ValueError("Selection already frozen")
        results = {name: {window: study.compact(evaluate(prepared, name, first, last))
                         for window, (first, last) in manifest["selection_windows"].items()} for name in NAMES}
        chosen, best = "current_target125", sum(v["pnl_rub"] for v in results["current_target125"].values())
        for name in NAMES:
            if name in ("cash", "current_target125"):
                continue
            values = results[name]
            passing = all(v["pnl_rub"] > 0 and v["daily_close_mdd_pct"] >= -15 for v in values.values())
            if name.startswith("target125_then_"):
                passing &= all(v["pnl_rub"] > results["current_target125"][w]["pnl_rub"] for w, v in values.items())
            profit = sum(v["pnl_rub"] for v in values.values())
            if passing and profit > best:
                chosen, best = name, profit
        study.write(selected_path, {"name": chosen, "manifest_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
                                    "results": results, "status": "research_only"})
        print(json.dumps({"selected": chosen, "results": results}, indent=2), flush=True)
        return
    choice = json.loads(selected_path.read_text())
    if choice["manifest_sha256"] != hashlib.sha256(contract_path.read_bytes()).hexdigest():
        raise ValueError("Frozen contract changed")
    windows = {"test": ("2026-04-01", None), "validation": ("2026-04-01", "2026-07-16"),
               "later": ("2026-07-17", None)}
    results = {}
    for name in NAMES:
        results[name] = {}
        for window, (first, last) in windows.items():
            result = evaluate(prepared, name, first, last)
            results[name][window] = study.compact(result)
            if window == "test":
                study.write(OUTPUT / f"ledger_{name}.json", result)
        print("Evaluated:", name, flush=True)
    report = {"selected": choice["name"], "results": results, "audit": audit,
              "production_unchanged": True, "archive_reused": True}
    study.write(OUTPUT / "report.json", report)
    print(json.dumps({"selected": choice["name"], "results": results}, indent=2), flush=True)


if __name__ == "__main__":
    main()
