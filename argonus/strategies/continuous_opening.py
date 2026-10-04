"""Closed-bar morning scanner over every eligible share, independent of lists."""
from __future__ import annotations

import statistics as st

from argonus.strategies import continuous_intraday as core
from argonus.strategies import intraday_signals as candles

SETUPS = ("opening_follow", "opening_fade", "gap_reclaim", "opening_union")


def scan(ds, tm, sessions, contexts):
    if not "07:20" <= tm <= "09:30":
        return [], {}
    features = {}
    for symbol, full in sessions.items():
        ctx = contexts.get(symbol)
        if not ctx or ctx["liquidity"] < 50_000_000 or not ctx.get("previous_close"):
            continue
        rows = [r for r in full if "07:00" <= r[0] < tm and candles.minutes(r[0]) + 5 <= candles.minutes(tm)]
        if len(rows) < 4 or rows[0][0] != "07:00" or candles.minutes(rows[-1][0]) + 5 != candles.minutes(tm):
            continue
        recent = rows[-min(7, len(rows)):]
        if any(candles.minutes(b[0]) - candles.minutes(a[0]) != 5 for a, b in zip(recent, recent[1:])):
            continue
        turnover = sum(candles.turnover(r) for r in rows[-6:])
        if turnover < 5_000_000:
            continue
        atr = st.fmean(max(b[2] - b[3], abs(b[2] - a[4]), abs(b[3] - a[4]))
                      for a, b in zip(rows[-15:-1], rows[-14:])) if len(rows) >= 15 else st.fmean(
                          max(b[2] - b[3], abs(b[2] - a[4]), abs(b[3] - a[4]))
                          for a, b in zip(rows[:-1], rows[1:]))
        if atr <= 0:
            continue
        price = rows[-1][4]
        features[symbol] = {"rows": rows, "price": price, "atr": atr,
                            "return": (price / rows[0][1] - 1) * 100,
                            "gap": (rows[0][1] / ctx["previous_close"] - 1) * 100,
                            "turnover": turnover,
                            "volume_ratio": rows[-1][5] / max(1, st.median(r[5] for r in rows[-7:-1]))}
    if not features:
        return [], {}
    market = st.median(f["return"] for f in features.values())
    output = []
    for symbol, f in features.items():
        rows, price, atr, ret = f["rows"], f["price"], f["atr"], f["return"]
        sign = 1 if ret >= 0 else -1
        relative = sign * (ret - market)
        last = rows[-1]
        previous = rows[-4:-1]
        top, bottom = max(r[2] for r in previous), min(r[3] for r in previous)

        def add(setup, direction, score):
            output.append(core.Opportunity(ds, symbol, "long" if direction > 0 else "short",
                                           setup, tm, price, score, 1., 2., f["turnover"]))

        if abs(ret) >= .50 and relative >= .30 and f["volume_ratio"] >= 1.25:
            if (sign > 0 and price > top) or (sign < 0 and price < bottom):
                add("opening_follow", sign, relative / max(.01, atr / price * 100))
        # A directional reversal candle confirms exhaustion before an intent.
        body_pct = (price / last[1] - 1) * 100
        if abs(ret) >= .80 and sign * body_pct <= -.10 and bottom < price < top:
            add("opening_fade", -sign, abs(ret - market) / max(.01, atr / price * 100))
        gap_sign = 1 if f["gap"] >= 0 else -1
        if abs(f["gap"]) >= .50 and gap_sign * ret <= -.20 and gap_sign * body_pct < 0:
            # The initial gap must still be open; do not fade after full recovery.
            remaining = gap_sign * (price / contexts[symbol]["previous_close"] - 1) * 100
            if remaining >= .30:
                add("gap_reclaim", -gap_sign, abs(f["gap"]) + abs(ret - market))
    return sorted(output, key=lambda x: (-x.score, x.symbol, x.setup)), {
        "market_return_pct": market, "eligible_symbols": len(features)}


def gate(name):
    setup = name.removeprefix("A_plus_")
    return lambda items, market, contexts: [i for i in items if setup == "opening_union" or i.setup == setup]
