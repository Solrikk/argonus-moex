#!/usr/bin/env python3
"""Этап 2, часть 1: признаки на 07:30 + исходы сделок для каждой (бумага, день).

Для каждой пары считает на момент 07:30 МСК (никакого заглядывания вперёд):
  gap        — открытие 07:00 к вчерашнему закрытию, %
  mom30      — импульс 07:00->07:30 (открытие первой свечи -> откр. свечи 07:30), %
  rs1, rs5   — относительная сила к IMOEX за 1 и 5 дней (по вчера), pp
  range_pos  — позиция вчерашнего закрытия в диапазоне вчерашнего дня, 0..1
  atr14_pct  — ATR14 по дневкам (по вчера), %
  volsurge   — объём 07:00-07:25 к медиане того же окна этой бумаги за прошлые дни
  bullish    — режим: IMOEX(вчера) выше EMA20(по вчера)
И исходы (вход 07:30, стоп 1%, стоп-раньше-цели в одной свече, выход 18:35):
  ret_{long,short}_t{20,30} — сделка с целью 2.0% / 3.0% от входа, брутто %.
Пишет features.json (список словарей). Только будни."""
from __future__ import annotations

from argonus.paths import UNIVERSE_DIR, WATCHLIST_DIR, PROJECT_ROOT
import os, sys, json, gzip
from datetime import date

HERE = str(UNIVERSE_DIR)
SP = HERE
W_START, W_END = "2025-11-01", "2026-07-16"

def calculate_ema(values, period):
    if not values: return 0.0
    k = 2.0 / (period + 1.0); ema = values[0]
    for v in values[1:]: ema = ema + (v - ema) * k
    return ema

def sim(sess, entry, direction, target_pct, stop_pct=1.0):
    sign = 1.0 if direction == "long" else -1.0
    tgt = entry * (1 + sign * target_pct / 100)
    stop = entry * (1 - sign * stop_pct / 100)
    last = entry
    for tm, o, h, l, c, v in sess:
        hit_t = h >= tgt if direction == "long" else l <= tgt
        hit_s = l <= stop if direction == "long" else h >= stop
        if hit_t and hit_s: return -stop_pct
        if hit_t: return target_pct
        if hit_s: return -stop_pct
        last = c
    return sign * (last / entry - 1) * 100

def main() -> int:
    idx = json.load(open(os.path.join(SP, "index_daily.json")))
    json.dump(idx, open(os.path.join(HERE, "index_daily.json"), "w"))
    idx_dates = [d for d, _ in idx]; idx_close = {d: c for d, c in idx}
    def idx_ret(ds, n):
        past = [c for d, c in idx if d < ds]
        return (past[-1] / past[-1 - n] - 1) * 100 if len(past) > n else None
    def ema20_bull(ds):
        past = [c for d, c in idx if d < ds]
        return len(past) >= 20 and past[-1] > calculate_ema(past[-20:], 20)

    manifest = json.load(open(os.path.join(HERE, "manifest.json")))
    rows = []
    for si, sym in enumerate(manifest["selected"]):
        gz = os.path.join(HERE, "five_min", f"{sym}.json.gz")
        dj = os.path.join(HERE, "dailies", f"{sym}.json")
        if not (os.path.exists(gz) and os.path.exists(dj)): continue
        try:
            fm = json.loads(gzip.open(gz, "rt").read())
            dailies = json.load(open(dj))
        except Exception:
            continue
        closes = {r[0]: r[4] for r in dailies}
        dlist = [r[0] for r in dailies]
        morning_vol_hist: list[float] = []
        for i, drow in enumerate(dailies):
            ds = drow[0]
            if not (W_START <= ds <= W_END):
                # всё равно накапливаем историю утренних объёмов до окна
                pass
            y, m, dd = map(int, ds.split("-"))
            if date(y, m, dd).weekday() >= 5: continue
            candles = fm.get(ds)
            if not candles: continue
            sig_win = [c for c in candles if "07:00" <= c[0] < "07:30"]
            rest = [c for c in candles if "07:30" <= c[0] <= "18:35"]
            mvol = sum(c[5] for c in sig_win) if sig_win else 0.0
            med = sorted(morning_vol_hist)[len(morning_vol_hist) // 2] if len(morning_vol_hist) >= 10 else None
            if sig_win: morning_vol_hist.append(mvol)
            if not (W_START <= ds <= W_END): continue
            if i < 15 or not sig_win or len(rest) < 3: continue
            prev = dailies[i - 1]
            if prev[0] >= ds: continue
            prev_close, prev_high, prev_low = prev[4], prev[2], prev[3]
            if prev_close <= 0 or prev[1] <= 0: continue
            entry = rest[0][1]
            if entry <= 3.0: continue
            open0700 = sig_win[0][1]
            # ATR14 по дневкам (по вчера)
            trs = []
            for j in range(max(1, i - 14), i):
                pc = dailies[j - 1][4]
                tr = max(dailies[j][2] - dailies[j][3], abs(dailies[j][2] - pc), abs(dailies[j][3] - pc))
                trs.append(tr / pc * 100 if pc > 0 else 0)
            atr = sum(trs) / len(trs) if trs else None
            r1 = (prev_close / dailies[i - 2][4] - 1) * 100 if i >= 2 and dailies[i - 2][4] > 0 else None
            r5 = (prev_close / dailies[i - 6][4] - 1) * 100 if i >= 6 and dailies[i - 6][4] > 0 else None
            ir1, ir5 = idx_ret(ds, 1), idx_ret(ds, 5)
            rng = prev_high - prev_low
            rows.append({
                "sym": sym, "date": ds, "month": ds[:7],
                "gap": (open0700 / prev_close - 1) * 100,
                "mom30": (entry / open0700 - 1) * 100,
                "rs1": (r1 - ir1) if (r1 is not None and ir1 is not None) else None,
                "rs5": (r5 - ir5) if (r5 is not None and ir5 is not None) else None,
                "range_pos": (prev_close - prev_low) / rng if rng > 0 else None,
                "atr": atr,
                "volsurge": (mvol / med) if (med and med > 0) else None,
                "bullish": ema20_bull(ds),
                "ret_long_t20": sim(rest, entry, "long", 2.0),
                "ret_short_t20": sim(rest, entry, "short", 2.0),
                "ret_long_t30": sim(rest, entry, "long", 3.0),
                "ret_short_t30": sim(rest, entry, "short", 3.0),
            })
        if (si + 1) % 20 == 0:
            print(f"  {si+1} бумаг, строк {len(rows)}", flush=True)
    out = os.path.join(HERE, "features.json")
    json.dump(rows, open(out, "w"))
    print(f"готово: {len(rows)} строк (бумага×день) -> {out}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
