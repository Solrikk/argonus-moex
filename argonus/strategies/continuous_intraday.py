"""Causal intraday opportunity scanner and shared-capital event portfolio.

Independent of watchlists, broker clients and the live trading bot. A caller
feeds closed candles, schedules intents, then supplies subsequent market bars.
The same event engine can be used for offline replay or paper execution.
"""
from __future__ import annotations

import math
import statistics as st
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date

from argonus.strategies import intraday_signals as candles


STRATEGIES = ("cash", "current_target125", "rolling_breakout", "rolling_pullback",
              "rolling_reversion", "regime_router", "target125_then_router", "single_snapshot_router")


def clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def ema(values, span=12):
    result = values[0]
    alpha = 2 / (span + 1)
    for value in values[1:]:
        result += alpha * (value - result)
    return result


@dataclass(frozen=True)
class Opportunity:
    date: str
    symbol: str
    direction: str
    setup: str
    decision_time: str
    reference: float
    score: float
    stop_pct: float
    target_pct: float
    recent_turnover: float
    source: str = "scanner"
    original_t4: float | None = None

    def __post_init__(self):
        date.fromisoformat(self.date)
        if self.direction not in ("long", "short") or self.source not in ("scanner", "legacy"):
            raise ValueError("Invalid opportunity direction/source")
        if candles.minutes(self.decision_time) % 5:
            raise ValueError("Intent time must align to five minutes")
        if any(not math.isfinite(x) for x in (self.reference, self.score, self.stop_pct, self.target_pct, self.recent_turnover)):
            raise ValueError("Invalid opportunity numbers")
        if self.reference <= 0 or not 0 < self.stop_pct < 100 or not 0 < self.target_pct < 100 or self.recent_turnover < 0:
            raise ValueError("Invalid opportunity geometry")

    @property
    def identity(self):
        return (self.date, self.symbol, self.direction, self.setup, self.decision_time)


def prior_context(daily, trade_date):
    rows = [row for row in daily if row[0] < trade_date]
    candles._validate_rows(rows)
    if len(rows) < 21:
        return None
    return {"liquidity": st.median(candles.turnover(r) for r in rows[-20:]),
            "returns": {row[0]: row[4] / previous[4] - 1
                        for previous, row in zip(rows[-21:-1], rows[-20:])}}


def _features(rows, decision_time):
    cutoff = candles.minutes(decision_time)
    past = [row for row in rows if "09:50" <= row[0] < decision_time
            and candles.minutes(row[0]) + 5 <= cutoff]
    candles._validate_rows(past)
    if len(past) < 8 or past[0][0] != "09:50" or candles.minutes(past[-1][0]) + 5 != cutoff:
        return None
    # Require uninterrupted recent signal bars; no interpolated prices.
    recent = past[-min(14, len(past)):]
    if any(candles.minutes(b[0]) - candles.minutes(a[0]) != 5 for a, b in zip(recent, recent[1:])):
        return None
    last, previous = past[-1], past[:-1]
    typical = lambda row: (row[2] + row[3] + row[4]) / 3
    volume = sum(row[5] for row in past)
    if volume <= 0:
        return None
    vwap_proxy = sum(typical(row) * row[5] for row in past) / volume
    atr = st.fmean(max(row[2] - row[3], abs(row[2] - prev[4]), abs(row[3] - prev[4]))
                   for prev, row in zip(past[:-1][-14:], past[1:][-14:]))
    if atr <= 0:
        return None
    hour = past[-13:]
    hourly_return = (hour[-1][4] / hour[0][1] - 1) * 100
    path_returns = [(row[4] / row[1] - 1) * 100 for row in hour]
    return {"rows": past, "last": last, "atr": atr, "vwap": vwap_proxy,
            "ema": ema([r[4] for r in previous[-24:]]), "hour_return": hourly_return,
            "returns": path_returns, "volume_ratio": last[5] / max(1, st.median(r[5] for r in previous[-12:])),
            "turnover": sum(candles.turnover(row) for row in past[-6:])}


