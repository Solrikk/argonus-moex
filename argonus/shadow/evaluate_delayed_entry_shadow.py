#!/usr/bin/env python3
"""Post-session evaluator for the immutable 07:05 delayed-entry shadow.

The module is deliberately broker-free.  It accepts a pre-existing immutable
capture and completed five-minute candles, then compares:

* the control from its actual broker fill;
* the challenger from executable depth-50 order-book VWAP;
* the same challenger with an additional adverse 5 and 10 bps entry stress.

The absolute target, 1% stop, 18:35 exit, round-trip cost assumption, and
stop-first intrabar convention are frozen.  The five-minute bar containing an
entry observation is always excluded: a normal 07:05 capture therefore starts
evaluation at 07:10.  This is intentionally more conservative than the
historical 07:05-candle-open proxy and avoids using price action that occurred
before an executable quote was observed.

No result from this file can place an order or activate production trading.
"""
from __future__ import annotations

from argonus.paths import RUNTIME_DIR

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from argonus.shadow import delayed_entry_shadow as registry
from argonus.shadow import forward_shadow_research as stats_registry


SCHEMA_VERSION = 1
ENGINE_VERSION = "delayed-entry-shadow-evaluator-v1"
RECORD_TYPE_CAPTURE = "delayed_entry_shadow_capture"
RECORD_TYPE_OUTCOME = "delayed_entry_shadow_outcome"
POLICY = registry.POLICY
STOP_PCT = float(registry.STOP_PCT)
TIME_EXIT = str(registry.TIME_EXIT)
STRESS_BPS = tuple(int(value) for value in registry.STRESS_BPS)
MOSCOW = ZoneInfo("Europe/Moscow")
SESSION_COMPLETE_TIME = time(18, 40)
SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")
HISTORICAL_PROXY_DIFFERENCE = (
    "forward executable quote starts at the next complete bar; "
    "historical research entered at the exact 07:05 candle open"
)


class EvaluationError(ValueError):
    """Raised when evidence cannot support a conservative evaluation."""


@dataclass(frozen=True, slots=True)
class Candle:
    trade_date: str
    time: str
    open: float
    high: float
    low: float
    close: float


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"value is not canonical-JSON compatible: {exc}") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite(value: Any, field: str, *, positive: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"{field} must be numeric") from exc
    if not math.isfinite(result):
        raise EvaluationError(f"{field} must be finite")
    if positive and result <= 0.0:
        raise EvaluationError(f"{field} must be positive")
    return result


def _cost(value: Any, field: str) -> float:
    result = _finite(value, field)
    if result < 0.0 or result > 20.0:
        raise EvaluationError(f"{field} must be between 0 and 20 percent")
    return result


def _now_moscow(now: datetime | None = None) -> datetime:
    value = now or datetime.now(MOSCOW)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=MOSCOW)
    return value.astimezone(MOSCOW)


def _aware_timestamp(value: Any, field: str, trade_date: str | None) -> datetime:
    text = str(value or "").strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EvaluationError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvaluationError(f"{field} must include a timezone offset")
    local = parsed.astimezone(MOSCOW)
    if trade_date is not None and local.date().isoformat() != trade_date:
        raise EvaluationError(
            f"{field} must fall on trade_date in Europe/Moscow"
        )
    return local


def next_complete_bar(value: Any, trade_date: str) -> str:
    """Return the first full five-minute candle after an observation."""
    local = _aware_timestamp(value, "entry observation timestamp", trade_date)
    containing = local.replace(
        minute=local.minute - local.minute % 5,
        second=0,
        microsecond=0,
    )
    result = containing + timedelta(minutes=5)
    if result.date() != local.date():
        raise EvaluationError("entry observation is too late for same-day evaluation")
    return result.strftime("%H:%M")


def normalize_capture(
    record: Mapping[str, Any], now: datetime | None = None
) -> dict[str, Any]:
    """Validate a capture through the frozen registry and recheck boundaries."""
    raw = (
        record.get("_raw")
        if isinstance(record, Mapping) and isinstance(record.get("_raw"), Mapping)
        else record
    )
    try:
        frozen = registry.validate_capture(raw)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise EvaluationError(str(exc)) from exc
    if frozen.get("record_type") != RECORD_TYPE_CAPTURE:
        raise EvaluationError(f"unexpected record_type: {frozen.get('record_type')!r}")
    if frozen.get("execution_claim") is not False:
        raise EvaluationError("capture must explicitly set execution_claim=false")
    trade_date = str(frozen["trade_date"])
    current = _now_moscow(now)
    if date.fromisoformat(trade_date) > current.date():
        raise EvaluationError(f"future trade_date is forbidden: {trade_date}")
    challenger = frozen["challenger"]
    expected_start = next_complete_bar(challenger["captured_at"], trade_date)
    if challenger.get("evaluation_start_time") != expected_start:
        raise EvaluationError(
            "challenger.evaluation_start_time must exclude the capture-containing "
            f"bar: expected {expected_start}"
        )
    control_start = next_complete_bar(
        frozen["control"]["actual_fill_timestamp"], trade_date
    )
    return {
        **frozen,
        "control_evaluation_start_time": control_start,
        "capture_sha256": canonical_sha256(frozen),
        "_raw": frozen,
    }


def _records_from_document(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, list):
        rows = document
    elif isinstance(document, Mapping):
        if document.get("record_type") == RECORD_TYPE_CAPTURE:
            rows = [document]
        else:
            rows = None
            for key in ("captures", "records", "journal"):
                if isinstance(document.get(key), list):
                    rows = document[key]
                    break
            if rows is None:
                raise EvaluationError("JSON contains no delayed-entry capture list")
    else:
        raise EvaluationError("capture input must be a JSON object or array")
    result = [
        dict(row)
        for row in rows
        if isinstance(row, Mapping) and row.get("record_type") == RECORD_TYPE_CAPTURE
    ]
    if not result:
        raise EvaluationError("no delayed_entry_shadow_capture records found")
    return result


def load_capture_records(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    artifact = Path(path)
    if not artifact.is_file():
        raise EvaluationError(f"capture artifact does not exist: {artifact}")
    text = artifact.read_text(encoding="utf-8")
    if artifact.suffix.lower() == ".jsonl":
        rows: list[dict[str, Any]] = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EvaluationError(f"invalid capture JSONL line {number}: {exc}") from exc
            if isinstance(value, Mapping) and value.get("record_type") == RECORD_TYPE_CAPTURE:
                rows.append(dict(value))
        if not rows:
            raise EvaluationError("no delayed-entry captures found in JSONL")
        return rows
    try:
        return _records_from_document(json.loads(text))
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"invalid capture JSON: {exc}") from exc


