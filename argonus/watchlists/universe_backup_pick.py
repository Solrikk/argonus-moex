#!/usr/bin/env python3
"""План Б («второй мотор»): выбор шорта из ВСЕЙ вселенной TQBR.

Когда фильтр дня бракует выбор основного пайплайна, вместо пропуска дня
торгуется лучший шорт по universe-модели (P(ход >=2% от открытия завтра)).
Валидация (6 мес, LOMO, интрадей): основной бот 61 сделка +42.3%; комбо с
планом Б — 121 сделка (каждый день), +62.5%, все 6 месяцев в плюсе; плато
параметров широкое (цель 2.5-4%, стоп 1-2% — все варианты +52..63%).

Обучение: python3 -m argonus.training.train_universe_runner (пишет universe_runner_model.json).
Фичи считаются ЗДЕСЬ (compute_universe_features) — обучение и бой используют
одну функцию, менять синхронно не нужно.
"""
from __future__ import annotations

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from argonus.market_data.tbank_market_data import TBankInvestClient

from argonus.paths import PROJECT_ROOT, MODEL_DIR

PROJECT_DIR = str(PROJECT_ROOT)
MODEL_FILENAME = "universe_runner_model.json"

UNIVERSE_FEATURES = (
    "atr_expansion", "atr20_pct", "range_pos_dir", "dist_ema20_dir", "dmom5",
    "dmom10", "recent_expansion", "body_ratio", "consec_dir", "vol_trend", "avg_day_pct",
    # v2 (2026-07-02): относительная сила к IMOEX, вчерашний импульс, всплеск
    # объёма, близость пробоя экстремума. LOMO: EV топ-выбора R +0.33 -> +0.38.
    "dmom1", "rs5", "rs20", "vol_surge", "dist_extreme",
)


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2 / (period + 1)
    e = values[0]
    for x in values[1:]:
        e = e + k * (x - e)
    return e


def _atr_pct(cs: list[tuple], n: int) -> float:
    """cs: (date, open, high, low, close, volume)."""
    if len(cs) < n + 1:
        n = len(cs) - 1
    if n <= 0:
        return 0.0
    trs = []
    for i in range(len(cs) - n, len(cs)):
        pc = cs[i - 1][4]
        trs.append(max(cs[i][2] - cs[i][3], abs(cs[i][2] - pc), abs(cs[i][3] - pc)))
    return (sum(trs) / len(trs)) / cs[-1][4] * 100 if cs[-1][4] else 0.0


def compute_universe_features(
    cs: list[tuple],
    direction: str,
    idx_ret5: float = 0.0,
    idx_ret20: float = 0.0,
) -> list[float] | None:
    """cs — свечи ДО торгового дня, кортежи (date, o, h, l, c, vol); >=25 штук.
    idx_ret5/idx_ret20 — доходность IMOEX за 5/20 дней до этого дня (для rs5/rs20)."""
    if len(cs) < 25:
        return None
    closes = [c[4] for c in cs]
    highs = [c[2] for c in cs]
    lows = [c[3] for c in cs]
    vols = [c[5] for c in cs]
    sign = 1.0 if direction == "long" else -1.0
    a5, a20 = _atr_pct(cs, 5), _atr_pct(cs, 20)
    hi20, lo20 = max(highs[-20:]), min(lows[-20:])
    rng = max(hi20 - lo20, 1e-9)
    rpos = (closes[-1] - lo20) / rng
    e20 = _ema(closes[-20:], 20)
    day_ranges = [(highs[i] - lows[i]) / closes[i] * 100 for i in range(len(cs) - 20, len(cs)) if closes[i]]
    avg_day = sum(day_ranges) / len(day_ranges) if day_ranges else 0.01
    last3 = [(highs[i] - lows[i]) / closes[i] * 100 for i in range(len(cs) - 3, len(cs)) if closes[i]]
    bodies = [abs(cs[i][4] - cs[i][1]) / max(cs[i][2] - cs[i][3], 1e-9) for i in range(len(cs) - 5, len(cs))]
    consec = 0
    for i in range(len(cs) - 1, 0, -1):
        if (cs[i][4] - cs[i][1]) * sign > 0:
            consec += 1
        else:
            break
    r5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) > 6 else 0.0
    r20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) > 21 else 0.0
    return [
        a5 / max(a20, 1e-9),
        a20,
        rpos if direction == "long" else 1 - rpos,
        (closes[-1] / max(e20, 1e-9) - 1) * 100 * sign,
        r5 * sign,
        (closes[-1] / closes[-11] - 1) * 100 * sign if len(closes) > 11 else 0.0,
        (sum(last3) / len(last3) if last3 else 0.0) / max(avg_day, 1e-9),
        sum(bodies) / len(bodies),
        float(consec),
        (sum(vols[-5:]) / 5) / max(sum(vols[-20:]) / 20, 1e-9),
        avg_day,
        (closes[-1] / closes[-2] - 1) * 100 * sign if len(closes) > 2 else 0.0,
        (r5 - idx_ret5) * sign,
        (r20 - idx_ret20) * sign,
        vols[-1] / max(sum(vols[-20:]) / 20, 1e-9),
        ((closes[-1] - lo20) / max(closes[-1], 1e-9) * 100 if direction == "short"
         else (hi20 - closes[-1]) / max(closes[-1], 1e-9) * 100),
    ]


