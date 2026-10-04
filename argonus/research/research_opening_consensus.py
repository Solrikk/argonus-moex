"""Chronological morning consensus with an explicit extra execution-cost budget.

This separate development round follows the single-model cost sensitivity.
Nothing here relabels the reused archive as independent validation.
"""
from __future__ import annotations
from argonus.serialization import load_pickle_bytes

import argparse
import hashlib
import json
import math
import pickle
from dataclasses import replace

from argonus.research import research_continuous_opening as opening
from argonus.research import research_opening_expectancy as base
from argonus.research import research_scanner_v2 as study
from argonus.research.research_signal_direction import replay_events

OUTPUT = opening.OUTPUT / "consensus"
METHODS = ("mean", "consensus", "mean_costguard", "consensus_costguard")
NAMES = ("current_target125", *METHODS, *("A_plus_" + m for m in METHODS), "cash")


def combine(item, predictions, method):
    scores = [predictions[n].get(item.identity, float("nan")) for n in ("rolling_ridge", "rolling_boosting")]
    if not all(math.isfinite(v) for v in scores):
        return float("nan")
    method = method.removeprefix("A_plus_")
    score = min(scores) if method.startswith("consensus") else sum(scores) / 2
    # Predictions already include 5bps on each side. Reserve another 5bps
    # on each side before comparing against the same +0.15% net edge floor.
    return score - (.10 if method.endswith("costguard") else 0.)


def evaluate(prepared, name, predictions, first=None, last=None, bps=5):
    def choose(items, market, contexts):
        chosen = [replace(i, score=combine(i, predictions, name)) for i in items
                  if math.isfinite(combine(i, predictions, name)) and combine(i, predictions, name) >= base.MIN_NET]
        return sorted(chosen, key=lambda i: (-i.score, i.symbol, i.setup))
    return replay_events(prepared, name, first=first, last=last, bps=bps,
                         choose=None if name in ("current_target125", "cash") else choose)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection-only", action="store_true")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUTPUT / "study_manifest.json"
    if not manifest_path.exists():
        study.write(manifest_path, {"candidates": list(NAMES), "selection_cutoff": "2026-03-31",
                                    "round_reason": "Morning rolling learners improve base-cost profit, but a single learner is execution-cost sensitive. Test agreement and a fixed incremental cost reserve.",
                                    "combination": "Equal mean or minimum of monthly expanding ridge/boost predictions; no fitted ensemble weights.",
                                    "execution_cost_reserve_pct": .10, "minimum_net_edge_pct": .15,
                                    "selection": "Maximize the smaller Feb--Mar net PnL at 5/10bps, subject to daily MDD >= -15% at both costs. Control and cash included. Freeze before opening April onward in this round.",
                                    "models_and_features": "Exactly research_opening_expectancy.py; monthly refits use strictly earlier dates.",
                                    "archive_reused": True, "orders_allowed": False})
    if json.loads(manifest_path.read_text())["candidates"] != list(NAMES):
        raise ValueError("Registered family changed")
    prepared, audit = opening.load(args.selection_only)
    data_path = base.OUTPUT / ("dataset_fit.pkl" if args.selection_only else "dataset_full.pkl")
    rows = load_pickle_bytes(data_path.read_bytes())
    predictions, training_audit = base.predictions(prepared, rows, "selection" if args.selection_only else "test")
    choice_path = OUTPUT / "selected_architecture.json"
    if args.selection_only:
        if choice_path.exists():
            raise ValueError("Selection frozen")
        values = {}
        for name in NAMES:
            values[name] = {str(bps): study.compact(evaluate(prepared, name, predictions, "2026-02-01", "2026-03-31", bps)) for bps in (5, 10)}
            print(name, [(bps, round(v["pnl_rub"], 2), v["trades"]) for bps, v in values[name].items()], flush=True)
        ranking = [(min(v["pnl_rub"] for v in by_cost.values()), n) for n, by_cost in values.items()
                   if all(v["daily_close_mdd_pct"] >= -15 for v in by_cost.values())]
        chosen = max(ranking, key=lambda p: (p[0], p[1] == "current_target125"))[1]
        study.write(choice_path, {"name": chosen, "selection_results": values, "training_audit": training_audit,
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
            result = evaluate(prepared, name, predictions, first, last)
            results[name][w] = study.compact(result)
            if w == "test":
                study.write(OUTPUT / f"ledger_{name}.json", result)
        print(name, [(w, round(v["pnl_rub"], 2), v["trades"]) for w, v in results[name].items()], flush=True)
    stress = {str(bps): {n: study.compact(evaluate(prepared, n, predictions, "2026-04-01", bps=bps))
                        for n in NAMES} for bps in (10, 20)}
    study.write(OUTPUT / "report.json", {"selected": choice["name"], "results": results, "stress": stress,
                                        "training_audit": training_audit, "data_audit": audit,
                                        "production_unchanged": True, "archive_reused": True})


if __name__ == "__main__":
    main()