def _hhmm(value: Any, field: str) -> str:
    text_value = str(value).strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::00)?", text_value)
    if not match:
        raise EvaluationError(f"{field} must be HH:MM")
    hour, minute = map(int, match.groups())
    try:
        parsed = time(hour, minute)
    except ValueError as exc:
        raise EvaluationError(f"{field} is invalid") from exc
    return parsed.strftime("%H:%M")


def _timestamp_parts(value: Any, default_date: str) -> tuple[str, str]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if abs(number) > 10_000_000_000:
            number /= 1000.0
        parsed = datetime.fromtimestamp(number, tz=MOSCOW)
        return parsed.date().isoformat(), parsed.strftime("%H:%M")
    text_value = str(value).strip()
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", text_value):
        return default_date, _hhmm(text_value, "candle.time")
    normalized = text_value[:-1] + "+00:00" if text_value.endswith("Z") else text_value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EvaluationError(f"invalid candle timestamp: {text_value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW)
    else:
        parsed = parsed.astimezone(MOSCOW)
    if parsed.second or parsed.microsecond:
        raise EvaluationError("candle timestamps must be minute-aligned")
    return parsed.date().isoformat(), parsed.strftime("%H:%M")


def _raw_candle_rows(document: Any) -> list[Any]:
    if isinstance(document, list):
        return document
    if isinstance(document, Mapping):
        for key in ("candles", "rows", "data"):
            if isinstance(document.get(key), list):
                return document[key]
    raise EvaluationError("candle document must be a list or contain candles/rows/data")


def normalize_candles(document: Any, trade_date: str | date) -> list[Candle]:
    expected_date = date.fromisoformat(str(trade_date)[:10]).isoformat()
    result: list[Candle] = []
    for number, row in enumerate(_raw_candle_rows(document), start=1):
        if isinstance(row, Mapping):
            timestamp = row.get("time", row.get("timestamp", row.get("datetime")))
            raw_values = [row.get(key) for key in ("open", "high", "low", "close")]
        elif isinstance(row, Sequence) and not isinstance(row, (str, bytes)) and len(row) >= 5:
            timestamp = row[0]
            raw_values = list(row[1:5])
        else:
            raise EvaluationError(f"invalid candle row {number}")
        row_date, row_time = _timestamp_parts(timestamp, expected_date)
        if row_date != expected_date:
            continue
        if int(row_time[3:5]) % 5:
            raise EvaluationError(
                f"candle row {number} is not aligned to a five-minute boundary"
            )
        open_price, high, low, close = [
            _finite(value, f"candle[{number}].{name}", positive=True)
            for value, name in zip(raw_values, ("open", "high", "low", "close"))
        ]
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            raise EvaluationError(f"invalid OHLC ordering in candle row {number}")
        result.append(Candle(expected_date, row_time, open_price, high, low, close))
    if not result:
        raise EvaluationError(f"no candles for trade_date {expected_date}")
    result.sort(key=lambda candle: candle.time)
    seen: set[str] = set()
    duplicates: set[str] = set()
    for candle in result:
        if candle.time in seen:
            duplicates.add(candle.time)
        seen.add(candle.time)
    if duplicates:
        raise EvaluationError(f"duplicate candle times: {sorted(duplicates)}")
    return result


def _canonical_candles(candles: Sequence[Candle]) -> list[list[Any]]:
    return [
        [item.trade_date, item.time, item.open, item.high, item.low, item.close]
        for item in candles
    ]


def _validate_completed_session(
    capture: Mapping[str, Any], candles: Sequence[Candle], now: datetime | None
) -> None:
    times = {item.time for item in candles}
    starts = (
        str(capture["control_evaluation_start_time"]),
        str(capture["challenger"]["evaluation_start_time"]),
    )
    first = min(starts)
    cursor = datetime.strptime(first, "%H:%M")
    finish = datetime.strptime(TIME_EXIT, "%H:%M")
    required: set[str] = set()
    while cursor <= finish:
        required.add(cursor.strftime("%H:%M"))
        cursor += timedelta(minutes=5)
    missing = sorted(required - times)
    if missing:
        preview = missing[:8]
        suffix = f" (+{len(missing) - len(preview)} more)" if len(missing) > len(preview) else ""
        raise EvaluationError(
            f"completed session is missing exact five-minute candles: {preview}{suffix}"
        )
    current = _now_moscow(now)
    trade_day = date.fromisoformat(str(capture["trade_date"]))
    if trade_day > current.date():
        raise EvaluationError("future session cannot be evaluated")
    if trade_day == current.date() and current.timetz().replace(tzinfo=None) < SESSION_COMPLETE_TIME:
        raise EvaluationError("session is not complete until 18:40 Europe/Moscow")


def _target_ahead(direction: str, entry_price: float, target_price: float) -> bool:
    sign = 1.0 if direction == "long" else -1.0
    return sign * (target_price / entry_price - 1.0) > 0.0


def _adverse_entry(entry_price: float, direction: str, stress_bps: int) -> float:
    sign = 1.0 if direction == "long" else -1.0
    return entry_price * (1.0 + sign * stress_bps / 10_000.0)


def simulate_policy(
    candles: Sequence[Candle],
    *,
    direction: str,
    entry_price: float,
    target_price: float,
    evaluation_start_time: str,
    cost_pct: float,
) -> dict[str, Any]:
    """Evaluate one entry with the single frozen stop-first convention."""
    if direction not in {"long", "short"}:
        raise EvaluationError(f"unsupported direction: {direction!r}")
    entry = _finite(entry_price, "entry_price", positive=True)
    target = _finite(target_price, "target_price", positive=True)
    cost = _cost(cost_pct, "cost_pct")
    if not _target_ahead(direction, entry, target):
        return {
            "evaluated": False,
            "exclusion_reason": "frozen_absolute_target_not_ahead",
            "entry_price": entry,
            "target_price": target,
            "evaluation_start_time": evaluation_start_time,
            "target_ahead": False,
        }
    window = [
        candle
        for candle in candles
        if evaluation_start_time <= candle.time <= TIME_EXIT
    ]
    if not window or window[0].time != evaluation_start_time or window[-1].time != TIME_EXIT:
        raise EvaluationError(
            f"incomplete candle window {evaluation_start_time}..{TIME_EXIT}"
        )
    sign = 1.0 if direction == "long" else -1.0
    stop = entry * (1.0 - sign * STOP_PCT / 100.0)
    exit_price = window[-1].close
    exit_time = window[-1].time
    reason = "time_exit"
    ambiguous = False
    for candle in window:
        if direction == "long":
            stop_hit = candle.low <= stop
            target_hit = candle.high >= target
        else:
            stop_hit = candle.high >= stop
            target_hit = candle.low <= target
        if stop_hit:
            ambiguous = bool(target_hit)
            exit_price = stop
            exit_time = candle.time
            reason = "both_stop_first" if ambiguous else "stop"
            break
        if target_hit:
            exit_price = target
            exit_time = candle.time
            reason = "target"
            break
    gross = sign * (exit_price / entry - 1.0) * 100.0
    return {
        "evaluated": True,
        "entry_price": entry,
        "target_price": target,
        "stop_price": stop,
        "evaluation_start_time": evaluation_start_time,
        "exit_time": exit_time,
        "exit_price": exit_price,
        "exit_reason": reason,
        "ambiguous": ambiguous,
        "gross_return_pct": gross,
        "round_trip_cost_pct": cost,
        "net_return_pct": gross - cost,
        "target_ahead": True,
        "gap_fill_convention": "frozen_threshold_price",
        "ambiguous_candle_policy": "stop_first",
    }


def _gate_reasons(capture: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    challenger = capture["challenger"]
    gates = capture["gates"]
    for name in (
        "capture_window",
        "quote_freshness",
        "depth_coverage",
        "target_ahead",
        "control_fill_before_quote",
    ):
        if gates.get(name) is not True:
            reasons.append(f"gate_failed:{name}")
    if challenger.get("entry_price_source") != "executable_orderbook_vwap":
        reasons.append("non_executable_entry_source")
    if challenger.get("freshness_pass") is not True:
        reasons.append("stale_orderbook_quote")
    if challenger.get("depth_sufficient") is not True:
        reasons.append("insufficient_orderbook_depth")
    if challenger.get("target_ahead") is not True:
        reasons.append("frozen_absolute_target_not_ahead")
    if capture.get("execution_claim") is not False:
        reasons.append("execution_claim_must_be_false")
    if capture.get("promotion_eligible") is not True:
        reasons.append("capture_marked_ineligible")
    return sorted(set(reasons))


def evaluate_capture(
    capture: Mapping[str, Any],
    candles: Sequence[Candle] | Any,
    candle_source: Mapping[str, Any] | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate one immutable capture after the completed session."""
    normalized = normalize_capture(capture, now=now)
    if candles and isinstance(candles, Sequence) and isinstance(candles[0], Candle):
        normalized_candles = list(candles)
    else:
        normalized_candles = normalize_candles(candles, normalized["trade_date"])
    _validate_completed_session(normalized, normalized_candles, now)

    direction = str(normalized["direction"])
    target = _finite(normalized["target_price"], "target_price", positive=True)
    cost = _cost(
        normalized["assumed_round_trip_cost_pct"],
        "assumed_round_trip_cost_pct",
    )
    control_entry = _finite(
        normalized["control"]["actual_fill_price"],
        "control.actual_fill_price",
        positive=True,
    )
    control = simulate_policy(
        normalized_candles,
        direction=direction,
        entry_price=control_entry,
        target_price=target,
        evaluation_start_time=normalized["control_evaluation_start_time"],
        cost_pct=cost,
    )

    challenger = normalized["challenger"]
    raw_vwap = challenger.get("executable_vwap")
    vwap = (
        _finite(
            raw_vwap,
            "challenger.executable_vwap",
            positive=True,
        )
        if raw_vwap is not None
        else None
    )
    scenarios: dict[str, dict[str, Any]] = {}
    for stress_bps in STRESS_BPS:
        if vwap is None:
            result = {
                "evaluated": False,
                "exclusion_reason": "no_full_depth_executable_vwap",
                "entry_price": None,
                "target_price": target,
                "evaluation_start_time": challenger["evaluation_start_time"],
                "target_ahead": False,
            }
        else:
            stressed_entry = _adverse_entry(vwap, direction, stress_bps)
            result = simulate_policy(
                normalized_candles,
                direction=direction,
                entry_price=stressed_entry,
                target_price=target,
                evaluation_start_time=challenger["evaluation_start_time"],
                cost_pct=cost,
            )
        result["stress_bps"] = stress_bps
        result["reference_executable_vwap"] = vwap
        result["stress_direction"] = "adverse_entry"
        scenarios[str(stress_bps)] = result

    gate_reasons = _gate_reasons(normalized)
    if not control["evaluated"]:
        gate_reasons.append("control_not_evaluable")
    if not scenarios[str(STRESS_BPS[0])]["evaluated"]:
        gate_reasons.append("challenger_base_not_evaluable")
    gate_reasons = sorted(set(gate_reasons))
    eligible = not gate_reasons

    comparisons: dict[str, dict[str, Any]] = {}
    for key, scenario in scenarios.items():
        comparable = bool(control["evaluated"] and scenario["evaluated"])
        comparisons[key] = {
            "stress_bps": int(key),
            "comparable": comparable,
            "challenger_minus_control_pp": (
                scenario["net_return_pct"] - control["net_return_pct"]
                if comparable
                else None
            ),
        }

    source = (
        dict(candle_source)
        if isinstance(candle_source, Mapping)
        else {"kind": "offline_supplied", "locator": str(candle_source)}
    )
    raw_capture = normalized["_raw"]
    evaluated_at = _now_moscow(now).isoformat()
    body = {
        "schema_version": SCHEMA_VERSION,
        "record_type": RECORD_TYPE_OUTCOME,
        "capture_mode": "forward",
        "policy": POLICY,
        "shadow_only": True,
        "production_effect": "none",
        "execution_claim": False,
        "trade_date": normalized["trade_date"],
        "symbol": normalized["symbol"],
        "uid": normalized["uid"],
        "direction": direction,
        "plan_id": normalized["plan_id"],
        "capture_id": normalized["capture_id"],
        "capture_sha256": canonical_sha256(raw_capture),
        "manifest_sha256": normalized["manifest_sha256"],
        "evaluated_at": evaluated_at,
        "evaluator_sha256": sha256_file(__file__),
        "candle_source": source,
        "candles_sha256": canonical_sha256(_canonical_candles(normalized_candles)),
        "contract": {
            "absolute_target": "frozen_pre_trade_target",
            "target_price": target,
            "stop_pct_from_each_entry": STOP_PCT,
            "time_exit": TIME_EXIT,
            "round_trip_cost_pct": cost,
            "ambiguous_candle_policy": "stop_first",
            "capture_bar_excluded": True,
            "historical_proxy_difference": HISTORICAL_PROXY_DIFFERENCE,
        },
        "control": control,
        "challenger": {
            "entry_price_source": challenger["entry_price_source"],
            "executable_vwap": vwap,
            "captured_at": challenger["captured_at"],
            "orderbook_ts": challenger["orderbook_ts"],
            "requested_lots": challenger["requested_lots"],
            "covered_lots": challenger["covered_lots"],
            "evaluation_start_time": challenger["evaluation_start_time"],
            "scenarios": scenarios,
        },
        "evidence_gates": dict(normalized["gates"]),
        "eligible_for_promotion_analysis": eligible,
        "ineligibility_reasons": gate_reasons,
        "comparison": comparisons,
    }
    return {**body, "outcome_id": canonical_sha256(body)}


def validate_outcome(outcome: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute immutable identity and the fail-closed eligibility decision."""
    if not isinstance(outcome, Mapping):
        raise EvaluationError("outcome must be a JSON object")
    frozen = json.loads(canonical_bytes(dict(outcome)))
    supplied_id = frozen.pop("outcome_id", None)
    expected_id = canonical_sha256(frozen)
    if supplied_id != expected_id:
        raise EvaluationError("outcome_id does not match canonical outcome body")
    expected_top_level = {
        "schema_version",
        "record_type",
        "capture_mode",
        "policy",
        "shadow_only",
        "production_effect",
        "execution_claim",
        "trade_date",
        "symbol",
        "uid",
        "direction",
        "plan_id",
        "capture_id",
        "capture_sha256",
        "manifest_sha256",
        "evaluated_at",
        "evaluator_sha256",
        "candle_source",
        "candles_sha256",
        "contract",
        "control",
        "challenger",
        "evidence_gates",
        "eligible_for_promotion_analysis",
        "ineligibility_reasons",
        "comparison",
    }
    if set(frozen) != expected_top_level:
        raise EvaluationError(
            f"outcome field set mismatch: {sorted(set(frozen) ^ expected_top_level)}"
        )
    if frozen.get("record_type") != RECORD_TYPE_OUTCOME:
        raise EvaluationError("unexpected outcome record_type")
    if frozen.get("schema_version") != SCHEMA_VERSION:
        raise EvaluationError("unexpected outcome schema_version")
    if frozen.get("policy") != POLICY:
        raise EvaluationError("unexpected outcome policy")
    if frozen.get("capture_mode") != "forward":
        raise EvaluationError("outcome is not forward evidence")
    if frozen.get("shadow_only") is not True or frozen.get("production_effect") != "none":
        raise EvaluationError("outcome must remain shadow-only")
    if frozen.get("execution_claim") is not False:
        raise EvaluationError("outcome cannot claim execution")
    for name in (
        "plan_id",
        "capture_id",
        "capture_sha256",
        "manifest_sha256",
        "evaluator_sha256",
        "candles_sha256",
    ):
        value = str(frozen.get(name) or "")
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise EvaluationError(f"{name} is not a lowercase SHA-256 digest")
    if frozen["evaluator_sha256"] != sha256_file(__file__):
        raise EvaluationError("outcome was not produced by this registered evaluator")
    if not str(frozen.get("symbol") or "").strip() or not str(frozen.get("uid") or "").strip():
        raise EvaluationError("outcome symbol/uid is missing")
    _aware_timestamp(frozen.get("evaluated_at"), "evaluated_at", None)
    if not isinstance(frozen.get("candle_source"), Mapping):
        raise EvaluationError("outcome candle_source must be an object")

    direction = str(frozen.get("direction"))
    if direction not in {"long", "short"}:
        raise EvaluationError("outcome direction is invalid")
    sign = 1.0 if direction == "long" else -1.0
    contract = frozen.get("contract")
    if not isinstance(contract, Mapping):
        raise EvaluationError("outcome contract is missing")
    expected_contract_fields = {
        "absolute_target",
        "target_price",
        "stop_pct_from_each_entry",
        "time_exit",
        "round_trip_cost_pct",
        "ambiguous_candle_policy",
        "capture_bar_excluded",
        "historical_proxy_difference",
    }
    if set(contract) != expected_contract_fields:
        raise EvaluationError("outcome contract field set changed")
    if contract.get("absolute_target") != "frozen_pre_trade_target":
        raise EvaluationError("outcome target contract changed")
    if contract.get("capture_bar_excluded") is not True:
        raise EvaluationError("outcome must exclude the capture-containing bar")
    if contract.get("historical_proxy_difference") != HISTORICAL_PROXY_DIFFERENCE:
        raise EvaluationError("outcome historical-proxy warning changed")
    if contract.get("ambiguous_candle_policy") != "stop_first":
        raise EvaluationError("outcome intrabar convention changed")
    if str(contract.get("time_exit")) != TIME_EXIT:
        raise EvaluationError("outcome time_exit changed")
    stop_pct = _finite(contract.get("stop_pct_from_each_entry"), "contract.stop_pct")
    if not math.isclose(stop_pct, STOP_PCT, rel_tol=0.0, abs_tol=1e-12):
        raise EvaluationError("outcome stop_pct changed")
    cost = _cost(contract.get("round_trip_cost_pct"), "contract.round_trip_cost_pct")
    target = _finite(contract.get("target_price"), "contract.target_price", positive=True)

    def validate_result(result: Any, label: str) -> None:
        if not isinstance(result, Mapping) or not isinstance(result.get("evaluated"), bool):
            raise EvaluationError(f"{label} has no evaluated flag")
        common_fields = {
            "evaluated",
            "entry_price",
            "target_price",
            "evaluation_start_time",
            "target_ahead",
        }
        if label.startswith("challenger.scenarios."):
            common_fields |= {
                "stress_bps",
                "reference_executable_vwap",
                "stress_direction",
            }
        expected_fields = (
            common_fields
            | {
                "stop_price",
                "exit_time",
                "exit_price",
                "exit_reason",
                "ambiguous",
                "gross_return_pct",
                "round_trip_cost_pct",
                "net_return_pct",
                "gap_fill_convention",
                "ambiguous_candle_policy",
            }
            if result["evaluated"]
            else common_fields | {"exclusion_reason"}
        )
        if set(result) != expected_fields:
            raise EvaluationError(f"{label} field set changed")
        entry_value = result.get("entry_price")
        if result["evaluated"]:
            entry = _finite(entry_value, f"{label}.entry_price", positive=True)
            result_target = _finite(result.get("target_price"), f"{label}.target_price", positive=True)
            if not math.isclose(result_target, target, rel_tol=0.0, abs_tol=1e-12):
                raise EvaluationError(f"{label} mutated the frozen target")
            expected_stop = entry * (1.0 - sign * STOP_PCT / 100.0)
            actual_stop = _finite(result.get("stop_price"), f"{label}.stop_price", positive=True)
            if not math.isclose(actual_stop, expected_stop, rel_tol=0.0, abs_tol=1e-10):
                raise EvaluationError(f"{label} stop is inconsistent with its entry")
            exit_price = _finite(result.get("exit_price"), f"{label}.exit_price", positive=True)
            expected_gross = sign * (exit_price / entry - 1.0) * 100.0
            gross = _finite(result.get("gross_return_pct"), f"{label}.gross_return_pct")
            net = _finite(result.get("net_return_pct"), f"{label}.net_return_pct")
            if not math.isclose(gross, expected_gross, rel_tol=0.0, abs_tol=1e-10):
                raise EvaluationError(f"{label} gross return is inconsistent")
            if not math.isclose(net, gross - cost, rel_tol=0.0, abs_tol=1e-10):
                raise EvaluationError(f"{label} net return is inconsistent")
            if not math.isclose(
                _cost(result.get("round_trip_cost_pct"), f"{label}.round_trip_cost_pct"),
                cost,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise EvaluationError(f"{label} cost is inconsistent")
            if result.get("ambiguous_candle_policy") != "stop_first":
                raise EvaluationError(f"{label} intrabar convention changed")
            if result.get("gap_fill_convention") != "frozen_threshold_price":
                raise EvaluationError(f"{label} gap convention changed")
            if result.get("target_ahead") is not True:
                raise EvaluationError(f"{label} evaluated a target that was not ahead")
            reason = result.get("exit_reason")
            if reason not in {"target", "stop", "both_stop_first", "time_exit"}:
                raise EvaluationError(f"{label} exit reason is invalid")
            if not isinstance(result.get("ambiguous"), bool):
                raise EvaluationError(f"{label} ambiguous flag is invalid")
            if (reason == "both_stop_first") is not result["ambiguous"]:
                raise EvaluationError(f"{label} ambiguity metadata is inconsistent")
        else:
            if result.get("exclusion_reason") not in {
                "frozen_absolute_target_not_ahead",
                "no_full_depth_executable_vwap",
            }:
                raise EvaluationError(f"{label} has an unknown exclusion reason")
            if result.get("target_ahead") is not False:
                raise EvaluationError(f"{label} excluded target metadata is inconsistent")

    control = frozen.get("control")
    validate_result(control, "control")
    challenger = frozen.get("challenger")
    if not isinstance(challenger, Mapping):
        raise EvaluationError("outcome challenger is missing")
    expected_challenger_fields = {
        "entry_price_source",
        "executable_vwap",
        "captured_at",
        "orderbook_ts",
        "requested_lots",
        "covered_lots",
        "evaluation_start_time",
        "scenarios",
    }
    if set(challenger) != expected_challenger_fields:
        raise EvaluationError("outcome challenger field set changed")
    source = challenger.get("entry_price_source")
    requested_lots = challenger.get("requested_lots")
    covered_lots = challenger.get("covered_lots")
    if (
        isinstance(requested_lots, bool)
        or not isinstance(requested_lots, int)
        or requested_lots <= 0
        or isinstance(covered_lots, bool)
        or not isinstance(covered_lots, int)
        or not 0 <= covered_lots <= requested_lots
    ):
        raise EvaluationError("outcome challenger lot coverage is invalid")
    _aware_timestamp(challenger.get("captured_at"), "challenger.captured_at", str(frozen["trade_date"]))
    _aware_timestamp(challenger.get("orderbook_ts"), "challenger.orderbook_ts", str(frozen["trade_date"]))
    scenarios = challenger.get("scenarios")
    if not isinstance(scenarios, Mapping) or set(scenarios) != {
        str(value) for value in STRESS_BPS
    }:
        raise EvaluationError("outcome stress scenario set changed")
    vwap_raw = challenger.get("executable_vwap")
    vwap = (
        _finite(vwap_raw, "challenger.executable_vwap", positive=True)
        if vwap_raw is not None
        else None
    )
    for stress_bps in STRESS_BPS:
        key = str(stress_bps)
        scenario = scenarios[key]
        validate_result(scenario, f"challenger.scenarios.{key}")
        if scenario.get("stress_bps") != stress_bps:
            raise EvaluationError(f"challenger scenario {key} stress label changed")
        if scenario.get("stress_direction") != "adverse_entry":
            raise EvaluationError(f"challenger scenario {key} stress direction changed")
        if scenario.get("reference_executable_vwap") != vwap_raw:
            raise EvaluationError(f"challenger scenario {key} VWAP reference changed")
        if vwap is None:
            if scenario.get("evaluated") is not False or scenario.get("entry_price") is not None:
                raise EvaluationError(f"challenger scenario {key} invented a depth fill")
        else:
            expected_entry = _adverse_entry(vwap, direction, stress_bps)
            actual_entry = _finite(scenario.get("entry_price"), f"scenario {key} entry", positive=True)
            if not math.isclose(actual_entry, expected_entry, rel_tol=0.0, abs_tol=1e-10):
                raise EvaluationError(f"challenger scenario {key} stress entry changed")

    comparisons = frozen.get("comparison")
    if not isinstance(comparisons, Mapping) or set(comparisons) != set(scenarios):
        raise EvaluationError("outcome comparison set changed")
    for stress_bps in STRESS_BPS:
        key = str(stress_bps)
        comparison = comparisons[key]
        scenario = scenarios[key]
        if not isinstance(comparison, Mapping) or set(comparison) != {
            "stress_bps",
            "comparable",
            "challenger_minus_control_pp",
        }:
            raise EvaluationError(f"comparison {key} field set changed")
        expected_comparable = bool(control["evaluated"] and scenario["evaluated"])
        if comparison.get("stress_bps") != stress_bps or comparison.get("comparable") is not expected_comparable:
            raise EvaluationError(f"comparison {key} metadata is inconsistent")
        expected_delta = (
            scenario["net_return_pct"] - control["net_return_pct"]
            if expected_comparable
            else None
        )
        actual_delta = comparison.get("challenger_minus_control_pp")
        if expected_delta is None:
            if actual_delta is not None:
                raise EvaluationError(f"comparison {key} must be null")
        elif not math.isclose(
            _finite(actual_delta, f"comparison {key} delta"),
            expected_delta,
            rel_tol=0.0,
            abs_tol=1e-10,
        ):
            raise EvaluationError(f"comparison {key} delta is inconsistent")

    gates = frozen.get("evidence_gates")
    expected_gate_names = {
        "capture_window",
        "quote_freshness",
        "depth_coverage",
        "target_ahead",
        "control_fill_before_quote",
    }
    if not isinstance(gates, Mapping) or set(gates) != expected_gate_names:
        raise EvaluationError("outcome evidence gate set changed")
    if any(not isinstance(value, bool) for value in gates.values()):
        raise EvaluationError("outcome evidence gates must be boolean")
    reasons: list[str] = [
        f"gate_failed:{name}" for name in sorted(expected_gate_names) if gates[name] is not True
    ]
    if source != "executable_orderbook_vwap":
        reasons.append("non_executable_entry_source")
    if gates["quote_freshness"] is not True:
        reasons.append("stale_orderbook_quote")
    if gates["depth_coverage"] is not True:
        reasons.append("insufficient_orderbook_depth")
    if gates["target_ahead"] is not True:
        reasons.append("frozen_absolute_target_not_ahead")
    if not all(gates.values()):
        reasons.append("capture_marked_ineligible")
    if control["evaluated"] is not True:
        reasons.append("control_not_evaluable")
    if scenarios[str(STRESS_BPS[0])]["evaluated"] is not True:
        reasons.append("challenger_base_not_evaluable")
    expected_reasons = sorted(set(reasons))
    actual_reasons = frozen.get("ineligibility_reasons")
    if not isinstance(actual_reasons, list) or any(not isinstance(item, str) for item in actual_reasons):
        raise EvaluationError("outcome ineligibility reasons must be a string list")
    if actual_reasons != expected_reasons:
        raise EvaluationError("outcome ineligibility reasons are inconsistent")
    expected_eligible = not expected_reasons
    if frozen.get("eligible_for_promotion_analysis") is not expected_eligible:
        raise EvaluationError("outcome promotion-analysis eligibility is inconsistent")
    return {**frozen, "outcome_id": supplied_id}


def _safe_component(value: Any, field: str) -> str:
    cleaned = SAFE_ID.sub("_", str(value)).strip("._")
    if not cleaned:
        raise EvaluationError(f"{field} cannot form a safe path")
    return cleaned[:160]


def _append_jsonl_once(path: Path, outcome: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_bytes(outcome).decode("utf-8")
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        for number, existing in enumerate(handle, start=1):
            if not existing.strip():
                continue
            try:
                row = json.loads(existing)
            except json.JSONDecodeError as exc:
                raise EvaluationError(f"immutable ledger has invalid line {number}") from exc
            if row.get("outcome_id") == outcome.get("outcome_id"):
                return
            if row.get("capture_id") == outcome.get("capture_id"):
                raise EvaluationError(
                    "immutable ledger already has a different outcome for capture "
                    f"{outcome.get('capture_id')}"
                )
        handle.seek(0, os.SEEK_END)
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_immutable_outcome(
    outcome: Mapping[str, Any],
    outcomes_dir: str | os.PathLike[str],
    ledger_path: str | os.PathLike[str] | None = None,
) -> tuple[Path, bool]:
    frozen = validate_outcome(outcome)
    root = Path(outcomes_dir)
    destination = (
        root
        / _safe_component(frozen["policy"], "policy")
        / _safe_component(frozen["trade_date"], "trade_date")
        / f"{_safe_component(frozen['capture_id'], 'capture_id')}.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        frozen, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n"
    temporary: str | None = None
    created = False
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
            created = True
        except FileExistsError:
            try:
                stored = json.loads(destination.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise EvaluationError(f"immutable outcome is unreadable: {destination}") from exc
            if stored.get("outcome_id") != frozen["outcome_id"]:
                raise EvaluationError(
                    f"immutable outcome conflict for capture {frozen['capture_id']}"
                )
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    ledger = Path(ledger_path) if ledger_path is not None else root / "outcomes.jsonl"
    _append_jsonl_once(ledger, frozen)
    return destination, created


def _compound(values: Sequence[float]) -> float:
    capital = 1.0
    for value in values:
        capital *= 1.0 + value / 100.0
    return (capital - 1.0) * 100.0


def _report_manifest_status(manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    expected_per_capture_gates = {
        "capture_window": True,
        "quote_freshness": True,
        "depth_coverage": True,
        "target_ahead": True,
        "control_fill_before_quote": True,
        "execution_claim": False,
        "candle_open_fallback_allowed": False,
    }
    expected_statistical_gates = {
        "minimum_eligible_captures": 50,
        "positive_delta_both_chronological_halves": True,
        "mdd_nonworse": True,
        "max_positive_contributor_share_pct": 35.0,
        "paired_sign_flip_p_max": 0.1,
        "positive_delta_required_at_stress_bps": 10,
    }
    expected_gates = {**expected_per_capture_gates, **expected_statistical_gates}
    if manifest is None:
        return {
            "manifest_verified": False,
            "manifest_sha256": None,
            "registered_gates": expected_gates,
            "per_capture_gates": expected_per_capture_gates,
            "statistical_gate_spec": expected_statistical_gates,
            "evidence_not_before": None,
            "statistical_gates_registered": True,
            "reason": "manifest_not_supplied_to_report",
        }
    if not isinstance(manifest, Mapping):
        raise EvaluationError("report manifest must be a mapping")
    if manifest.get("production_activation_allowed") is not False:
        raise EvaluationError("report manifest must forbid production activation")
    if manifest.get("promotion_gates") != expected_gates:
        raise EvaluationError("report manifest promotion gates changed")
    manifest_hash = str(manifest.get("manifest_sha256") or "")
    if len(manifest_hash) != 64 or any(
        character not in "0123456789abcdef" for character in manifest_hash
    ):
        raise EvaluationError("report manifest has no canonical manifest_sha256")
    try:
        evidence_not_before = date.fromisoformat(
            str(manifest.get("evidence_not_before"))
        ).isoformat()
    except ValueError as exc:
        raise EvaluationError("report manifest evidence_not_before is invalid") from exc
    return {
        "manifest_verified": True,
        "manifest_sha256": manifest_hash,
        "registered_gates": expected_gates,
        "per_capture_gates": expected_per_capture_gates,
        "statistical_gate_spec": expected_statistical_gates,
        "evidence_not_before": evidence_not_before,
        "statistical_gates_registered": True,
        "reason": "registered forward-only manual-review gates",
    }


def summarize_outcomes(
    outcomes: Sequence[Mapping[str, Any]],
    *,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an evidence-only report; never declare production activation."""
    manifest_status = _report_manifest_status(manifest)
    manifest_hash = manifest_status["manifest_sha256"]
    evidence_not_before = manifest_status["evidence_not_before"]
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    seen: set[str] = set()
    seen_dates: set[str] = set()
    for number, raw in enumerate(outcomes, start=1):
        try:
            row = validate_outcome(raw)
            if manifest_hash is not None and row["manifest_sha256"] != manifest_hash:
                raise EvaluationError("outcome manifest_sha256 does not match report manifest")
            if evidence_not_before is not None and row["trade_date"] < evidence_not_before:
                raise EvaluationError("outcome predates registered evidence window")
            if row["capture_id"] in seen:
                raise EvaluationError("duplicate capture_id")
            if row["trade_date"] in seen_dates:
                raise EvaluationError("duplicate trade_date")
            seen.add(row["capture_id"])
            seen_dates.add(row["trade_date"])
            accepted.append(row)
        except (EvaluationError, TypeError, ValueError) as exc:
            rejected.append({"record": str(number), "reason": str(exc)})
    accepted.sort(key=lambda row: (row["trade_date"], row["capture_id"]))
    eligible = [row for row in accepted if row["eligible_for_promotion_analysis"]]
    stress_report: dict[str, Any] = {}
    for stress_bps in STRESS_BPS:
        key = str(stress_bps)
        comparable = [
            row
            for row in eligible
            if row["comparison"][key]["comparable"]
        ]
        control_values = [row["control"]["net_return_pct"] for row in comparable]
        challenger_values = [
            row["challenger"]["scenarios"][key]["net_return_pct"]
            for row in comparable
        ]
        deltas = [
            row["comparison"][key]["challenger_minus_control_pp"]
            for row in comparable
        ]
        stress_report[key] = {
            "stress_bps": stress_bps,
            "comparable_observations": len(comparable),
            "control_compounded_return_pct": _compound(control_values),
            "challenger_compounded_return_pct": _compound(challenger_values),
            "compounded_delta_pp": _compound(challenger_values) - _compound(control_values),
            "mean_paired_delta_pp": sum(deltas) / len(deltas) if deltas else None,
            "positive_paired_deltas": sum(value > 0.0 for value in deltas),
        }
    base_key = str(STRESS_BPS[0])
    gate_spec = manifest_status["statistical_gate_spec"]
    base_rows = [
        row for row in eligible if row["comparison"][base_key]["comparable"]
    ]
    base_complete = bool(eligible) and len(base_rows) == len(eligible)
    base_control = [row["control"]["net_return_pct"] for row in base_rows]
    base_challenger = [
        row["challenger"]["scenarios"][base_key]["net_return_pct"]
        for row in base_rows
    ]
    split = len(base_rows) // 2
    first_control, second_control = base_control[:split], base_control[split:]
    first_challenger, second_challenger = (
        base_challenger[:split],
        base_challenger[split:],
    )
    first_delta = (
        _compound(first_challenger) - _compound(first_control)
        if first_control
        else 0.0
    )
    second_delta = (
        _compound(second_challenger) - _compound(second_control)
        if second_control
        else 0.0
    )
    log_deltas = [
        math.log1p(challenger / 100.0) - math.log1p(control / 100.0)
        for control, challenger in zip(base_control, base_challenger, strict=True)
    ]
    positive_log_deltas = [value for value in log_deltas if value > 0.0]
    positive_sum = sum(positive_log_deltas)
    max_contributor_share = (
        max(positive_log_deltas) / positive_sum * 100.0
        if positive_sum > 0.0
        else 100.0
    )
    sign_flip = stats_registry.paired_sign_flip_p_one_sided(log_deltas)
    control_mdd = stats_registry.trade_close_mdd(base_control)
    challenger_mdd = stats_registry.trade_close_mdd(base_challenger)
    stress_required = int(gate_spec["positive_delta_required_at_stress_bps"])
    stress_required_key = str(stress_required)
    stress_required_rows = [
        row
        for row in eligible
        if row["comparison"][stress_required_key]["comparable"]
    ]
    stress_required_complete = bool(eligible) and len(stress_required_rows) == len(eligible)
    stress_required_control = [
        row["control"]["net_return_pct"] for row in stress_required_rows
    ]
    stress_required_challenger = [
        row["challenger"]["scenarios"][stress_required_key]["net_return_pct"]
        for row in stress_required_rows
    ]
    stress_required_delta = (
        _compound(stress_required_challenger) - _compound(stress_required_control)
        if stress_required_complete
        else None
    )
    manifest_statistical_gates = {
        "minimum_eligible_captures": len(eligible)
        >= int(gate_spec["minimum_eligible_captures"]),
        "positive_delta_first_chronological_half": base_complete and first_delta > 0.0,
        "positive_delta_second_chronological_half": base_complete and second_delta > 0.0,
        "mdd_nonworse": base_complete and challenger_mdd >= control_mdd - 1e-12,
        "max_positive_contributor_share_at_most_35pct": (
            base_complete
            and max_contributor_share
            <= float(gate_spec["max_positive_contributor_share_pct"]) + 1e-12
        ),
        "paired_sign_flip_p_at_most_0_10": (
            base_complete
            and float(sign_flip["p_value"])
            <= float(gate_spec["paired_sign_flip_p_max"]) + 1e-12
        ),
        "positive_delta_at_10bps": (
            stress_required_complete
            and stress_required_delta is not None
            and stress_required_delta > 0.0
        ),
    }
    data_quality_gates = {
        "manifest_verified": manifest_status["manifest_verified"] is True,
        "base_comparable_for_every_eligible_outcome": base_complete,
        "stress_10bps_comparable_for_every_eligible_outcome": stress_required_complete,
    }
    evidence_gate_passed = all(manifest_statistical_gates.values()) and all(
        data_quality_gates.values()
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": "delayed_entry_shadow_evidence_report",
        "policy": POLICY,
        "accepted_outcomes": len(accepted),
        "rejected_outcomes": rejected,
        "eligible_outcomes": len(eligible),
        "ineligible_outcomes": len(accepted) - len(eligible),
        "manifest_gate_status": {
            **manifest_status,
            "eligible_outcomes": len(eligible),
            "all_accepted_outcomes_eligible": bool(accepted) and len(eligible) == len(accepted),
        },
        "statistics": {
            "primary_scenario_stress_bps": 0,
            "eligible_observations": len(eligible),
            "chronological_split_index": split,
            "first_half_delta_pp": first_delta,
            "second_half_delta_pp": second_delta,
            "control_compounded_return_pct": _compound(base_control),
            "challenger_compounded_return_pct": _compound(base_challenger),
            "full_delta_pp": _compound(base_challenger) - _compound(base_control),
            "control_trade_close_mdd_pct": control_mdd,
            "challenger_trade_close_mdd_pct": challenger_mdd,
            "max_positive_contributor_share_pct": max_contributor_share,
            "concentration_basis": "positive paired log-return deltas",
            "paired_sign_flip_one_sided": sign_flip,
            "required_stress_bps": stress_required,
            "required_stress_full_delta_pp": stress_required_delta,
        },
        "data_quality_gates": data_quality_gates,
        "statistical_gates": manifest_statistical_gates,
        "evidence_gate_passed": evidence_gate_passed,
        "manual_review_ready": evidence_gate_passed,
        "stress": stress_report,
        "historical_proxy_equivalence": False,
        "production_activation_allowed": False,
        "promotion_ready": False,
        "warning": (
            "Forward evidence only. manual_review_ready cannot activate production; "
            "a separate user-reviewed activation artifact would be required."
        ),
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"invalid JSON in {path}: {exc}") from exc


def _select_session(document: Any, capture: Mapping[str, Any]) -> Any:
    if isinstance(document, list):
        return document
    if not isinstance(document, Mapping):
        raise EvaluationError("candle source returned neither object nor array")
    if any(isinstance(document.get(key), list) for key in ("candles", "rows", "data")):
        return document
    container: Mapping[str, Any] = document
    for key in ("sessions", "by_capture", "by_symbol"):
        if isinstance(document.get(key), Mapping):
            container = document[key]
            break
    for key in (
        str(capture["capture_id"]),
        f"{capture['trade_date']}|{capture['symbol']}",
        f"{capture['trade_date']}_{capture['symbol']}",
        str(capture["symbol"]),
    ):
        if key in container:
            return container[key]
    raise EvaluationError(
        f"no candle session for {capture['trade_date']} {capture['symbol']}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate immutable delayed-entry captures after session close."
    )
    parser.add_argument("--captures", required=True, help="Capture JSON/JSONL journal.")
    parser.add_argument("--candles-file", required=True, help="Offline candle JSON; '-' reads stdin.")
    parser.add_argument("--capture-id", action="append")
    parser.add_argument("--outcomes-dir", default=str(RUNTIME_DIR / "delayed_entry_shadow_outcomes"))
    parser.add_argument("--ledger")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        captures = [normalize_capture(row) for row in load_capture_records(args.captures)]
        if args.capture_id:
            wanted = set(args.capture_id)
            captures = [row for row in captures if row["capture_id"] in wanted]
            missing = wanted - {row["capture_id"] for row in captures}
            if missing:
                raise EvaluationError(f"capture_id not found: {sorted(missing)}")
        if not captures:
            raise EvaluationError("no captures selected")
        if args.candles_file == "-":
            candle_document = json.load(sys.stdin)
            source = {"kind": "offline_stdin", "requested_after_trade": True}
        else:
            candle_path = Path(args.candles_file)
            if not candle_path.is_file():
                raise EvaluationError(f"candle file does not exist: {candle_path}")
            candle_document = _read_json(candle_path)
            source = {
                "kind": "offline_file",
                "path": str(candle_path.resolve()),
                "input_sha256": sha256_file(candle_path),
                "requested_after_trade": True,
            }
        if isinstance(candle_document, list) and len(captures) > 1:
            raise EvaluationError("flat candle list can evaluate only one capture")
        outcomes = []
        summaries = []
        for capture in captures:
            session = _select_session(candle_document, capture)
            outcome = evaluate_capture(capture, session, source)
            outcomes.append(outcome)
            path = None
            created = False
            if not args.dry_run:
                path, created = write_immutable_outcome(
                    outcome, args.outcomes_dir, ledger_path=args.ledger
                )
            base = outcome["challenger"]["scenarios"][str(STRESS_BPS[0])]
            summaries.append(
                {
                    "capture_id": outcome["capture_id"],
                    "outcome_id": outcome["outcome_id"],
                    "trade_date": outcome["trade_date"],
                    "symbol": outcome["symbol"],
                    "eligible_for_promotion_analysis": outcome[
                        "eligible_for_promotion_analysis"
                    ],
                    "control_net_return_pct": control_net
                    if (control_net := outcome["control"].get("net_return_pct")) is not None
                    else None,
                    "challenger_base_net_return_pct": base.get("net_return_pct"),
                    "path": str(path) if path is not None else None,
                    "created": created,
                    "dry_run": bool(args.dry_run),
                }
            )
        result = {
            "evaluated": len(outcomes),
            "outcomes": summaries,
            "report": summarize_outcomes(outcomes, manifest=registry.load_manifest()),
        }
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                indent=2 if args.pretty else None,
                allow_nan=False,
            )
        )
        return 0
    except (EvaluationError, OSError, json.JSONDecodeError) as exc:
        print(f"delayed-entry evaluation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
