"""Recover discarded public morning bars and test causal broad-market entries."""
from __future__ import annotations
from argonus.serialization import load_pickle_bytes

import argparse
import gzip
import hashlib
import json
import pickle
from collections import defaultdict
from datetime import date
from pathlib import Path

from argonus.strategies import continuous_opening as opening
from argonus.strategies import continuous_intraday as core
from argonus.research import research_scanner_v2 as study
from argonus.research import research_win_rate_60 as historical
from argonus.research.research_signal_direction import replay_events

OUTPUT = study.ROOT / "data/backtests/continuous_opening_2026-10-03"
NAMES = ("current_target125", *opening.SETUPS, *("A_plus_" + s for s in opening.SETUPS), "cash")


def register():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    p = OUTPUT / "study_manifest.json"
    if not p.exists():
        study.write(p, {"candidates": list(NAMES), "selection_end": "2026-03-31",
                        "selection_windows": {"development": ["2025-11-03", "2026-01-31"],
                                              "confirmation": ["2026-02-01", "2026-03-31"]},
                        "rules": "Causal opening continuation, confirmed opening exhaustion, confirmed unfilled overnight gap recovery; union. Decisions 07:20--09:30 every 5m; first fill +5m; existing shared portfolio.",
                        "selection": "Hybrid incremental profit >0 in BOTH early windows; daily MDD >= -15%; maximize summed early net profits among control and eligible alternatives.",
                        "stop_pct": 1, "target_pct": 2, "maximum_total_notional_rub": 150000,
                        "maximum_leverage": 3, "maximum_new_position_rub": 50000,
                        "fee_pct_per_side": .04, "slippage_bps_per_side": 5,
                        "archive_reused": True, "orders_allowed": False,
                        "data_repair": "Both archives use Europe/Moscow. MOEX original minute pages retain 07:00--09:50 omitted by the prior aggregate; recover to a new, separate full-session archive without API requests."})
    if json.loads(p.read_text())["candidates"] != list(NAMES):
        raise ValueError("Candidate family changed")
    return p


