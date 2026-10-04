#!/usr/bin/env python3
"""Post-trade evaluator for the frozen exit-shadow policy.

This module is deliberately standalone and read-only with respect to markets.
It never imports ``trade_bot`` or a broker SDK and contains no order methods.
It consumes an immutable pre-trade shadow plan plus a completed five-minute
session, evaluates the current control and the frozen challenger, and writes a
first-writer-wins outcome record.  The optional network source is HTTPS GET
only and is intended for a read-only candle-export service.

When the immutable plan contains ``entry_price``, it is the authoritative
frozen live fill for both engines.  The observed 07:00 candle open is retained
as comparison evidence and becomes the fallback only when ``entry_price`` is
null; that fallback is explicitly not claimed as a live fill.  All return math
is delegated to ``forward_shadow_research`` so CLI and promotion use exactly
one registered intrabar convention (stop first on an ambiguous candle).
"""
from __future__ import annotations

from argonus.paths import RUNTIME_DIR

import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from argonus.shadow import forward_shadow_research as registry


SCHEMA_VERSION = 1
ENGINE_VERSION = "canonical-forward-shadow-evaluator-v2"
POLICY = registry.EXIT_POLICY
RECORD_TYPE_PLAN = "exit_shadow_plan"
RECORD_TYPE_OUTCOME = "exit_shadow_outcome"
MOSCOW = ZoneInfo("Europe/Moscow")
ENTRY_TIME = "07:00"
CONTROL_STOP_PCT = float(registry.CONTROL_EXIT["stop_pct"])
CONTROL_TIME_EXIT = str(registry.CONTROL_EXIT["time_exit"])
CHALLENGER_STOP_PCT = float(registry.CHALLENGER_EXIT["stop_pct"])
CHALLENGER_TARGET_MULTIPLE = float(registry.CHALLENGER_EXIT["target_multiple"])
CHALLENGER_TIME_EXIT = str(registry.CHALLENGER_EXIT["time_exit"])
DEFAULT_ASSUMED_COST_PCT = 0.08
SESSION_COMPLETE_TIME = time(18, 40)
SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


class EvaluationError(ValueError):
    """Raised when an input cannot support a conservative evaluation."""


@dataclass(frozen=True, slots=True)
class Candle:
    trade_date: str
    time: str
    open: float
    high: float
    low: float
    close: float


def canonical_bytes(value: Any) -> bytes:
    """Encode canonical JSON used by all immutable identities."""
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


def sha256_canonical(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite_number(value: Any, field: str, *, positive: bool = False) -> float:
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
    result = _finite_number(value, field)
    if result < 0.0:
        raise EvaluationError(f"{field} must be non-negative")
    if result > 20.0:
        raise EvaluationError(f"{field} is implausibly large: {result}%")
    return result


def _now_moscow(now: datetime | None = None) -> datetime:
    value = now or datetime.now(MOSCOW)
    if value.tzinfo is None:
        return value.replace(tzinfo=MOSCOW)
    return value.astimezone(MOSCOW)


def _iso_date(value: Any, field: str) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"{field} must be an ISO date") from exc


def _hhmm(value: Any, field: str) -> str:
    text = str(value).strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::00)?", text)
    if not match:
        raise EvaluationError(f"{field} must be HH:MM")
    hour_value, minute_value = map(int, match.groups())
    try:
        parsed = time(hour_value, minute_value)
    except ValueError as exc:
        raise EvaluationError(f"{field} is not a valid time") from exc
    return parsed.strftime("%H:%M")


