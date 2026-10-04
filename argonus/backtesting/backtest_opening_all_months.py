"""Carry one account through all 13 historical months, marking scanner gaps.

October--January are explicit training warmup. Scanner candles end Sep9;
missing universe data are recorded, never called zero-signal observations.
The legacy-only control must reproduce the frozen 136495.475247 balance.
"""
from __future__ import annotations
from argonus.serialization import load_pickle_bytes

import csv
import gzip
import hashlib
import json
import pickle
from datetime import date, timedelta
from pathlib import Path

from argonus.models import opening_profit_model as model
from argonus.research import research_continuous_opening as data
from argonus.research import research_opening_expectancy as original
from argonus.research import research_scanner_v2 as study
from argonus.research import research_win_rate_60 as legacy
from argonus.research.research_signal_direction import replay_events

OUTPUT = study.ROOT / "data/backtests/opening_integration_2026-10-03"


def build():
    prepared, audit = data.load(False)
    rows = load_pickle_bytes((original.OUTPUT / "dataset_full.pkl").read_bytes())
    trades, _ = legacy.load_data(True)
    tail_path = study.ROOT / "data/backtests/profit_first_2026-10-03/fresh_tail/market_cache.json.gz"
    with gzip.open(tail_path, "rt") as handle:
        tail = json.load(handle)
    for day in tail["selection_days"]:
        if day["selected"]:
            key = day["date"] + "|" + day["selected"]["symbol"]
            session = tail["sessions"][key]
            if legacy.inputs.october.canonical_sha256(session["rows"]) != session["session_sha256"]:
                raise ValueError("Fresh legacy candle hash changed")
            trades.append(legacy.inputs.to_trade(day, session))
    by_date = {t.date: t for t in trades}
    if len(by_date) != len(trades):
        raise ValueError("Duplicate legacy trade dates")
    first, last = date(2025, 10, 1), date(2026, 10, 2)
    combined = {}
    ds = first
    while ds <= last:
        key = ds.isoformat()
        if ds.weekday() < 5:
            day = prepared.get(key, {"rows": {}, "contexts": {}, "legacy": None, "timeline": {}})
            day["legacy"] = by_date.get(key)
            if day["legacy"]:
                for c in day["legacy"].candles:
                    day["rows"].setdefault(c.time, {})[("legacy", day["legacy"].symbol)] = [c.time, c.open, c.high, c.low, c.close, 0]
            combined[key] = day
        ds += timedelta(days=1)
    models, training_audit, scores, groups = {}, [], {}, {}
    for ds, day in combined.items():
        if ds < "2026-02-01" or ds > "2026-09-09":
            continue
        month = ds[:7]
        if month not in models:
            models[month], provenance = model.train_models(rows, month)
            training_audit.append(provenance)
            print("Trained:", month, provenance["latest_training_date"], flush=True)
        for items, market in day["timeline"].values():
            for item in items:
                groups.setdefault(month, []).append((item, model.vector(item, market, day)))
    for month, items in groups.items():
        features = model.np.array([v for _, v in items])
        predictions = [models[month][name].predict(features) for name in model.LEARNERS]
        for (item, _), a, b in zip(items, *predictions):
            score = float((a+b)/2)
            if model.math.isfinite(score) and score >= model.MIN_NET_PCT:
                scores[item.identity] = score
        print("Scored:", month, len(items), flush=True)

    def choose(items, market, contexts):
        from dataclasses import replace
        return sorted([replace(i, score=scores[i.identity]) for i in items if i.identity in scores],
                      key=lambda i: (-i.score, i.symbol, i.setup))

    candidate = replay_events(combined, "A_plus_mean", choose=choose)
    control = replay_events(combined, "current_target125")
    frozen = json.loads((study.ROOT / "data/backtests/profit_first_2026-10-03/report.json").read_text())
    expected = frozen["fresh_tail"]["continuous_with_archive"]["target125"]["ending_equity_rub"]
    if abs(control["ending_equity_rub"] - expected) > 1e-6:
        raise ValueError(f"Frozen control differs: {control['ending_equity_rub']} != {expected}")
    months = []
    for month in sorted({d["date"][:7] for d in candidate["daily"]}):
        daily = [d for d in candidate["daily"] if d["date"].startswith(month)]
        legs = [r for r in candidate["ledger"] if r["date"].startswith(month)]
        start, end = daily[0]["equity_before"], daily[-1]["equity_after"]
        scanner_days = sum(d["date"] in prepared and "2026-02-01" <= d["date"] <= "2026-09-09" for d in daily)
        coverage = ("legacy_only_training_warmup" if month <= "2026-01" else
                    "scanner_and_legacy" if month <= "2026-08" else
                    "scanner_through_september9_legacy_all_month" if month == "2026-09" else
                    "legacy_only_scanner_data_missing")
        months.append({"month": month, "first_date": daily[0]["date"], "last_date": daily[-1]["date"],
                       "start_equity_rub": start, "end_equity_rub": end, "pnl_rub": end-start,
                       "return_pct": (end/start-1)*100, "trades": len(legs),
                       "wins": sum(t["pnl_rub"]>0 for t in legs),
                       "new_scanner_trades": sum(t["source"]=="scanner" for t in legs),
                       "legacy_trades": sum(t["source"]=="legacy" for t in legs),
                       "scanner_observed_weekday_sessions": scanner_days, "coverage": coverage,
                       "control_pnl_rub": sum(d["pnl_rub"] for d in control["daily"] if d["date"].startswith(month))})
    return {"policy": model.POLICY, "mode": "historical_coverage_limited_replay", "archive_reused": True,
            "period": [first.isoformat(), last.isoformat()], "candidate": candidate, "control": control,
            "monthly": months, "training_audit": training_audit, "source_audit": audit,
            "not_full_new_architecture_backtest": True,
            "limits": ["October--January contain only legacy trades while the morning model trains.",
                       "February--March were used for architecture selection, not independent test months.",
                       "Broad morning data end September9. Additional scanner trades after that date are unknown, not proven absent.",
                       "One cash account is carried through all months; no monthly reset or additional leverage.",
                       "Reused universe, fractional shares, no historical orderbooks or short permissions."]}


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    value = build()
    study.write(OUTPUT / "all_months.json", value)
    with (OUTPUT / "all_months.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(value["monthly"][0]))
        writer.writeheader(); writer.writerows(value["monthly"])
    print(json.dumps({"candidate": study.compact(value["candidate"]), "control": study.compact(value["control"]),
                      "monthly": value["monthly"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