def full_moex_sessions():
    manifest = json.loads((study.NEW / "manifest.json").read_text())
    result, audit = {}, {"timezone": "Europe/Moscow", "sources": {}, "minute_rows": 0,
                         "morning_minute_rows_recovered": 0}
    for symbol in manifest["selected"]:
        chunks = []
        paths = list((study.NEW / "pages").glob(f"{symbol}_1_{manifest['from']}_{manifest['till']}_*.json"))
        for p in sorted(paths, key=lambda p: int(p.stem.rsplit("_", 1)[1])):
            audit["sources"][str(p.relative_to(study.ROOT))] = hashlib.sha256(p.read_bytes()).hexdigest()
            chunks.extend(json.loads(p.read_text()))
        stamps = [r[0] for r in chunks]
        if stamps != sorted(set(stamps)):
            raise ValueError(f"Duplicated/unordered source minute timestamps: {symbol}")
        groups = defaultdict(list)
        for r in chunks:
            ds, tm = r[0][:10], r[0][11:16]
            if not "07:00" <= tm <= "23:49":
                continue
            audit["minute_rows"] += 1
            if tm < "09:50":
                audit["morning_minute_rows_recovered"] += 1
            hh, mm = map(int, tm.split(":"))
            groups[(ds, core.clock(hh * 60 + mm // 5 * 5))].append(r)
        days = defaultdict(list)
        for (ds, tm), rows in sorted(groups.items()):
            days[ds].append([tm, rows[0][1], max(r[2] for r in rows), min(r[3] for r in rows),
                             rows[-1][4], sum(r[5] for r in rows), sum(r[6] for r in rows)])
        result[symbol] = dict(days)
        p = OUTPUT / "recovered_five_min" / f"{symbol}.json.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(gzip.compress(json.dumps(days, separators=(",", ":")).encode(), mtime=0))
    study.write(OUTPUT / "recovery_audit.json", audit)
    return result


def load(selection_only):
    path = OUTPUT / ("prepared_fit.pkl" if selection_only else "prepared_full.pkl")
    if path.exists():
        return load_pickle_bytes(path.read_bytes())
    previous = study.ROOT / "data/backtests/signal_direction_2026-10-03/prepared_fit.pkl"
    if selection_only and previous.exists():
        prepared, audit = load_pickle_bytes(previous.read_bytes())
    else:
        # Do not calculate the unrelated 17,222 afternoon scanner decisions.
        # This study consumes raw, unmodified source rows and its own clock.
        names = json.loads((study.ROOT / "data/intraday_universe/manifest.json").read_text())["selected"]
        old_books, hashes = {}, {}
        for symbol in names:
            p = study.ROOT / "data/intraday_universe/five_min" / f"{symbol}.json.gz"
            with gzip.open(p, "rt") as handle:
                old_books[symbol] = json.load(handle)
            hashes[str(p.relative_to(study.ROOT))] = hashlib.sha256(p.read_bytes()).hexdigest()
        trades, _ = historical.load_data(not selection_only)
        by_date = {t.date: t for t in trades}
        late_books = full_moex_sessions() if not selection_only else {}
        dates = sorted({ds for books in (old_books, late_books) for book in books.values() for ds in book
                        if "2025-11-03" <= ds <= ("2026-03-31" if selection_only else "2026-09-09")
                        and date.fromisoformat(ds).weekday() < 5})
        prepared, daily_cache = {}, {}
        for ds in dates:
            books = old_books if ds <= study.VALIDATION_END else late_books
            directory = study.ROOT / "data/intraday_universe" if ds <= study.VALIDATION_END else study.NEW
            contexts, market_rows = {}, {}
            for symbol, book in books.items():
                if ds not in book:
                    continue
                key = (str(directory), symbol)
                if key not in daily_cache:
                    dp = directory / "dailies" / f"{symbol}.json"
                    daily_cache[key] = json.loads(dp.read_text())
                    hashes[str(dp.relative_to(study.ROOT))] = hashlib.sha256(dp.read_bytes()).hexdigest()
                ctx = core.prior_context(daily_cache[key], ds)
                if ctx:
                    contexts[symbol] = ctx
                for row in book[ds]:
                    market_rows.setdefault(row[0], {})[("scanner", symbol)] = row
            trade = by_date.get(ds)
            if trade:
                for c in trade.candles:
                    market_rows.setdefault(c.time, {})[("legacy", trade.symbol)] = [c.time, c.open, c.high, c.low, c.close, 0]
            prepared[ds] = {"rows": market_rows, "contexts": contexts, "legacy": trade}
        audit = {"dates": [dates[0], dates[-1]], "weekday_sessions": len(dates),
                 "old_symbols": len(old_books), "late_symbols": len(late_books), "source_hashes": hashes}
    late = {}  # Full sessions have already been recovered above.
    dailies = {}
    for ds, day in prepared.items():
        directory = study.ROOT / "data/intraday_universe" if ds <= study.VALIDATION_END else study.NEW
        sessions = defaultdict(list)
        if ds > study.VALIDATION_END:
            for symbol, dates in late.items():
                for r in dates.get(ds, []):
                    day["rows"].setdefault(r[0], {})[("scanner", symbol)] = r
        for tm, rows in sorted(day["rows"].items()):
            for (source, symbol), r in rows.items():
                if source == "scanner":
                    sessions[symbol].append(r)
        for symbol, ctx in day["contexts"].items():
            key = (str(directory), symbol)
            if key not in dailies:
                dailies[key] = json.loads((directory / "dailies" / f"{symbol}.json").read_text())
            previous = [r for r in dailies[key] if r[0] < ds]
            ctx["previous_close"] = previous[-1][4] if previous else None
        day["timeline"] = {core.clock(m): opening.scan(ds, core.clock(m), sessions, day["contexts"])
                           for m in range(440, 571, 5)}
    path.write_bytes(pickle.dumps((prepared, audit), protocol=5))
    return prepared, audit


def evaluate(prepared, name, first=None, last=None, bps=5):
    return replay_events(prepared, name, first=first, last=last, bps=bps,
                         choose=None if name in ("cash", "current_target125") else opening.gate(name))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection-only", action="store_true")
    args = parser.parse_args()
    manifest_path = register()
    prepared, audit = load(args.selection_only)
    choice_path = OUTPUT / "selected_architecture.json"
    if args.selection_only:
        if choice_path.exists():
            raise ValueError("Candidate already frozen")
        windows = json.loads(manifest_path.read_text())["selection_windows"]
        results = {}
        for name in NAMES:
            results[name] = {w: study.compact(evaluate(prepared, name, first, last))
                             for w, (first, last) in windows.items()}
            print(name, [(w, round(v["pnl_rub"], 2), v["trades"]) for w, v in results[name].items()], flush=True)
        best = sum(v["pnl_rub"] for v in results["current_target125"].values())
        chosen = "current_target125"
        for name in NAMES:
            values = results[name]
            passing = all(v["pnl_rub"] > 0 and v["daily_close_mdd_pct"] >= -15 for v in values.values())
            if name.startswith("A_plus_"):
                passing &= all(v["pnl_rub"] > results["current_target125"][w]["pnl_rub"] for w, v in values.items())
            profit = sum(v["pnl_rub"] for v in values.values())
            if passing and profit > best:
                chosen, best = name, profit
        study.write(choice_path, {"name": chosen, "results": results,
                                  "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                                  "status": "research_only"})
        print("FROZEN:", chosen, flush=True)
        return
    choice = json.loads(choice_path.read_text())
    if choice["manifest_sha256"] != hashlib.sha256(manifest_path.read_bytes()).hexdigest():
        raise ValueError("Frozen contract changed")
    windows = {"test": ("2026-04-01", None), "validation": ("2026-04-01", "2026-07-16"),
               "later": ("2026-07-17", None), "full": (None, None)}
    results = {}
    for name in NAMES:
        results[name] = {}
        for w, (first, last) in windows.items():
            value = evaluate(prepared, name, first, last)
            results[name][w] = study.compact(value)
            if w in ("full", "test"):
                study.write(OUTPUT / f"ledger_{name}_{w}.json", value)
        print(name, [(w, round(v["pnl_rub"], 2), v["trades"]) for w, v in results[name].items()], flush=True)
    study.write(OUTPUT / "report.json", {"selected": choice["name"], "results": results,
                                        "data_audit": audit, "production_unchanged": True,
                                        "archive_reused": True})


if __name__ == "__main__":
    main()
