#!/usr/bin/env python3
"""Обучение universe-модели плана Б: P(акция пройдёт >=2% от открытия в направлении).

Датасет: все ликвидные акции TQBR × все торговые дни доступных месяцев,
оба направления (фичи знаковые). Валидация leave-one-month-out, финальная
модель обучается на всём. Пишет universe_runner_model.json.

    python3 -m argonus.training.train_universe_runner               # только LOMO-отчёт
    WRITE_MODEL=1 python3 -m argonus.training.train_universe_runner # + сохранить модель
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from argonus.paths import PROJECT_ROOT, MODEL_DIR

PROJECT_DIR = str(PROJECT_ROOT)
from argonus.watchlists.universe_backup_pick import (MODEL_FILENAME, UNIVERSE_FEATURES,
                                  compute_universe_features, fetch_universe_candles)
from argonus.market_data.tbank_market_data import TBankInvestClient

MIN_VOLUME_RUB = 50_000_000
# С 2025-11: включает ралли-месяцы (ноя +6%, дек +3.4%) — без них модель
# не знала, как выглядят провальные шорты в сильном росте (ноя-2025 OOS: −9.7%).
TRAIN_START = date(2025, 11, 1)
RUNNER_PCT = 2.0
TARGET_PCT = 3.0   # параметры сделки мотора B (валидированное плато 2.5-4 / 1-2)
STOP_PCT = 1.5
HIGH_CONF_PCTILE = 25  # ставка 1.5% на сделках выше этого перцентиля pick-prob


def main() -> int:
    today = date.today()
    print("качаю вселенную…", flush=True)
    universe = fetch_universe_candles(today, lookback_days=(today - TRAIN_START).days + 120)
    print(f"акций с историей: {len(universe)}", flush=True)

    client = TBankInvestClient(user_agent="train-universe/1.0")
    iid = client.resolve_index_instrument_id("IMOEX", class_code_hint="SNDX")
    from datetime import timedelta
    idx = client.get_daily_candles(iid, start_date=TRAIN_START - timedelta(days=120), end_date=today)
    idx_dates = [c.trade_date.isoformat() for c in idx]
    idx_close = {c.trade_date.isoformat(): c.close for c in idx}

    def idx_ret(day: str, n: int) -> float:
        prior = [d for d in idx_dates if d < day]
        if len(prior) < n + 1:
            return 0.0
        return (idx_close[prior[-1]] / idx_close[prior[-1 - n]] - 1) * 100

    idx_cache: dict[str, tuple[float, float]] = {}

    X, y, months, days_col, dirs = [], [], [], [], []
    for ticker, cs in universe.items():
        for i in range(25, len(cs)):
            day = cs[i][0]
            if day < TRAIN_START.isoformat():
                continue
            hist = cs[max(0, i - 60):i]
            avg_vol_rub = sum(c[5] * c[4] for c in hist[-20:]) / min(20, len(hist))
            if avg_vol_rub < MIN_VOLUME_RUB:
                continue
            o, h, l = cs[i][1], cs[i][2], cs[i][3]
            if not o:
                continue
            if day not in idx_cache:
                idx_cache[day] = (idx_ret(day, 5), idx_ret(day, 20))
            r5, r20 = idx_cache[day]
            for d, mfe in (("long", (h / o - 1) * 100), ("short", (o / l - 1) * 100)):
                f = compute_universe_features(hist, d, r5, r20)
                if f is None:
                    continue
                X.append(f)
                y.append(1 if mfe >= RUNNER_PCT else 0)
                months.append(day[:7])
                days_col.append(day)
                dirs.append(d)

    X, y, months = np.array(X), np.array(y), np.array(months)
    days_col, dirs = np.array(days_col), np.array(dirs)
    uniq = sorted(set(months))
    print(f"сэмплов {len(X)}, месяцев {len(uniq)}, базовая частота {y.mean():.2f}")

    pred = np.full(len(X), np.nan)
    for m in uniq:
        tr = months != m
        te = months == m
        mu, sd = X[tr].mean(0), X[tr].std(0)
        sd[sd == 0] = 1
        clf = LogisticRegression(C=1.0, max_iter=3000, class_weight="balanced").fit((X[tr] - mu) / sd, y[tr])
        pred[te] = clf.predict_proba((X[te] - mu) / sd)[:, 1]
        print(f"  hold-out {m}: AUC={roc_auc_score(y[te], pred[te]):.3f}")
    print(f"  пул LOMO AUC = {roc_auc_score(y, pred):.3f}")

    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1
    clf = LogisticRegression(C=1.0, max_iter=3000, class_weight="balanced").fit((X - mu) / sd, y)
    # порог «уверенной» сделки: перцентиль вероятностей дневного топ-шорта
    # (с гвардом по 10д-росту) на обучающем периоде
    probs_all = clf.predict_proba((X - mu) / sd)[:, 1]
    dmom10_i = list(UNIVERSE_FEATURES).index("dmom10")
    pick_probs = []
    for day in sorted(set(days_col)):
        mask = (days_col == day) & (dirs == "short") & (X[:, dmom10_i] > -8.0)
        if mask.any():
            pick_probs.append(float(probs_all[mask].max()))
    high_conf = float(np.percentile(pick_probs, HIGH_CONF_PCTILE))
    payload = {
        "features": list(UNIVERSE_FEATURES),
        "scaler_mean": [float(v) for v in mu],
        "scaler_scale": [float(v) for v in sd],
        "coefficients": [float(v) for v in clf.coef_[0]],
        "intercept": float(clf.intercept_[0]),
        "label": f"mfe_from_open>={RUNNER_PCT}pct",
        "min_volume_rub": MIN_VOLUME_RUB,
        "target_pct": TARGET_PCT,
        "stop_pct": STOP_PCT,
        "max_10d_rally_pct": 8.0,
        "high_conf_threshold": high_conf,
        "train_months": uniq,
        "train_samples": int(len(X)),
    }
    out = str(MODEL_DIR / MODEL_FILENAME)
    if os.environ.get("WRITE_MODEL") == "1":
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print(f"записано {out}")
    else:
        print("(dry run) WRITE_MODEL=1 чтобы сохранить")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
