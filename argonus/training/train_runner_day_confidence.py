#!/usr/bin/env python3
"""Обучение и валидация модели «уверенности дня» (runner_day_confidence_model.json).

Модель: логистическая регрессия P(идея дойдёт до T3+) по 13 дневным фичам
(wbt.RUNNER_DAY_FEATURES, считаются wbt.compute_runner_day_features — той же
функцией, что и в бою). Используется НЕ для выбора акции, а как фильтр дня:
если runner-prob финального выбора ниже порога — день лучше пропустить
(на таких днях EV исторически ~0).

Запуск (из директории проекта, нужны generated_watchlists_* и .tbank_token):
    python3 -m argonus.training.train_runner_day_confidence            # только LOMO-валидация
    WRITE_MODEL=1 python3 -m argonus.training.train_runner_day_confidence   # + записать модель
    KEEP_FRACTION=0.6  — доля торгуемых дней для выбора порога (по умолчанию 0.6)

Валидация честная: порог берётся как перцентиль pick-prob ТОЛЬКО train-месяцев,
применяется к hold-out месяцу (leave-one-month-out, без подглядывания).
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from argonus.paths import PROJECT_ROOT, MODEL_DIR, WATCHLIST_DIR

PROJECT_DIR = str(PROJECT_ROOT)

from argonus.backtesting import backtest_generated_watchlists as bt
from argonus.watchlists import watchlist_best_target as wbt

STOP = wbt.STOP_LOSS_PCT
FEATS = list(wbt.RUNNER_DAY_FEATURES)


def mfe(d, r, c):
    return (c.high / r - 1) * 100 if d == "long" else (r / c.low - 1) * 100


def mae(d, r, c):
    return max(0.0, (1 - c.low / r) * 100) if d == "long" else max(0.0, (c.high / r - 1) * 100)


def close_move(d, r, c):
    return (c.close / r - 1) * 100 if d == "long" else (r / c.close - 1) * 100


def target_move(d, r, p):
    return (p / r - 1) * 100 if d == "long" else (r / p - 1) * 100


def pnl(d, r, c, target_price, assume):
    """EV сделки за день: вход по ref, стоп 1%, выход по цели (стратегия T4).

    assume="stop"/"target" — какая граница считается первой, если за день
    зацепило и стоп, и цель (дневная свеча не даёт порядок внутри дня).
    """
    adverse = mae(d, r, c) >= STOP
    t = target_move(d, r, target_price)
    hit = mfe(d, r, c) >= t - 1e-9
    if adverse and hit:
        return -STOP if assume == "stop" else t
    if hit:
        return t
    if adverse:
        return -STOP
    return close_move(d, r, c)


def full_reach(d, idea, c):
    reached = 0
    for t in idea.targets:
        if (d == "long" and c.high >= t.price) or (d == "short" and c.low <= t.price):
            reached = max(reached, t.index)
    return reached


def main() -> int:
    wl = bt.discover_watchlists(str(WATCHLIST_DIR))
    if not wl:
        print("Нет generated_watchlists_*/watchlist_*.txt.", file=sys.stderr)
        return 1
    print(f"watchlists: {len(wl)} days", flush=True)
    stock_cache, index_candles = bt.build_candle_cache(4, wl)
    bt.patch_watchlist_data_access(stock_cache, index_candles)
    wbt.load_same_day_top_reranker_model()
    wbt.load_same_day_top_winner_reranker_model()
    wbt.load_same_day_top_t2plus_reranker_model()

    samples = []  # все кандидаты (обучающая выборка)
    day_pick = {}  # день -> sample финального выбора текущего пайплайна
    for e in wl:
        mo = e.trade_date.strftime("%Y-%m")
        try:
            analyses = wbt.analyze_watchlist(e.raw_text, e.trade_date)
        except Exception:
            analyses = []
        top_sym = analyses[0].symbol if analyses and not analyses[0].skipped_reason else None
        for idea in wbt.parse_watchlist(e.raw_text):
            candles = [c for c in stock_cache.get(idea.symbol, []) if c.trade_date < e.trade_date]
            if len(candles) < 25:
                continue
            try:
                c = bt.fetch_trade_day_candle(stock_cache, idea.symbol, e.trade_date)
            except Exception:
                continue
            sessions = wbt.candles_to_sessions(candles)
            ref = sessions[-1].close
            d = wbt.infer_direction(ref, idea.targets, idea.declared_direction)
            if d not in ("long", "short"):
                continue
            feats = wbt.compute_runner_day_features(sessions, idea, d, ref)
            if feats is None:
                continue
            depth = full_reach(d, idea, c)
            sample = dict(
                day=e.trade_date.isoformat(), month=mo, sym=idea.symbol, dir=d,
                ref=ref, candle=c, idea=idea, feat=feats, depth=depth,
                label=1 if depth >= 3 else 0,
            )
            samples.append(sample)
            if idea.symbol == top_sym:
                day_pick[sample["day"]] = sample

    X = np.array([[s["feat"][k] for k in FEATS] for s in samples])
    y = np.array([s["label"] for s in samples])
    months = np.array([s["month"] for s in samples])
    print(f"samples={len(samples)} picked_days={len(day_pick)} base_rate(T3+)={y.mean():.2f}", flush=True)

    def ev_of(s, assume):
        tmax = max(s["idea"].targets, key=lambda t: t.index).price
        return pnl(s["dir"], s["ref"], s["candle"], tmax, assume)

    uniq_months = sorted(set(months))
    print("\n=== LOMO: порог из перцентиля pick-prob train-месяцев (без подглядывания) ===")
    for keep in (1.0, 0.8, 0.7, 0.6, 0.5):
        total_tr, ev_tr_s, ev_tr_t = 0, 0.0, 0.0
        total_sk, ev_sk_s = 0, 0.0
        per_month = {}
        for m in uniq_months:
            tr = months != m
            mu = X[tr].mean(0)
            sd = X[tr].std(0)
            sd[sd == 0] = 1
            clf = LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced").fit(
                (X[tr] - mu) / sd, y[tr]
            )

            def prob(s):
                v = (np.array([s["feat"][k] for k in FEATS]) - mu) / sd
                return clf.predict_proba(v.reshape(1, -1))[0, 1]

            train_probs = sorted(prob(s) for s in day_pick.values() if s["month"] != m)
            thr = train_probs[int(round((1 - keep) * (len(train_probs) - 1)))]
            traded = [s for s in day_pick.values() if s["month"] == m and prob(s) >= thr]
            skipped = [s for s in day_pick.values() if s["month"] == m and prob(s) < thr]
            total_tr += len(traded)
            ev_tr_s += sum(ev_of(s, "stop") for s in traded)
            ev_tr_t += sum(ev_of(s, "target") for s in traded)
            total_sk += len(skipped)
            ev_sk_s += sum(ev_of(s, "stop") for s in skipped)
            per_month[m] = (
                len(traded),
                len(traded) + len(skipped),
                sum(ev_of(s, "stop") for s in traded) / len(traded) if traded else float("nan"),
            )
        pm = "  ".join(f"{m[-2:]}:{v[0]}/{v[1]}d,EV{v[2]:+.2f}" for m, v in sorted(per_month.items()))
        print(
            f"keep~{int(keep * 100)}%: traded {total_tr}/{total_tr + total_sk}d "
            f"EV={ev_tr_s / total_tr:+.2f}..{ev_tr_t / total_tr:+.2f}%/trade | "
            f"skipped {total_sk}d EV={ev_sk_s / total_sk if total_sk else 0:+.2f}% | {pm}",
            flush=True,
        )

    pred = np.full(len(samples), np.nan)
    for m in uniq_months:
        tr = months != m
        te = months == m
        mu = X[tr].mean(0)
        sd = X[tr].std(0)
        sd[sd == 0] = 1
        clf = LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced").fit(
            (X[tr] - mu) / sd, y[tr]
        )
        pred[te] = clf.predict_proba((X[te] - mu) / sd)[:, 1]
    print(f"\npooled LOMO AUC(T3+) = {roc_auc_score(y, pred):.3f}")

    keep_fraction = float(os.environ.get("KEEP_FRACTION", "0.6"))
    # Лонгам — строже (L2, валидировано LOMO на 6 мес: лонги генератора слабы,
    # торгуются только при высокой уверенности; шорты — обычный порог).
    long_pct = float(os.environ.get("LONG_PCT", "70"))
    mu = X.mean(0)
    sd = X.std(0)
    sd[sd == 0] = 1
    clf = LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced").fit((X - mu) / sd, y)
    pick_probs = sorted(
        clf.predict_proba(((np.array([[s["feat"][k] for k in FEATS]]) - mu) / sd))[0, 1]
        for s in day_pick.values()
    )
    threshold = pick_probs[int(round((1 - keep_fraction) * (len(pick_probs) - 1)))]
    threshold_long = pick_probs[int(round(long_pct / 100 * (len(pick_probs) - 1)))]
    payload = {
        "features": FEATS,
        "scaler_mean": [float(v) for v in mu],
        "scaler_scale": [float(v) for v in sd],
        "coefficients": [float(v) for v in clf.coef_[0]],
        "intercept": float(clf.intercept_[0]),
        "threshold": float(threshold),
        "threshold_long": float(threshold_long),
        "keep_fraction": keep_fraction,
        "long_pct": long_pct,
        "label": "reach_T3plus",
        "train_months": uniq_months,
        "train_days": len(day_pick),
        "train_samples": len(samples),
    }
    out_path = str(MODEL_DIR / wbt.RUNNER_DAY_CONFIDENCE_MODEL_FILENAME)
    if os.environ.get("WRITE_MODEL") == "1":
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print(f"\nзаписано {out_path} (threshold={threshold:.3f}, threshold_long={threshold_long:.3f}, keep={keep_fraction})")
    else:
        print(
            f"\n(dry run) threshold={threshold:.3f} keep={keep_fraction}; "
            "WRITE_MODEL=1 чтобы сохранить модель"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
