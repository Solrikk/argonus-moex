"""Net-return learning on recovered morning opportunities; chronological only."""
from __future__ import annotations
from argonus.serialization import load_pickle_bytes

import argparse
import hashlib
import json
import math
import os
import pickle
import statistics as st
from collections import Counter
from dataclasses import replace

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from argonus.strategies import continuous_intraday as core
from argonus.strategies import intraday_signals as signals
from argonus.research import research_continuous_opening as opening
from argonus.research import research_scanner_expectancy as previous
from argonus.research import research_scanner_v2 as study
from argonus.research.research_signal_direction import replay_events

OUTPUT = opening.OUTPUT / "expectancy"
LEARNERS = ("fixed_ridge", "fixed_boosting", "rolling_ridge", "rolling_boosting")
NAMES = ("current_target125", *LEARNERS, *("A_plus_" + s for s in LEARNERS), "cash")
MIN_NET = .15


def vector(item, market, day):
    sign = 1 if item.direction == "long" else -1
    rows = [rows[("scanner", item.symbol)] for tm, rows in sorted(day["rows"].items())
            if "07:00" <= tm < item.decision_time and ("scanner", item.symbol) in rows]
    last = rows[-1]
    ctx = day["contexts"][item.symbol]
    ret = (last[4] / rows[0][1] - 1) * 100
    gap = (rows[0][1] / ctx["previous_close"] - 1) * 100
    width = max(1e-9, max(r[2] for r in rows) - min(r[3] for r in rows))
    position = (last[4] - min(r[3] for r in rows)) / width
    prior = [v for ds, v in sorted(ctx["returns"].items())][-5:]
    atr = st.fmean(max(b[2] - b[3], abs(b[2] - a[4]), abs(b[3] - a[4])) for a, b in zip(rows[:-1][-14:], rows[1:][-14:]))
    past_vol = st.median(r[5] for r in rows[-7:-1])
    return [sign, *(int(item.setup == n) for n in ("opening_follow", "opening_fade", "gap_reclaim")),
            item.score, sign * ret, sign * gap, sign * (ret - market["market_return_pct"]),
            sign * sum(prior) * 100, st.pstdev(ctx["returns"].values()) * 100,
            atr / last[4] * 100, position if sign > 0 else 1 - position,
            sign * (last[4] / last[1] - 1) * 100,
            (last[2] - max(last[1], last[4])) / width,
            (min(last[1], last[4]) - last[3]) / width,
            last[5] / max(1, past_vol), math.log1p(item.recent_turnover), math.log1p(ctx["liquidity"]),
            (signals.minutes(item.decision_time) - 420) / 60]


def dataset(prepared):
    rows = []
    for ds, day in prepared.items():
        for items, market in day["timeline"].values():
            for item in items:
                rows.append({"date": ds, "identity": item.identity, "features": vector(item, market, day),
                             "net_return_pct": previous.label(item, day)})
    return rows


def fit(name, rows, cutoff):
    train = [r for r in rows if r["date"] <= cutoff]
    if not train:
        raise ValueError("Empty historical training sample")
    counts = Counter(r["date"] for r in train)
    weights = np.array([len(train) / len(counts) / counts[r["date"]] for r in train])
    x, y = np.array([r["features"] for r in train]), np.array([r["net_return_pct"] for r in train])
    if name.endswith("ridge"):
        model = make_pipeline(StandardScaler(), Ridge(alpha=200))
        model.fit(x, y, ridge__sample_weight=weights)
    else:
        model = HistGradientBoostingRegressor(max_iter=150, max_depth=3, max_leaf_nodes=8,
                                              learning_rate=.05, min_samples_leaf=100,
                                              l2_regularization=20, random_state=20261003,
                                              early_stopping=False)
        model.fit(x, y, sample_weight=weights)
    return model


def predictions(prepared, rows, phase):
    inference = {}
    for ds, day in prepared.items():
        for items, market in day["timeline"].values():
            for item in items:
                inference[item.identity] = vector(item, market, day)
    months = sorted({ds[:7] for ds in prepared if ds >= ("2026-02-01" if phase == "selection" else "2026-04-01")})
    result, training_audit = {}, []
    for learner in LEARNERS:
        result[learner] = {}
        fixed_cutoff = "2026-01-31" if phase == "selection" else "2026-03-31"
        models = {}
        for month in months:
            cutoff = month + "-01"
            cutoff = (min(ds for ds in prepared if ds[:7] == month))
            if learner.startswith("fixed_"):
                train = [r for r in rows if r["date"] <= fixed_cutoff]
                key = fixed_cutoff
            else:
                train = [r for r in rows if r["date"] < cutoff]
                key = cutoff
            if key not in models:
                models[key] = fit(learner, train, max(r["date"] for r in train))
                training_audit.append({"learner": learner, "inference_month": month,
                                       "latest_training_day": max(r["date"] for r in train),
                                       "training_days": len({r["date"] for r in train}), "rows": len(train)})
            ids = [identity for identity in inference if identity[0].startswith(month)]
            if ids:
                values = models[key].predict(np.array([inference[i] for i in ids]))
                result[learner].update({i: float(v) for i, v in zip(ids, values)})
        print("Predictions:", learner, len(result[learner]), flush=True)
    return result, training_audit


