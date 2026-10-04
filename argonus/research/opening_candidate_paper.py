"""Reproduce the pinned morning portfolio candidate; no broker or live orders.

Training labels must precede the first day of the inference month. New signals
come from closed morning candles; the shared event engine handles later fills.
This is historical paper replay, never independent forward validation.
"""
from __future__ import annotations
from argonus.serialization import load_pickle_bytes

import argparse
import hashlib
import json
import pickle
from pathlib import Path

from argonus.research import research_continuous_opening as opening
from argonus.research import research_opening_expectancy as learners
from argonus.research import research_opening_consensus as consensus
from argonus.research import research_scanner_v2 as study

PROFILE = consensus.OUTPUT / "candidate_profile.json"


def validate_profile(path=PROFILE):
    profile = json.loads(path.read_text())
    if profile["name"] != "A_plus_mean" or profile["orders_allowed"] or profile["mode"] != "historical_paper_replay":
        raise ValueError("Unsupported paper profile")
    for name, expected in profile["pinned_files"].items():
        if hashlib.sha256((study.ROOT / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Paper profile artifact changed: {name}")
    return profile


def training_rows_for_date(rows, ds):
    # Calendar cutoff is resolved before reading any current-month labels.
    cutoff = ds[:7] + "-01"
    return [r for r in rows if r["date"] < cutoff]


def replay(first, last):
    profile = validate_profile()
    prepared, audit = opening.load(False)
    days = {ds: day for ds, day in prepared.items() if first <= ds <= last}
    if not days or min(days) != first or max(days) != last or first < "2026-04-01":
        raise ValueError("Requested endpoints must exist in the registered April--September replay archive")
    all_training = load_pickle_bytes((learners.OUTPUT / "dataset_full.pkl").read_bytes())
    prediction_map = {"rolling_ridge": {}, "rolling_boosting": {}}
    model_cache, training_audit = {}, []
    for ds, day in days.items():
        month = ds[:7]
        if month not in model_cache:
            train = training_rows_for_date(all_training, ds)
            cutoff = max(r["date"] for r in train)
            models = {name: learners.fit(name, train, cutoff) for name in prediction_map}
            model_cache[month] = models
            training_audit.append({"inference_month": month, "latest_training_date": cutoff,
                                   "training_rows": len(train), "training_days": len({r['date'] for r in train})})
        for items, market in day["timeline"].values():
            if not items:
                continue
            features = [learners.vector(i, market, day) for i in items]
            for name, model in model_cache[month].items():
                values = model.predict(features)
                prediction_map[name].update({i.identity: float(v) for i, v in zip(items, values)})
    result = consensus.evaluate(days, profile["name"], prediction_map)
    return {"mode": profile["mode"], "orders_allowed": False,
            "profile_sha256": hashlib.sha256(PROFILE.read_bytes()).hexdigest(),
            "dates": [first, last], "training_audit": training_audit, "result": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="first", required=True)
    parser.add_argument("--till", dest="last", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = replay(args.first, args.last)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    study.write(args.output, value)
    print(json.dumps({"mode": value["mode"], "dates": value["dates"], **study.compact(value["result"])}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
