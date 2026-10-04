#!/usr/bin/env python3
"""Pure fail-closed execution contract for the production 07:05 entry.

The historical study used the open of the 07:05 five-minute candle.  A live
order cannot claim that price.  This module therefore validates one fresh
depth-50 exchange book, measures the executable VWAP for the frozen lot count,
and returns the worst consumed level for a single marketable FOK limit order.

There are deliberately no broker, order, filesystem, or bot imports here.
"""
from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping, Sequence


POLICY = "delayed_0705_depth50_fok"
BOOK_DEPTH = 50
MAX_QUOTE_AGE_MS = 3_000
MAX_IMPACT_BPS = 10.0
TIME_IN_FORCE = "TIME_IN_FORCE_FILL_OR_KILL"


class EntryQuoteError(ValueError):
    """The book cannot safely support the frozen production entry."""


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise EntryQuoteError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise EntryQuoteError(f"{label} must be a positive integer") from exc
    if result < 1 or str(value).strip() not in (str(result), f"+{result}"):
        raise EntryQuoteError(f"{label} must be a positive integer")
    return result


def _finite_positive(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EntryQuoteError(f"{label} must be a positive number") from exc
    if not math.isfinite(result) or result <= 0.0:
        raise EntryQuoteError(f"{label} must be a positive number")
    return result


def _quotation(value: Any, label: str) -> float:
    if isinstance(value, Mapping):
        try:
            result = float(value.get("units", 0)) + float(value.get("nano", 0)) / 1_000_000_000
        except (TypeError, ValueError) as exc:
            raise EntryQuoteError(f"{label} quotation is invalid") from exc
        return _finite_positive(result, label)
    return _finite_positive(value, label)


def _timestamp(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise EntryQuoteError(f"{label} must be an ISO timestamp") from exc
    else:
        raise EntryQuoteError(f"{label} must be an ISO timestamp")
    if result.tzinfo is None or result.utcoffset() is None:
        raise EntryQuoteError(f"{label} must include timezone")
    return result


def _levels(raw: Any, label: str, *, descending: bool) -> list[dict[str, Any]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise EntryQuoteError(f"order book {label} must be a list")
    levels: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise EntryQuoteError(f"{label}[{index}] must be an object")
        levels.append(
            {
                "price": _quotation(item.get("price"), f"{label}[{index}].price"),
                "quantity": _positive_int(item.get("quantity"), f"{label}[{index}].quantity"),
            }
        )
    if not levels:
        raise EntryQuoteError(f"order book {label} is empty")
    prices = [float(row["price"]) for row in levels]
    if prices != sorted(prices, reverse=descending) or len(prices) != len(set(prices)):
        raise EntryQuoteError(f"order book {label} is not strictly price ordered")
    return levels


def normalize_orderbook(
    raw: Mapping[str, Any],
    *,
    expected_uid: str,
    expected_symbol: str,
    expected_depth: int = BOOK_DEPTH,
) -> dict[str, Any]:
    """Validate identity, timestamp, depth, ordering and both exchange sides."""
    if not isinstance(raw, Mapping):
        raise EntryQuoteError("GetOrderBook response must be an object")
    uid = str(raw.get("instrumentUid", raw.get("instrument_uid", "")) or "").strip()
    if not uid or uid != str(expected_uid):
        raise EntryQuoteError("order book instrument uid mismatch")
    ticker = str(raw.get("ticker", "") or "").strip().upper()
    if ticker and ticker != str(expected_symbol).strip().upper():
        raise EntryQuoteError("order book ticker mismatch")
    depth = _positive_int(raw.get("depth"), "order book depth")
    if depth != int(expected_depth):
        raise EntryQuoteError(f"order book depth must be exactly {expected_depth}")

    timestamp_values = [
        raw[key] for key in ("orderbookTs", "orderbook_ts") if raw.get(key)
    ]
    if not timestamp_values:
        raise EntryQuoteError("GetOrderBook response lacks mandatory orderbookTs")
    if raw.get("time"):
        timestamp_values.append(raw["time"])
    parsed = [_timestamp(value, "orderbookTs") for value in timestamp_values]
    if any(value != parsed[0] for value in parsed[1:]):
        raise EntryQuoteError("conflicting order book timestamps")

    bids = _levels(raw.get("bids"), "bids", descending=True)
    asks = _levels(raw.get("asks"), "asks", descending=False)
    if float(bids[0]["price"]) >= float(asks[0]["price"]):
        raise EntryQuoteError("order book is crossed or locked")
    return {
        "depth": depth,
        "instrument_uid": uid,
        "ticker": ticker or str(expected_symbol).strip().upper(),
        "orderbook_ts": parsed[0].isoformat(timespec="milliseconds"),
        "bids": bids,
        "asks": asks,
    }


def build_execution_quote(
    book: Mapping[str, Any],
    *,
    direction: str,
    requested_lots: int,
    target_price: float,
    received_at: datetime | str,
    max_quote_age_ms: int = MAX_QUOTE_AGE_MS,
    max_impact_bps: float = MAX_IMPACT_BPS,
) -> dict[str, Any]:
    """Return the executable VWAP and marketable FOK limit or fail closed."""
    if direction not in ("long", "short"):
        raise EntryQuoteError(f"unsupported direction {direction!r}")
    requested = _positive_int(requested_lots, "requested_lots")
    target = _finite_positive(target_price, "target_price")
    received = _timestamp(received_at, "received_at")
    formed = _timestamp(book.get("orderbook_ts"), "orderbook_ts")
    try:
        max_age = int(max_quote_age_ms)
        max_impact = float(max_impact_bps)
    except (TypeError, ValueError) as exc:
        raise EntryQuoteError("quote gates have invalid types") from exc
    if max_age < 0 or not math.isfinite(max_impact) or max_impact < 0.0:
        raise EntryQuoteError("quote gates must be non-negative")
    quote_age_ms = (received - formed).total_seconds() * 1_000.0
    if quote_age_ms < 0.0 or quote_age_ms > max_age:
        raise EntryQuoteError(
            f"order book is stale: age={quote_age_ms:.0f}ms, max={max_age}ms"
        )

    side = "asks" if direction == "long" else "bids"
    raw_levels = book.get(side)
    if not isinstance(raw_levels, Sequence) or isinstance(raw_levels, (str, bytes)):
        raise EntryQuoteError(f"normalized order book {side} is invalid")
    remaining = requested
    covered = 0
    notional = 0.0
    worst: float | None = None
    for item in raw_levels:
        if remaining <= 0:
            break
        if not isinstance(item, Mapping):
            raise EntryQuoteError(f"normalized order book {side} is invalid")
        price = _finite_positive(item.get("price"), f"{side}.price")
        quantity = _positive_int(item.get("quantity"), f"{side}.quantity")
        used = min(quantity, remaining)
        notional += used * price
        covered += used
        remaining -= used
        worst = price
    if covered != requested or worst is None:
        raise EntryQuoteError(
            f"order book depth is insufficient: {covered}/{requested} lots"
        )

    best = _finite_positive(raw_levels[0].get("price"), f"{side}.best")
    vwap = notional / requested
    impact_bps = (
        (vwap / best - 1.0) * 10_000.0
        if direction == "long"
        else (1.0 - vwap / best) * 10_000.0
    )
    if impact_bps < -1e-9:
        raise EntryQuoteError("order book VWAP has impossible favorable impact")
    impact_bps = max(impact_bps, 0.0)
    if impact_bps > max_impact:
        raise EntryQuoteError(
            f"VWAP impact is too large: {impact_bps:.3f}bps > {max_impact:.3f}bps"
        )
    target_ahead = target > worst if direction == "long" else target < worst
    if not target_ahead:
        raise EntryQuoteError(
            f"frozen target {target} is not ahead of worst executable price {worst}"
        )

    opposite = "bids" if direction == "long" else "asks"
    opposite_best = _finite_positive(book[opposite][0].get("price"), f"{opposite}.best")
    spread_bps = (float(book["asks"][0]["price"]) / float(book["bids"][0]["price"]) - 1.0) * 10_000.0
    return {
        "policy": POLICY,
        "side": side,
        "requested_lots": requested,
        "covered_lots": covered,
        "best_touch": best,
        "opposite_best_touch": opposite_best,
        "executable_vwap": vwap,
        "worst_price": worst,
        "limit_price": worst,
        "quote_age_ms": quote_age_ms,
        "impact_bps": impact_bps,
        "spread_bps": spread_bps,
        "target_ahead": True,
        "orderbook_ts": formed.isoformat(timespec="milliseconds"),
        "received_at": received.isoformat(timespec="milliseconds"),
        "time_in_force": TIME_IN_FORCE,
    }


def round_marketable_limit(price: float, increment: float, direction: str) -> float:
    """Never round a marketable buy down or a marketable sell up."""
    value = Decimal(str(_finite_positive(price, "price")))
    step = Decimal(str(_finite_positive(increment, "increment")))
    rounding = ROUND_CEILING if direction == "long" else ROUND_FLOOR
    if direction not in ("long", "short"):
        raise EntryQuoteError(f"unsupported direction {direction!r}")
    ticks = (value / step).to_integral_value(rounding=rounding)
    return float(ticks * step)


__all__ = [
    "BOOK_DEPTH",
    "EntryQuoteError",
    "MAX_IMPACT_BPS",
    "MAX_QUOTE_AGE_MS",
    "POLICY",
    "TIME_IN_FORCE",
    "build_execution_quote",
    "normalize_orderbook",
    "round_marketable_limit",
]