def evaluate(prepared, name, prediction_map, first=None, last=None, bps=5):
    def choose(items, market, contexts):
        learner = name.removeprefix("A_plus_")
        chosen = [replace(i, score=prediction_map[learner][i.identity]) for i in items
                  if math.isfinite(prediction_map[learner].get(i.identity, float("nan")))
                  and prediction_map[learner][i.identity] >= MIN_NET]
        return sorted(chosen, key=lambda i: (-i.score, i.symbol, i.setup))
    return replay_events(prepared, name, first=first, last=last, bps=bps,
                         choose=None if name in ("cash", "current_target125") else choose)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection-only", action="store_true")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUTPUT / "study_manifest.json"
    if not manifest_path.exists():
        study.write(manifest_path, {"candidates": list(NAMES), "minimum_predicted_net_pct": MIN_NET,
                                    "fit_end": "2026-01-31", "selection_end": "2026-03-31",
                                    "test_first": "2026-04-01", "fixed_final_training_end": "2026-03-31",
                                    "rolling": "Refit once before each month using strictly earlier, already completed dates. Architecture and hyperparameters never selected using test months.",
                                    "models": {"ridge_alpha": 200, "boost_depth": 3, "boost_leaves": 8,
                                               "boost_iters": 150, "boost_lr": .05, "boost_leaf_min": 100, "boost_l2": 20},
                                    "selection": "Maximum Feb--Mar net PnL among all preregistered alternatives, with MDD >= -15%; cash and current strategy are eligible.",
                                    "execution": "Unchanged morning rules and shared event portfolio. No stop/target sweep.",
                                    "features": "19 causal numeric observations: direction, setup, morning price path, gap, relative strength, prior daily returns, volatility, liquidity, time. No ticker/date identities or future fills.",
                                    "archive_reused": True, "orders_allowed": False})
    if json.loads(manifest_path.read_text())["candidates"] != list(NAMES):
        raise ValueError("Registered candidates changed")
    prepared, audit = opening.load(args.selection_only)
    data_path = OUTPUT / ("dataset_fit.pkl" if args.selection_only else "dataset_full.pkl")
    if data_path.exists():
        rows = load_pickle_bytes(data_path.read_bytes())
    else:
        rows = dataset(prepared)
        data_path.write_bytes(pickle.dumps(rows, protocol=5))
    print("Training labels:", len(rows), "dates:", len({r['date'] for r in rows}), flush=True)
    pred, training_audit = predictions(prepared, rows, "selection" if args.selection_only else "test")
    choice_path = OUTPUT / "selected_architecture.json"
    if args.selection_only:
        if choice_path.exists():
            raise ValueError("Model choice frozen")
        values = {}
        for name in NAMES:
            v = evaluate(prepared, name, pred, "2026-02-01", "2026-03-31")
            values[name] = study.compact(v)
            print(name, round(v["pnl_rub"], 2), v["trades"], round(v["daily_close_mdd_pct"], 2), flush=True)
        passing = [(v["pnl_rub"], n) for n, v in values.items() if v["daily_close_mdd_pct"] >= -15]
        chosen = max(passing, key=lambda p: (p[0], p[1] == "current_target125"))[1]
        study.write(choice_path, {"name": chosen, "selection": values, "training_audit": training_audit,
                                  "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                                  "status": "research_only"})
        print("FROZEN:", chosen, flush=True)
        return
    choice = json.loads(choice_path.read_text())
    if choice["manifest_sha256"] != hashlib.sha256(manifest_path.read_bytes()).hexdigest():
        raise ValueError("Frozen contract changed")
    windows = {"test": ("2026-04-01", None), "validation": ("2026-04-01", "2026-07-16"),
               "later": ("2026-07-17", None)}
    results = {}
    for name in NAMES:
        results[name] = {}
        for w, (first, last) in windows.items():
            value = evaluate(prepared, name, pred, first, last)
            results[name][w] = study.compact(value)
            if w == "test":
                study.write(OUTPUT / f"ledger_{name}.json", value)
        print(name, [(w, round(v["pnl_rub"], 2), v["trades"]) for w, v in results[name].items()], flush=True)
    stress = {str(bps): {n: study.compact(evaluate(prepared, n, pred, "2026-04-01", bps=bps))
                        for n in (choice["name"], "current_target125")} for bps in (10, 20)}
    study.write(OUTPUT / "report.json", {"selected": choice["name"], "results": results,
                                        "stress": stress, "training_audit": training_audit,
                                        "production_unchanged": True, "archive_reused": True,
                                        "data_audit": audit})


if __name__ == "__main__":
    main()
