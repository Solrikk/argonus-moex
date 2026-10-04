"""Production feature/model contract shared by live opening and full replay."""
from __future__ import annotations

import math
import os
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

from argonus.strategies import intraday_signals as signals

POLICY = "A_plus_mean"
MIN_NET_PCT = .15
FEATURE_COUNT = 19
LEARNERS = ("rolling_ridge", "rolling_boosting")


def vector(item, market, day):
    sign = 1 if item.direction == "long" else -1
    rows = [rs[("scanner", item.symbol)] for tm, rs in sorted(day["rows"].items())
            if "07:00" <= tm < item.decision_time and ("scanner", item.symbol) in rs]
    last, ctx = rows[-1], day["contexts"][item.symbol]
    ret = (last[4] / rows[0][1] - 1) * 100
    gap = (rows[0][1] / ctx["previous_close"] - 1) * 100
    width = max(1e-9, max(r[2] for r in rows) - min(r[3] for r in rows))
    position = (last[4] - min(r[3] for r in rows)) / width
    prior = [v for _, v in sorted(ctx["returns"].items())][-5:]
    atr = st.fmean(max(b[2] - b[3], abs(b[2] - a[4]), abs(b[3] - a[4]))
                  for a, b in zip(rows[:-1][-14:], rows[1:][-14:]))
    values = [sign, *(int(item.setup == n) for n in ("opening_follow", "opening_fade", "gap_reclaim")),
              item.score, sign * ret, sign * gap, sign * (ret - market["market_return_pct"]),
              sign * sum(prior) * 100, st.pstdev(ctx["returns"].values()) * 100,
              atr / last[4] * 100, position if sign > 0 else 1 - position,
              sign * (last[4] / last[1] - 1) * 100,
              (last[2] - max(last[1], last[4])) / width,
              (min(last[1], last[4]) - last[3]) / width,
              last[5] / max(1, st.median(r[5] for r in rows[-7:-1])),
              math.log1p(item.recent_turnover), math.log1p(ctx["liquidity"]),
              (signals.minutes(item.decision_time) - 420) / 60]
    if len(values) != FEATURE_COUNT or not all(math.isfinite(v) for v in values):
        raise ValueError("Invalid opening feature vector")
    return values


def train_models(rows, inference_month):
    cutoff = inference_month + "-01"
    train = [r for r in rows if r["date"] < cutoff]
    if len({r["date"] for r in train}) < 20:
        raise ValueError("Opening model needs at least 20 completed past training days")
    x, y = np.array([r["features"] for r in train]), np.array([r["net_return_pct"] for r in train])
    if x.shape[1] != FEATURE_COUNT or not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("Invalid opening training data")
    counts = Counter(r["date"] for r in train)
    weights = np.array([len(train) / len(counts) / counts[r["date"]] for r in train])
    ridge = make_pipeline(StandardScaler(), Ridge(alpha=200))
    ridge.fit(x, y, ridge__sample_weight=weights)
    boosting = HistGradientBoostingRegressor(max_iter=150, max_depth=3, max_leaf_nodes=8,
                                             learning_rate=.05, min_samples_leaf=100,
                                             l2_regularization=20, random_state=20261003,
                                             early_stopping=False)
    boosting.fit(x, y, sample_weight=weights)
    return dict(zip(LEARNERS, (ridge, boosting))), {
        "inference_month": inference_month, "latest_training_date": max(r["date"] for r in train),
        "training_rows": len(train), "training_days": len(counts),
    }


def rank_opportunities(items, market, day, models):
    if not items:
        return [], {}
    features = np.array([vector(i, market, day) for i in items])
    scores = {name: models[name].predict(features) for name in LEARNERS}
    ranked, audit = [], {}
    for index, item in enumerate(items):
        pair = [float(scores[n][index]) for n in LEARNERS]
        mean = sum(pair) / 2
        audit[item.identity] = {"ridge_net_pct": pair[0], "boosting_net_pct": pair[1],
                                "mean_net_pct": mean, "features": features[index].tolist()}
        if all(math.isfinite(v) for v in pair) and mean >= MIN_NET_PCT:
            ranked.append(replace(item, score=mean))
    return sorted(ranked, key=lambda i: (-i.score, i.symbol, i.setup)), audit
