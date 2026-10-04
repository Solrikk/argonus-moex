"""Materialize the frozen profit-first challenger for paper/shadow use.

No broker client, account mutation, or production activation. Target distance
is 1.25 times the original T4 distance from the confirmed entry, while planned
stop risk remains at most 1%. Prices respect the instrument's actual tick.
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path


from argonus.paths import BACKTEST_DIR

PROFILE = BACKTEST_DIR / "profit_first_2026-10-03/selected_candidate.json"


def positive(value: float | str, name: str) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return number


def plan(direction: str, entry_price: float | str, original_t4: float | str,
         tick_size: float | str) -> dict:
    if direction not in ("long", "short"):
        raise ValueError("Direction must be long or short")
    entry = positive(entry_price, "Entry price")
    t4 = positive(original_t4, "T4")
    tick = positive(tick_size, "Tick size")
    sign = Decimal(1 if direction == "long" else -1)
    if sign * (t4 - entry) <= 0:
        raise ValueError("Original T4 is behind the confirmed entry")
    target = entry + Decimal("1.25") * (t4 - entry)
    stop = entry * (Decimal(1) - sign * Decimal("0.01"))
    # Round toward entry: avoid a greater-than-planned stop risk or an
    # artificially more ambitious target caused solely by tick rounding.
    target_rounding = ROUND_FLOOR if direction == "long" else ROUND_CEILING
    stop_rounding = ROUND_CEILING if direction == "long" else ROUND_FLOOR
    target = (target / tick).to_integral_value(rounding=target_rounding) * tick
    stop = (stop / tick).to_integral_value(rounding=stop_rounding) * tick
    if target <= 0 or stop <= 0 or sign * (target - entry) <= 0 or sign * (entry - stop) <= 0:
        raise ValueError("Tick size cannot represent this protected trade")
    return {
        "status": "research_only",
        "direction": direction,
        "entry_price": str(entry), "original_t4": str(t4),
        "target_price": str(target), "stop_price": str(stop),
        "tick_size": str(tick), "target_distance_multiplier": 1.25,
        "planned_stop_distance_pct": float(abs(stop / entry - 1) * 100),
        "time_exit_msk": "18:35", "orders_allowed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direction", choices=("long", "short"), required=True)
    parser.add_argument("--entry", required=True)
    parser.add_argument("--t4", required=True)
    parser.add_argument("--tick", required=True)
    args = parser.parse_args()
    profile = json.loads(PROFILE.read_text())
    parameters = profile["rule"]["parameters"]
    if profile["status"] != "research_only" or profile["name"] != "target125" or (
        parameters["target_multiple"], parameters["stop_pct"], parameters["time_exit"]
    ) != (1.25, 1.0, "18:35"):
        raise RuntimeError("Frozen profile differs from the implemented challenger")
    print(json.dumps(plan(args.direction, args.entry, args.t4, args.tick), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