def scan_opportunities(trade_date, decision_time, sessions, contexts):
    """All eligible names, three setups, no future OHLC or outcome ranking."""
    if decision_time < "10:30" or decision_time > "17:00" or candles.minutes(decision_time) % 5:
        return [], {"regime": "outside_window"}
    features = {}
    for symbol, rows in sessions.items():
        if not contexts.get(symbol) or contexts[symbol]["liquidity"] < 50_000_000:
            continue
        item = _features(rows, decision_time)
        if item and item["turnover"] >= 5_000_000:
            features[symbol] = item
    if not features:
        return [], {"regime": "no_eligible_market"}
    median_return = st.median(f["hour_return"] for f in features.values())
    width = min(len(f["returns"]) for f in features.values())
    market_path = [st.median(f["returns"][-width + i] for f in features.values()) for i in range(width)]
    efficiency = abs(sum(market_path)) / max(1e-12, sum(abs(x) for x in market_path))
    regime = "trend" if efficiency >= .35 else "range" if efficiency <= .25 else "wait"
    output = []
    for symbol, f in features.items():
        rows, last = f["rows"], f["last"]
        price, atr, vwap = last[4], f["atr"], f["vwap"]
        stop_pct = min(1.0, max(.30, 1.5 * atr / price * 100))
        # Structural distance must pay several round trips at registered costs.
        if 2 * stop_pct < 3 * (.08 + .10):
            continue
        def add(setup, sign, strength):
            output.append(Opportunity(trade_date, symbol, "long" if sign > 0 else "short",
                                      setup, decision_time, price, strength, stop_pct,
                                      2 * stop_pct, f["turnover"]))
        prior = rows[-7:-1]
        top, bottom = max(r[2] for r in prior), min(r[3] for r in prior)
        if f["volume_ratio"] >= 1.5:
            if price > top and price > vwap:
                add("rolling_breakout", 1, (price - top) / atr + f["volume_ratio"])
            elif price < bottom and price < vwap:
                add("rolling_breakout", -1, (bottom - price) / atr + f["volume_ratio"])
        sign = 1 if f["hour_return"] > 0 else -1
        relative = sign * (f["hour_return"] - median_return)
        if abs(f["hour_return"]) >= .30 and relative >= .15:
            crossed = rows[-2][4] <= f["ema"] < price if sign > 0 else rows[-2][4] >= f["ema"] > price
            if crossed and sign * (price - vwap) > 0:
                add("rolling_pullback", sign, relative / max(.01, atr / price * 100))
        deviation = (price - vwap) / atr
        spread = last[2] - last[3]
        if spread >= 2 * atr and abs(deviation) >= 2:
            if deviation > 0 and last[2] - max(last[1], price) >= .60 * spread:
                add("rolling_reversion", -1, abs(deviation))
            elif deviation < 0 and min(last[1], price) - last[3] >= .60 * spread:
                add("rolling_reversion", 1, abs(deviation))
    return sorted(output, key=lambda x: (-x.score, x.symbol, x.setup)), {
        "regime": regime, "efficiency": efficiency, "market_return_pct": median_return,
        "eligible_symbols": len(features),
    }


def route_opportunities(opportunities, market, strategy):
    if strategy == "single_snapshot_router" and opportunities and opportunities[0].decision_time != "10:30":
        return []
    if strategy in ("regime_router", "target125_then_router", "single_snapshot_router"):
        setups = {"rolling_breakout", "rolling_pullback"} if market["regime"] == "trend" else {
            "rolling_reversion"} if market["regime"] == "range" else set()
    else:
        setups = {strategy}
    return [item for item in opportunities if item.setup in setups]


def correlated(a, b, contexts):
    x, y = contexts[a]["returns"], contexts[b]["returns"]
    dates = sorted(x.keys() & y.keys())
    if len(dates) < 15:
        return True  # missing correlation evidence cannot authorize duplication
    xv, yv = [x[d] for d in dates], [y[d] for d in dates]
    if st.pstdev(xv) == 0 or st.pstdev(yv) == 0:
        return True
    return st.correlation(xv, yv) >= .85