def _actual_entry_timing(value: Any, trade_date: str) -> dict[str, str]:
    """Validate a timezone-aware fill timestamp and derive the excluded bar."""
    if value is None:
        raise EvaluationError("entry_price requires actual_entry_timestamp")
    text_value = str(value).strip()
    normalized = text_value[:-1] + "+00:00" if text_value.endswith("Z") else text_value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EvaluationError("actual_entry_timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvaluationError("actual_entry_timestamp must include a timezone offset")
    local = parsed.astimezone(MOSCOW)
    if local.date().isoformat() != trade_date:
        raise EvaluationError("actual_entry_timestamp must fall on trade_date in Europe/Moscow")
    entry_bar = local.replace(
        minute=local.minute - local.minute % 5,
        second=0,
        microsecond=0,
    )
    evaluation_start = entry_bar + timedelta(minutes=5)
    return {
        "actual_entry_timestamp": parsed.isoformat(),
        "actual_entry_time": local.strftime("%H:%M:%S"),
        "entry_bar_time": entry_bar.strftime("%H:%M"),
        "evaluation_start_time": evaluation_start.strftime("%H:%M"),
    }


def _require_close(actual: Any, expected: float, field: str) -> None:
    value = _finite_number(actual, field)
    if not math.isclose(value, expected, rel_tol=0.0, abs_tol=1e-12):
        raise EvaluationError(f"{field} must be frozen at {expected}, got {value}")


def normalize_plan(record: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Strictly validate a plan with the canonical research registry."""
    try:
        frozen = registry.validate_exit_shadow_plan(record)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise EvaluationError(str(exc)) from exc
    current = _now_moscow(now)
    if date.fromisoformat(frozen["trade_date"]) > current.date():
        raise EvaluationError(f"future trade_date is forbidden: {frozen['trade_date']}")
    return {
        **frozen,
        "plan_sha256": registry.canonical_sha256(frozen),
        "_raw": frozen,
    }


def _records_from_document(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, list):
        candidates = document
    elif isinstance(document, Mapping):
        if document.get("record_type", RECORD_TYPE_PLAN) == RECORD_TYPE_PLAN and (
            "trade_date" in document or "plan_id" in document
        ):
            candidates = [document]
        else:
            candidates = None
            for key in ("plans", "shadow_plans", "records", "journal"):
                if isinstance(document.get(key), list):
                    candidates = document[key]
                    break
            if candidates is None:
                raise EvaluationError("JSON does not contain a shadow plan list")
    else:
        raise EvaluationError("plan input must be a JSON object or array")
    records = [
        dict(item)
        for item in candidates
        if isinstance(item, Mapping)
        and item.get("record_type", RECORD_TYPE_PLAN) == RECORD_TYPE_PLAN
    ]
    if not records:
        raise EvaluationError("no exit_shadow_plan records found")
    return records


def load_plan_records(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Load plans from JSON, a wrapper/journal JSON, or mixed JSONL."""
    artifact = Path(path)
    if not artifact.is_file():
        raise EvaluationError(f"plan artifact does not exist: {artifact}")
    text = artifact.read_text(encoding="utf-8")
    if artifact.suffix.lower() == ".jsonl":
        rows: list[dict[str, Any]] = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EvaluationError(f"invalid plan JSONL line {number}: {exc}") from exc
            if isinstance(item, Mapping) and item.get("record_type") == RECORD_TYPE_PLAN:
                rows.append(dict(item))
        if not rows:
            raise EvaluationError("no exit_shadow_plan records found in JSONL")
        return rows
    try:
        return _records_from_document(json.loads(text))
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"invalid plan JSON: {exc}") from exc


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
        raise EvaluationError(f"candle timestamp must be minute-aligned: {text_value!r}")
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
    """Normalize common offline five-minute JSON row formats."""
    expected_date = _iso_date(trade_date, "trade_date")
    values: list[Candle] = []
    for number, row in enumerate(_raw_candle_rows(document), start=1):
        if isinstance(row, Mapping):
            timestamp = row.get("time", row.get("timestamp", row.get("datetime")))
            raw_open = row.get("open")
            raw_high = row.get("high")
            raw_low = row.get("low")
            raw_close = row.get("close")
        elif isinstance(row, Sequence) and not isinstance(row, (str, bytes)) and len(row) >= 5:
            timestamp, raw_open, raw_high, raw_low, raw_close = row[:5]
        else:
            raise EvaluationError(f"invalid candle row {number}")
        row_date, row_time = _timestamp_parts(timestamp, expected_date)
        if row_date != expected_date:
            continue
        open_price = _finite_number(raw_open, f"candle[{number}].open", positive=True)
        high = _finite_number(raw_high, f"candle[{number}].high", positive=True)
        low = _finite_number(raw_low, f"candle[{number}].low", positive=True)
        close = _finite_number(raw_close, f"candle[{number}].close", positive=True)
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            raise EvaluationError(f"invalid OHLC ordering in candle row {number}")
        values.append(Candle(expected_date, row_time, open_price, high, low, close))
    if not values:
        raise EvaluationError(f"no candles for trade_date {expected_date}")
    values.sort(key=lambda candle: candle.time)
    times = [candle.time for candle in values]
    duplicates = sorted({item for item in times if times.count(item) > 1})
    if duplicates:
        raise EvaluationError(f"duplicate candle times: {duplicates}")
    return values


def _canonical_candles(candles: Sequence[Candle]) -> list[list[Any]]:
    return [
        [item.trade_date, item.time, item.open, item.high, item.low, item.close]
        for item in candles
    ]


def _validate_completed_session(plan: Mapping[str, Any], candles: Sequence[Candle], now: datetime | None) -> None:
    by_time = {item.time: item for item in candles}
    entry_evidence_time = (
        ENTRY_TIME if plan.get("entry_price") is None else str(plan["entry_bar_time"])
    )
    for required in (
        entry_evidence_time,
        str(plan["evaluation_start_time"]),
        CHALLENGER_TIME_EXIT,
        CONTROL_TIME_EXIT,
    ):
        if required not in by_time:
            raise EvaluationError(
                f"completed session evidence is missing exact {required} candle"
            )
    current = _now_moscow(now)
    trade_day = date.fromisoformat(str(plan["trade_date"]))
    if trade_day > current.date():
        raise EvaluationError("future session cannot be evaluated")
    if trade_day == current.date() and current.timetz().replace(tzinfo=None) < SESSION_COMPLETE_TIME:
        raise EvaluationError("current session is not complete until 18:40 Europe/Moscow")


def evaluate_plan(
    plan: Mapping[str, Any],
    candles: Sequence[Candle] | Any,
    candle_source: Mapping[str, Any] | str,
    assumed_cost_override: float | None = None,
    actual_cost_override: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate completed evidence, then delegate to the canonical engine."""
    # Never trust a caller-added normalization sentinel.  Revalidate the raw
    # immutable record on every evaluation boundary.
    raw_candidate = (
        plan.get("_raw")
        if isinstance(plan, Mapping) and isinstance(plan.get("_raw"), Mapping)
        else plan
    )
    normalized_plan = normalize_plan(raw_candidate, now=now)
    if candles and isinstance(candles, Sequence) and isinstance(candles[0], Candle):
        normalized_candles = list(candles)
    else:
        normalized_candles = normalize_candles(candles, normalized_plan["trade_date"])
    _validate_completed_session(normalized_plan, normalized_candles, now)
    assumed = (
        _cost(assumed_cost_override, "assumed_cost_override")
        if assumed_cost_override is not None
        else float(normalized_plan["assumed_round_trip_cost_pct"])
    )
    actual = (
        _cost(actual_cost_override, "actual_cost_override")
        if actual_cost_override is not None
        else None
    )
    raw_plan = normalized_plan.get("_raw", normalized_plan)
    if assumed != float(raw_plan["assumed_round_trip_cost_pct"]):
        # Cost assumptions are part of the immutable decision.  A CLI override
        # may only supply measured actual cost, never rewrite the assumption.
        raise EvaluationError("assumed cost override cannot mutate a frozen plan")
    rows = [
        [item.time, item.open, item.high, item.low, item.close]
        for item in normalized_candles
    ]
    try:
        return registry.evaluate_exit_shadow(
            raw_plan,
            rows,
            candle_source=(
                dict(candle_source)
                if isinstance(candle_source, Mapping)
                else {"kind": "offline_supplied", "locator": str(candle_source)}
            ),
            actual_round_trip_cost_pct=actual,
            evaluated_at=_now_moscow(now).isoformat(),
            evaluator_sha256=sha256_file(__file__),
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        raise EvaluationError(str(exc)) from exc


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
            if row.get("plan_id") == outcome.get("plan_id"):
                raise EvaluationError(
                    f"immutable ledger already has a different outcome for plan {outcome.get('plan_id')}"
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
    """Persist one first-writer-wins JSON record and append-once JSONL row."""
    try:
        outcome = registry.validate_shadow_outcome(
            outcome, policy=POLICY
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        raise EvaluationError(str(exc)) from exc
    required = ("outcome_id", "plan_id", "trade_date", "policy")
    missing = [field for field in required if not outcome.get(field)]
    if missing:
        raise EvaluationError(f"outcome is missing immutable keys: {missing}")
    root = Path(outcomes_dir)
    destination = (
        root
        / _safe_component(outcome["policy"], "policy")
        / _safe_component(outcome["trade_date"], "trade_date")
        / f"{_safe_component(outcome['plan_id'], 'plan_id')}.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        outcome, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n"
    created = False
    temporary: str | None = None
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
            if stored.get("outcome_id") != outcome.get("outcome_id"):
                raise EvaluationError(
                    f"immutable outcome conflict for plan {outcome.get('plan_id')}: {destination}"
                )
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    ledger = Path(ledger_path) if ledger_path is not None else root / "outcomes.jsonl"
    _append_jsonl_once(ledger, outcome)
    return destination, created


def _read_json_or_csv(path: Path) -> Any:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        rows = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise EvaluationError(f"invalid candle JSONL line {number}: {exc}") from exc
        return rows
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"invalid candle JSON: {exc}") from exc


def _select_session(document: Any, plan: Mapping[str, Any]) -> Any:
    if isinstance(document, list):
        return document
    if not isinstance(document, Mapping):
        raise EvaluationError("candle source returned neither object nor array")
    if any(isinstance(document.get(key), list) for key in ("candles", "rows", "data")):
        return document
    container: Mapping[str, Any] = document
    for key in ("sessions", "by_plan", "by_symbol"):
        if isinstance(document.get(key), Mapping):
            container = document[key]
            break
    lookup = [
        str(plan["plan_id"]),
        str(plan.get("decision_id") or ""),
        f"{plan['trade_date']}|{plan['symbol']}",
        f"{plan['trade_date']}_{plan['symbol']}",
        f"{plan['symbol']}_{plan['trade_date']}",
        str(plan["symbol"]),
    ]
    for key in lookup:
        if key and key in container:
            return container[key]
    raise EvaluationError(
        f"candle document has no session for {plan['trade_date']} {plan['symbol']}"
    )


def _resolve_candle_file(directory: Path, plan: Mapping[str, Any]) -> Path:
    names = [
        f"{plan['trade_date']}_{plan['symbol']}",
        f"{plan['symbol']}_{plan['trade_date']}",
    ]
    candidates = []
    for stem in names:
        candidates.extend(directory / f"{stem}{suffix}" for suffix in (".json", ".jsonl", ".csv"))
    candidates.extend(
        directory / str(plan["trade_date"]) / f"{plan['symbol']}{suffix}"
        for suffix in (".json", ".jsonl", ".csv")
    )
    found = [path for path in candidates if path.is_file()]
    if not found:
        raise EvaluationError(
            f"no offline candle file for {plan['trade_date']} {plan['symbol']} in {directory}"
        )
    if len(found) > 1:
        raise EvaluationError(f"ambiguous offline candle files: {[str(path) for path in found]}")
    return found[0]


def _load_https_session(
    template: str,
    plan: Mapping[str, Any],
    timeout: float,
    bearer_token_env: str | None,
) -> tuple[Any, dict[str, Any]]:
    if not template.startswith("https://"):
        raise EvaluationError("network candle source must use HTTPS GET")
    url = template.format(
        symbol=urllib.parse.quote(str(plan["symbol"]), safe=""),
        trade_date=urllib.parse.quote(str(plan["trade_date"]), safe=""),
        plan_id=urllib.parse.quote(str(plan["plan_id"]), safe=""),
    )
    headers = {"Accept": "application/json", "User-Agent": ENGINE_VERSION}
    if bearer_token_env:
        token = os.environ.get(bearer_token_env)
        if not token:
            raise EvaluationError(f"bearer token environment variable is unset: {bearer_token_env}")
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except Exception as exc:  # urllib exposes several transport-specific errors
        raise EvaluationError(f"read-only candle GET failed: {exc}") from exc
    try:
        document = json.loads(body)
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"candle GET returned invalid JSON: {exc}") from exc
    return document, {
        "kind": "https_get_read_only",
        "url": url,
        "response_sha256": hashlib.sha256(body).hexdigest(),
        "requested_after_trade": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate immutable exit-shadow plans after a completed session."
    )
    parser.add_argument("--plans", required=True, help="Exit shadow plan JSON/JSONL journal.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--candles-file", help="Offline JSON/JSONL/CSV; '-' reads JSON stdin.")
    source.add_argument("--candles-dir", help="Offline per-date/per-symbol candle directory.")
    source.add_argument(
        "--candles-url-template",
        help="Optional HTTPS GET-only JSON source with {symbol}/{trade_date}/{plan_id} placeholders.",
    )
    parser.add_argument("--bearer-token-env", help="Environment variable used only for GET Authorization.")
    parser.add_argument("--network-timeout", type=float, default=20.0)
    parser.add_argument("--plan-id", action="append", help="Evaluate only selected plan_id; repeatable.")
    parser.add_argument("--assumed-cost-pct", type=float)
    parser.add_argument("--actual-cost-pct", type=float)
    parser.add_argument("--outcomes-dir", default=str(RUNTIME_DIR / "forward_shadow_outcomes"))
    parser.add_argument("--ledger", help="Append-once outcome JSONL path.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without writing outcomes.")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        raw_plans = load_plan_records(args.plans)
        plans = [normalize_plan(item) for item in raw_plans]
        if args.plan_id:
            wanted = set(args.plan_id)
            plans = [item for item in plans if item["plan_id"] in wanted]
            missing = wanted - {item["plan_id"] for item in plans}
            if missing:
                raise EvaluationError(f"requested plan_id not found: {sorted(missing)}")
        if not plans:
            raise EvaluationError("no plans selected")

        shared_document = None
        shared_source: dict[str, Any] | None = None
        if args.candles_file:
            if args.candles_file == "-":
                shared_document = json.load(sys.stdin)
                shared_source = {"kind": "offline_stdin", "requested_after_trade": True}
            else:
                candle_path = Path(args.candles_file)
                if not candle_path.is_file():
                    raise EvaluationError(f"candle artifact does not exist: {candle_path}")
                shared_document = _read_json_or_csv(candle_path)
                shared_source = {
                    "kind": "offline_file",
                    "path": str(candle_path.resolve()),
                    "input_sha256": sha256_file(candle_path),
                    "requested_after_trade": True,
                }
            if isinstance(shared_document, list) and len(plans) > 1:
                raise EvaluationError("a flat candle list can evaluate only one plan")

        summaries = []
        for plan in plans:
            if args.candles_dir:
                candle_path = _resolve_candle_file(Path(args.candles_dir), plan)
                document = _read_json_or_csv(candle_path)
                source_info = {
                    "kind": "offline_directory_file",
                    "path": str(candle_path.resolve()),
                    "input_sha256": sha256_file(candle_path),
                    "requested_after_trade": True,
                }
            elif args.candles_url_template:
                document, source_info = _load_https_session(
                    args.candles_url_template,
                    plan,
                    _finite_number(args.network_timeout, "network_timeout", positive=True),
                    args.bearer_token_env,
                )
            else:
                document = shared_document
                source_info = dict(shared_source or {})
            session = _select_session(document, plan)
            candles = normalize_candles(session, plan["trade_date"])
            outcome = evaluate_plan(
                plan,
                candles,
                source_info,
                assumed_cost_override=args.assumed_cost_pct,
                actual_cost_override=args.actual_cost_pct,
            )
            path = None
            created = False
            if not args.dry_run:
                path, created = write_immutable_outcome(
                    outcome, args.outcomes_dir, ledger_path=args.ledger
                )
            summaries.append(
                {
                    "plan_id": outcome["plan_id"],
                    "outcome_id": outcome["outcome_id"],
                    "trade_date": outcome["trade_date"],
                    "symbol": outcome["symbol"],
                    "control_net_return_pct": outcome["control"]["net_return_pct"],
                    "challenger_net_return_pct": outcome["challenger"]["net_return_pct"],
                    "challenger_minus_control_pp": outcome["comparison"]["challenger_minus_control_pp"],
                    "path": str(path) if path is not None else None,
                    "created": created,
                    "dry_run": bool(args.dry_run),
                }
            )
        print(
            json.dumps(
                {"evaluated": len(summaries), "outcomes": summaries},
                ensure_ascii=False,
                sort_keys=True,
                indent=2 if args.pretty else None,
                allow_nan=False,
            )
        )
        return 0
    except (EvaluationError, OSError, json.JSONDecodeError) as exc:
        print(f"evaluation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