def fetch_index_returns(as_of: date) -> tuple[float, float]:
    """Доходность IMOEX за 5 и 20 торговых дней до as_of (для rs5/rs20)."""
    try:
        client = TBankInvestClient(user_agent="universe-pick/1.0")
        iid = client.resolve_index_instrument_id("IMOEX", class_code_hint="SNDX")
        cs = client.get_daily_candles(iid, start_date=as_of - timedelta(days=60),
                                      end_date=as_of - timedelta(days=1))
        closes = [c.close for c in cs]
        r5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) > 6 else 0.0
        r20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) > 21 else 0.0
        return r5, r20
    except Exception:  # noqa: BLE001
        return 0.0, 0.0


def load_model() -> dict | None:
    path = str(MODEL_DIR / MODEL_FILENAME)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def score(model: dict, features: list[float]) -> float:
    logit = model["intercept"]
    for x, mu, sd, w in zip(features, model["scaler_mean"], model["scaler_scale"], model["coefficients"]):
        logit += w * (x - mu) / (sd if sd else 1.0)
    logit = max(-30.0, min(30.0, logit))
    return 1.0 / (1.0 + math.exp(-logit))


def fetch_universe_candles(as_of: date, lookback_days: int = 120, workers: int = 8) -> dict[str, list[tuple]]:
    """Свечи всех акций TQBR до as_of (не включая): {ticker: [(date,o,h,l,c,vol)...]}."""
    client = TBankInvestClient(user_agent="universe-pick/1.0")
    shares = client.list_moex_shares()

    def fetch(share):
        try:
            cs = client.get_daily_candles(
                share.instrument_id,
                start_date=as_of - timedelta(days=lookback_days),
                end_date=as_of - timedelta(days=1),
            )
            return share.ticker, [
                (c.trade_date.isoformat(), c.open, c.high, c.low, c.close, c.volume_lots) for c in cs
            ]
        except Exception:  # noqa: BLE001
            return share.ticker, None

    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for ticker, cs in ex.map(fetch, shares):
            if cs and len(cs) >= 25:
                out[ticker] = cs
    return out


def find_backup_short(
    as_of: date,
    universe: dict[str, list[tuple]] | None = None,
    exclude_symbol: str | None = None,
) -> dict | None:
    """Лучший шорт дня по universe-модели. None — нет модели/кандидатов.
    exclude_symbol — не предлагать эту акцию (уже торгуется другим мотором)."""
    model = load_model()
    if model is None:
        return None
    min_volume = float(model.get("min_volume_rub", 50_000_000))
    # «Не вставать перед поездом»: не шортим акцию, выросшую сильнее чем на
    # max_10d_rally_pct за 10 дней. Валидация: декабрь-2025 (IMOEX +3.4%) без
    # фильтра план Б шортил ралли SFIN 9 дней подряд, сумма −5.4R; с фильтром
    # +6.0R; в янв-июн сохраняется ~89% прибыли. Плато порогов 6-10%.
    max_rally = float(model.get("max_10d_rally_pct", 8.0))
    dmom10_index = UNIVERSE_FEATURES.index("dmom10")
    avg_day_index = UNIVERSE_FEATURES.index("avg_day_pct")
    if universe is None:
        universe = fetch_universe_candles(as_of)
    idx_ret5, idx_ret20 = (0.0, 0.0)
    if len(model.get("features", [])) > 11:
        idx_ret5, idx_ret20 = fetch_index_returns(as_of)
    best = None
    for ticker, cs in universe.items():
        if exclude_symbol and ticker == exclude_symbol:
            continue
        avg_vol_rub = sum(c[5] * c[4] for c in cs[-20:]) / min(20, len(cs))
        if avg_vol_rub < min_volume:
            continue
        features = compute_universe_features(cs, "short", idx_ret5, idx_ret20)
        if features is None:
            continue
        if features[dmom10_index] < -max_rally:
            continue
        prob = score(model, features[:len(model["features"])])
        if best is None or prob > best["prob"]:
            best = {
                "symbol": ticker,
                "direction": "short",
                "prob": prob,
                "reference_close": cs[-1][4],
                "avg_day_pct": features[avg_day_index],
                "target_pct": float(model.get("target_pct", 3.0)),
                "stop_pct": float(model.get("stop_pct", 1.5)),
                "high_conf_threshold": float(model.get("high_conf_threshold", 0.85)),
            }
    return best


if __name__ == "__main__":
    pick = find_backup_short(date.today())
    print(json.dumps(pick, ensure_ascii=False, indent=2) if pick else "нет кандидата")