class EventPortfolio:
    """Orders are paper intents; all positions compete for the same capacity."""
    def __init__(self, equity=50_000.0, bps=5.0, fee_pct=.04):
        if equity <= 0 or not 0 <= bps < 10000 or not 0 <= fee_pct < 100:
            raise ValueError("Invalid account or cost assumptions")
        self.start_equity = self.cash = float(equity)
        self.bps, self.fee_pct = bps, fee_pct
        self.positions, self.pending, self.ledger, self.daily = {}, [], [], []
        self.counters = Counter()
        self.close_peak = self.mark_peak = self.start_equity
        self.close_mdd = self.intrabar_mdd = 0.0
        self.max_gross = self.max_positions = 0
        self.last_step = None

    def begin_day(self, ds):
        if self.positions or self.pending:
            raise ValueError("Unclosed previous-day exposure")
        self.day = ds
        self.day_start = self.cash
        self.entries, self.exits, self.seen = Counter(), {}, set()
        self.day_ledger_start = len(self.ledger)

    def equity(self):
        return self.cash + sum(p["sign"] * p["qty"] * (p["mark"] - p["entry_price"]) for p in self.positions.values())

    def submit(self, opportunity, contexts):
        if opportunity.date != self.day or opportunity.identity in self.seen:
            return False
        self.seen.add(opportunity.identity)
        symbol = opportunity.symbol
        occupied = self.positions.keys() | {x.symbol for x in self.pending}
        if symbol in occupied or len(occupied) >= 3:
            self.counters["capacity_or_duplicate"] += 1
            return False
        if opportunity.source != "legacy":
            if self.equity() <= self.day_start * .97 or sum(self.entries.values()) + len(self.pending) >= 12:
                self.counters["daily_risk_cutoff"] += 1
                return False
            if self.entries[symbol] >= 2 or candles.minutes(opportunity.decision_time) < self.exits.get(symbol, -999) + 30:
                self.counters["repeat_limit_or_cooldown"] += 1
                return False
            for other in occupied:
                direction = self.positions[other]["direction"] if other in self.positions else next(x.direction for x in self.pending if x.symbol == other)
                if direction == opportunity.direction and (other not in contexts or symbol not in contexts or correlated(symbol, other, contexts)):
                    self.counters["correlated_position"] += 1
                    return False
            if any(p.get("stale") for p in self.positions.values()):
                self.counters["stale_position_blocks_entry"] += 1
                return False
        self.pending.append(opportunity)
        return True

    def _close(self, symbol, raw_price, tm, reason):
        p = self.positions.pop(symbol)
        price = raw_price * (1 - p["sign"] * self.bps / 10000)
        gross = p["sign"] * p["qty"] * (price - p["entry_price"])
        exit_fee = p["qty"] * price * self.fee_pct / 100
        pnl = gross - p["entry_fee"] - exit_fee
        self.cash += gross - exit_fee
        self.exits[symbol] = candles.minutes(tm) + 5
        self.ledger.append({k: v for k, v in p.items() if k not in ("qty", "sign", "mark", "stale")}
                           | {"date": self.day, "symbol": symbol, "exit_time": tm, "exit_price": price,
                              "reason": reason, "pnl_rub": pnl, "net_return_pct": pnl / p["notional_rub"] * 100})

    def _open(self, item, row, tm):
        if item.symbol in self.positions or len(self.positions) >= 3:
            self.counters["capacity_changed_before_fill"] += 1
            return
        sign = 1 if item.direction == "long" else -1
        entry = row[1] * (1 + sign * self.bps / 10000)
        if item.source != "legacy" and abs(entry / item.reference - 1) * 100 > .25 * item.stop_pct:
            self.counters["unfilled_chase"] += 1
            return
        target = entry * (1 + sign * item.target_pct / 100)
        if item.original_t4 is not None:
            if sign * (item.original_t4 - entry) <= 0:
                self.counters["target_behind_fill"] += 1
                return
            target = entry + 1.25 * (item.original_t4 - entry)
        eq = self.equity()
        gross = sum(p["qty"] * p["mark"] for p in self.positions.values())
        risk = sum(p["notional_rub"] * p["stop_pct"] / 100 for p in self.positions.values())
        notional = min(150000 if item.source == "legacy" else 50000,
                       max(0, min(150000, 3 * eq) - gross),
                       max(0, .03 * eq - risk) / (item.stop_pct / 100))
        if item.source != "legacy":
            if eq <= .97 * self.day_start or self.entries[item.symbol] >= 2 or sum(self.entries.values()) >= 12:
                self.counters["risk_changed_before_fill"] += 1
                return
            notional = min(notional, .01 * item.recent_turnover)
        if notional < 5000 or target <= 0:
            self.counters["insufficient_capacity_at_fill"] += 1
            return
        entry_fee = notional * self.fee_pct / 100
        self.cash -= entry_fee
        self.positions[item.symbol] = {
            "direction": item.direction, "setup": item.setup, "source": item.source,
            "decision_time": item.decision_time, "entry_time": tm, "entry_price": entry,
            "stop_price": entry * (1 - sign * item.stop_pct / 100), "target_price": target,
            "stop_pct": item.stop_pct, "notional_rub": notional, "entry_fee": entry_fee,
            "qty": notional / entry, "sign": sign, "mark": row[1], "stale": False,
        }
        self.entries[item.symbol] += 1
        self.max_positions = max(self.max_positions, len(self.positions))
        self.max_gross = max(self.max_gross, gross + notional)

    def step(self, tm, rows):
        """rows is keyed by (source, symbol), containing ONLY this 5m bar."""
        stamp = (self.day, tm)
        if self.last_step is not None and stamp <= self.last_step:
            raise ValueError("Portfolio bars must advance strictly; duplicate tick rejected")
        self.last_step = stamp
        # Existing open-gap exits release capital before new pending fills.
        for symbol, p in list(self.positions.items()):
            row = rows.get((p["source"], symbol))
            if row is None:
                p["stale"] = True
                self.counters["missing_bar_while_open"] += 1
                continue
            p["stale"], p["mark"] = False, row[1]
            if p["sign"] * (row[1] - p["stop_price"]) <= 0:
                self._close(symbol, row[1], tm, "stop_gap")
            elif p["sign"] * (row[1] - p["target_price"]) >= 0:
                self._close(symbol, p["target_price"], tm, "target_at_open")
            elif tm >= "18:35":
                self._close(symbol, row[1], tm, "time_exit_open")
        due, future = [], []
        for item in self.pending:
            scheduled = candles.minutes(item.decision_time) + 5
            (due if scheduled <= candles.minutes(tm) else future).append(item)
        self.pending = future
        for item in due:
            if candles.minutes(item.decision_time) + 5 != candles.minutes(tm):
                self.counters["expired_paper_intent"] += 1
                continue
            row = rows.get((item.source, item.symbol))
            if row is None:
                self.counters["unfilled_missing_entry"] += 1
            else:
                self._open(item, row, tm)
        adverse_equity = self.cash
        for symbol, p in list(self.positions.items()):
            row = rows.get((p["source"], symbol))
            if row is None:
                adverse_equity += p["sign"] * p["qty"] * (p["mark"] - p["entry_price"])
                continue
            stop_hit = row[3] <= p["stop_price"] if p["sign"] > 0 else row[2] >= p["stop_price"]
            target_hit = row[2] >= p["target_price"] if p["sign"] > 0 else row[3] <= p["target_price"]
            adverse = p["stop_price"] if stop_hit else row[3] if p["sign"] > 0 else row[2]
            adverse_fill = adverse * (1 - p["sign"] * self.bps / 10000)
            adverse_equity += p["sign"] * p["qty"] * (adverse_fill - p["entry_price"]) - p["qty"] * adverse_fill * self.fee_pct / 100
            if stop_hit:
                self._close(symbol, p["stop_price"], tm, "both_stop_first" if target_hit else "stop")
            elif target_hit:
                self._close(symbol, p["target_price"], tm, "target")
            else:
                p["mark"] = row[4]
        self.intrabar_mdd = min(self.intrabar_mdd, (adverse_equity / self.mark_peak - 1) * 100)
        self.mark_peak = max(self.mark_peak, self.equity())

    def end_day(self):
        if self.positions or self.pending:
            raise ValueError(f"Unclosed exposure or intent at end of {self.day}")
        self.close_peak = max(self.close_peak, self.cash)
        self.close_mdd = min(self.close_mdd, (self.cash / self.close_peak - 1) * 100)
        rows = self.ledger[self.day_ledger_start:]
        self.daily.append({"date": self.day, "trades": len(rows), "equity_before": self.day_start,
                           "equity_after": self.cash, "pnl_rub": self.cash - self.day_start})

    def result(self):
        return {"ending_equity_rub": self.cash, "pnl_rub": self.cash - self.start_equity,
                "return_pct": (self.cash / self.start_equity - 1) * 100,
                "trades": len(self.ledger), "wins": sum(x["pnl_rub"] > 0 for x in self.ledger),
                "days": len(self.daily), "active_days": sum(d["trades"] > 0 for d in self.daily),
                "days_with_multiple_trades": sum(d["trades"] > 1 for d in self.daily),
                "max_trades_per_day": max((d["trades"] for d in self.daily), default=0),
                "daily_close_mdd_pct": self.close_mdd, "intrabar_adverse_proxy_mdd_pct": self.intrabar_mdd,
                "max_simultaneous_positions": self.max_positions, "max_entry_exposure_rub": self.max_gross,
                "cost_rub": sum(x["entry_fee"] + x["notional_rub"] / x["entry_price"] * x["exit_price"] * self.fee_pct / 100 for x in self.ledger),
                "counters": dict(self.counters), "daily": self.daily, "ledger": self.ledger}
