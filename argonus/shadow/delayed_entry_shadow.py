#!/usr/bin/env python3
"""Pure registry for the isolated 07:05 delayed-entry shadow.

There are no broker/account/order imports in this module.  A daily plan is an
atomic reservation written *before* the separate capture command makes its
single read-only GetOrderBook request.  Re-running a reserved date never makes
another request.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from argonus.shadow import forward_shadow_research as exit_registry


SCHEMA_VERSION = 1
POLICY = "delayed_entry_0705_depth50_vwap"
PLAN_RECORD_TYPE = "delayed_entry_shadow_plan"
CAPTURE_RECORD_TYPE = "delayed_entry_shadow_capture"
CAPTURE_START = "07:05:00"
CAPTURE_END = "07:05:15"
BOOK_DEPTH = 50
MAX_QUOTE_AGE_MS = 3_000
STOP_PCT = 1.0
TIME_EXIT = "18:35"
STRESS_BPS = (0, 5, 10)
MOSCOW = ZoneInfo("Europe/Moscow")
from argonus.paths import CONFIG_DIR, PROJECT_ROOT

DEFAULT_MANIFEST_PATH = CONFIG_DIR / "delayed_entry_shadow_manifest.json"
ARTIFACT_PATHS = {
    "argonus/paths.py": "argonus/paths.py",
    "capture_delayed_entry_0705.py": "argonus/shadow/capture_delayed_entry_0705.py",
    "delayed_entry_shadow.py": "argonus/shadow/delayed_entry_shadow.py",
    "evaluate_delayed_entry_shadow.py": "argonus/shadow/evaluate_delayed_entry_shadow.py",
    "forward_shadow_research.py": "argonus/shadow/forward_shadow_research.py",
    "run_delayed_entry_shadow.sh": "scripts/run_delayed_entry_shadow.sh"
}

EXPECTED_ARTIFACTS = {
    "argonus/paths.py",
    "delayed_entry_shadow.py",
    "capture_delayed_entry_0705.py",
    "evaluate_delayed_entry_shadow.py",
    "run_delayed_entry_shadow.sh",
    "forward_shadow_research.py",
}


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    body = dict(manifest)
    body.pop("manifest_sha256", None)
    body.pop("manifest_file_sha256", None)
    return canonical_sha256(body)


def _finite(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if result <= 0 or str(value).strip() not in {str(result), f"{result}.0"}:
        raise ValueError(f"{label} must be a positive integer")
    return result


def _timestamp(value: datetime | str, label: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be timezone-aware ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset")
    return parsed.astimezone(MOSCOW)


def _timestamp_text(value: datetime | str, label: str) -> str:
    return _timestamp(value, label).isoformat(timespec="milliseconds")


def _window_bounds(trade_date: str) -> tuple[datetime, datetime]:
    day = date.fromisoformat(str(trade_date))
    start = datetime.fromisoformat(f"{day.isoformat()}T{CAPTURE_START}").replace(tzinfo=MOSCOW)
    end = datetime.fromisoformat(f"{day.isoformat()}T{CAPTURE_END}").replace(tzinfo=MOSCOW)
    return start, end


def _in_capture_window(value: datetime, trade_date: str) -> bool:
    start, end = _window_bounds(trade_date)
    return start <= value <= end


def require_capture_window(value: datetime | str, trade_date: str) -> str:
    parsed = _timestamp(value, "capture timestamp")
    if not _in_capture_window(parsed, trade_date):
        raise ValueError("timestamp is outside 07:05:00-07:05:15 Europe/Moscow")
    return parsed.isoformat(timespec="milliseconds")


def _next_complete_five_minute(value: datetime) -> str:
    floor = value.replace(minute=value.minute - value.minute % 5, second=0, microsecond=0)
    return (floor + timedelta(minutes=5)).strftime("%H:%M")


def _validate_manifest(payload: Mapping[str, Any], *, require_hash: bool) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RuntimeError("delayed-entry manifest must be a JSON object")
    snapshot = json.loads(canonical_json(dict(payload)))
    attached = snapshot.pop("manifest_sha256", None)
    snapshot.pop("manifest_file_sha256", None)
    calculated = canonical_sha256(snapshot)
    if require_hash and attached is None:
        raise RuntimeError("supplied manifest requires manifest_sha256")
    if attached is not None and attached != calculated:
        raise RuntimeError("manifest_sha256 is inconsistent with its canonical body")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("unsupported delayed-entry manifest schema")
    if snapshot.get("artifact_type") != "argonus_delayed_entry_shadow_manifest":
        raise RuntimeError("unexpected delayed-entry manifest artifact_type")
    if snapshot.get("release") != "delayed-entry-shadow-2026-07-20-v1":
        raise RuntimeError("unexpected delayed-entry manifest release")
    if snapshot.get("mode") != "shadow_only" or snapshot.get("production_activation_allowed") is not False:
        raise RuntimeError("delayed-entry manifest must be shadow-only")
    if snapshot.get("registered_at") != "2026-07-18" or snapshot.get("evidence_not_before") != "2026-07-20":
        raise RuntimeError("delayed-entry evidence window is not frozen")
    expected_policy = {
        "name": POLICY,
        "capture_start": CAPTURE_START,
        "capture_end": CAPTURE_END,
        "book_depth": BOOK_DEPTH,
        "max_quote_age_ms": MAX_QUOTE_AGE_MS,
        "long_side": "asks",
        "short_side": "bids",
        "size_basis": "actual_control_filled_lots",
        "entry_price": "depth_weighted_vwap",
        "frozen_absolute_target": True,
        "stop_pct": STOP_PCT,
        "time_exit": TIME_EXIT,
        "capture_bar": "exclude_capture_containing_5m_bar; start_at_next_complete_5m_bar",
        "stress_bps": list(STRESS_BPS),
    }
    if snapshot.get("policy") != expected_policy:
        raise RuntimeError("delayed-entry policy mismatch")
    expected_control = {
        "source": "same_day_protected_exit_shadow_plan",
        "entry_price": "actual_broker_fill",
        "target": "same_frozen_absolute_target",
        "stop_pct": STOP_PCT,
        "time_exit": TIME_EXIT,
    }
    if snapshot.get("control") != expected_control:
        raise RuntimeError("delayed-entry control mismatch")
    expected_gates = {
        "minimum_eligible_captures": 50,
        "capture_window": True,
        "quote_freshness": True,
        "depth_coverage": True,
        "target_ahead": True,
        "control_fill_before_quote": True,
        "execution_claim": False,
        "candle_open_fallback_allowed": False,
        "positive_delta_both_chronological_halves": True,
        "mdd_nonworse": True,
        "max_positive_contributor_share_pct": 35.0,
        "paired_sign_flip_p_max": 0.1,
        "positive_delta_required_at_stress_bps": 10,
    }
    if snapshot.get("promotion_gates") != expected_gates:
        raise RuntimeError("delayed-entry promotion gates mismatch")
    expected_audit = {
        "plan_publication": "atomic first-writer-wins before the only market-data request",
        "capture_publication": "atomic first-writer-wins per trade date",
        "broker_method_allowlist": [
            "tinkoff.public.invest.api.contract.v1.MarketDataService/GetOrderBook"
        ],
        "order_methods_allowed": False,
        "bot_lock_allowed": False,
    }
    if snapshot.get("audit") != expected_audit:
        raise RuntimeError("delayed-entry audit contract mismatch")
    expected_source = {
        "status": (
            "in_sample_07:05_open_discovery_is_diagnostic_only; "
            "promotion_requires_new_point_in_time_orderbook_evidence"
        )
    }
    if snapshot.get("source_research") != expected_source:
        raise RuntimeError("delayed-entry source-research disclosure mismatch")
    if snapshot.get("warning") != (
        "Forward evidence only. This manifest cannot place, replace, cancel, or "
        "protect an order and cannot activate production."
    ):
        raise RuntimeError("delayed-entry warning mismatch")
    artifacts = snapshot.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != EXPECTED_ARTIFACTS:
        raise RuntimeError("delayed-entry artifact set mismatch")
    root = PROJECT_ROOT
    for name, expected in artifacts.items():
        path = root / ARTIFACT_PATHS[name]
        if not path.is_file():
            raise RuntimeError(f"registered artifact is missing: {name}")
        if expected == "PENDING" or sha256_file(path) != expected:
            raise RuntimeError(f"registered artifact hash mismatch: {name}")
    snapshot["manifest_sha256"] = calculated
    return snapshot


def load_manifest(path: str | os.PathLike[str] = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    artifact = Path(path)
    try:
        raw = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load delayed-entry manifest: {exc}") from exc
    result = _validate_manifest(raw, require_hash=False)
    result["manifest_file_sha256"] = sha256_file(artifact)
    return result


def _manifest_or_default(manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    return load_manifest() if manifest is None else _validate_manifest(manifest, require_hash=True)


def build_plan(
    exit_plan: Mapping[str, Any],
    trade_state: Mapping[str, Any],
    *,
    created_at: datetime | str,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze one protected actual control before any quote request."""
    manifest_value = _manifest_or_default(manifest)
    frozen_exit = exit_registry.validate_exit_shadow_plan(exit_plan)
    if not isinstance(trade_state, Mapping):
        raise ValueError("trade_state must be a mapping")
    state = json.loads(canonical_json(dict(trade_state)))
    trade_date = str(frozen_exit["trade_date"])
    if date.fromisoformat(trade_date) < date.fromisoformat(str(manifest_value["evidence_not_before"])):
        raise ValueError("trade date precedes the delayed-entry evidence window")
    created = _timestamp(created_at, "created_at")
    if not _in_capture_window(created, trade_date):
        raise ValueError("plan reservation must occur inside 07:05:00-07:05:15 Europe/Moscow")
    evidence = frozen_exit.get("optional_evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("exit plan has no protection evidence")
    if evidence.get("actual_fill_authoritative") is not True or not evidence.get("stop_order_id"):
        raise ValueError("exit plan lacks authoritative fill or confirmed STOP")
    if evidence.get("protection_status") not in {"stop_placed", "protected"}:
        raise ValueError("exit plan was not captured after STOP protection")
    if evidence.get("target_guard_failed") is not False:
        raise ValueError("frozen target guard failed")
    control_entry = _finite(frozen_exit.get("entry_price"), "control entry_price")
    target = _finite(frozen_exit.get("target_price"), "target_price")
    fill_time = _timestamp(frozen_exit.get("actual_entry_timestamp"), "actual fill timestamp")
    if fill_time >= created:
        raise ValueError("actual control fill must precede the 07:05 quote plan")
    direction = str(frozen_exit["direction"])
    if (direction == "long" and target <= control_entry) or (direction == "short" and target >= control_entry):
        raise ValueError("frozen target is not ahead of the actual control fill")
    lots = _positive_int(evidence.get("filled_lots"), "filled_lots")
    lot_size = _positive_int(evidence.get("lot_size"), "lot_size")
    if state.get("engine") != "A" or str(state.get("date")) != trade_date:
        raise ValueError("trade_state is not same-day Engine A")
    if state.get("phase") != "entered":
        raise ValueError("trade_state must still be in entered phase at 07:05")
    if state.get("protection_status") not in {"stop_placed", "protected"}:
        raise ValueError("trade_state does not confirm STOP protection")
    if state.get("target_guard_failed") is not False:
        raise ValueError("trade_state target guard failed")
    if str(state.get("symbol") or "").upper() != str(frozen_exit["symbol"]).upper():
        raise ValueError("trade_state symbol does not match exit plan")
    uid = str(state.get("uid") or "").strip()
    if not uid:
        raise ValueError("trade_state instrument uid is required")
    if _positive_int(state.get("lots"), "trade_state lots") != lots:
        raise ValueError("trade_state lots do not match protected fill")
    if _positive_int(state.get("lot_size"), "trade_state lot_size") != lot_size:
        raise ValueError("trade_state lot_size does not match exit evidence")
    if not math.isclose(_finite(state.get("entry_price"), "trade_state entry_price"), control_entry, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("trade_state entry price does not match exit plan")
    state_target = state.get("planned_target_price", state.get("target_price"))
    if not math.isclose(_finite(state_target, "trade_state target"), target, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("trade_state target does not match exit plan")
    if state.get("stop_order_id") != evidence.get("stop_order_id"):
        raise ValueError("trade_state STOP does not match exit evidence")
    selector_shadow_id = str(evidence.get("selector_shadow_decision_id") or "")
    if len(selector_shadow_id) != 64 or any(
        character not in "0123456789abcdef" for character in selector_shadow_id
    ):
        raise ValueError("exit plan lacks the frozen same-day selector control identity")
    if state.get("selector_decision_id") != evidence.get("selector_decision_id"):
        raise ValueError("trade_state selector identity does not match exit evidence")
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": PLAN_RECORD_TYPE,
        "capture_mode": "forward",
        "policy": POLICY,
        "shadow_only": True,
        "production_effect": "none",
        "created_at": created.isoformat(timespec="milliseconds"),
        "trade_date": trade_date,
        "symbol": str(frozen_exit["symbol"]).upper(),
        "uid": uid,
        "direction": direction,
        "target_price": target,
        "assumed_round_trip_cost_pct": _finite(frozen_exit["assumed_round_trip_cost_pct"], "cost"),
        "control": {
            "actual_fill_price": control_entry,
            "actual_fill_timestamp": fill_time.isoformat(timespec="milliseconds"),
            "actual_filled_lots": lots,
            "lot_size": lot_size,
        },
        "challenger": {
            "capture_start": CAPTURE_START,
            "capture_end": CAPTURE_END,
            "book_depth": BOOK_DEPTH,
            "max_quote_age_ms": MAX_QUOTE_AGE_MS,
            "requested_lots": lots,
            "side": "asks" if direction == "long" else "bids",
        },
        "source_exit_plan_id": frozen_exit["plan_id"],
        "source_exit_plan_sha256": exit_registry.canonical_sha256(frozen_exit),
        "source_exit_manifest_sha256": frozen_exit["manifest_sha256"],
        "source_state_sha256": canonical_sha256(state),
        "manifest_sha256": manifest_value["manifest_sha256"],
    }
    record["plan_id"] = canonical_sha256(record)
    return record


def validate_plan(
    plan: Mapping[str, Any], *, manifest: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise ValueError("delayed-entry plan must be a mapping")
    snapshot = json.loads(canonical_json(dict(plan)))
    manifest_value = _manifest_or_default(manifest)
    plan_id = str(snapshot.pop("plan_id", ""))
    if len(plan_id) != 64 or plan_id != canonical_sha256(snapshot):
        raise ValueError("delayed-entry plan_id is invalid")
    snapshot["plan_id"] = plan_id
    required = {
        "schema_version", "record_type", "capture_mode", "policy", "shadow_only",
        "production_effect", "created_at", "trade_date", "symbol", "uid", "direction",
        "target_price", "assumed_round_trip_cost_pct", "control", "challenger",
        "source_exit_plan_id", "source_exit_plan_sha256", "source_exit_manifest_sha256",
        "source_state_sha256", "manifest_sha256", "plan_id",
    }
    if set(snapshot) != required:
        raise ValueError(f"delayed-entry plan field set mismatch: {sorted(set(snapshot) ^ required)}")
    if snapshot["schema_version"] != SCHEMA_VERSION or snapshot["record_type"] != PLAN_RECORD_TYPE:
        raise ValueError("delayed-entry plan schema/type mismatch")
    if snapshot["capture_mode"] != "forward" or snapshot["policy"] != POLICY:
        raise ValueError("delayed-entry plan is not registered forward evidence")
    if snapshot["shadow_only"] is not True or snapshot["production_effect"] != "none":
        raise ValueError("delayed-entry plan must have zero production effect")
    if snapshot["manifest_sha256"] != manifest_value["manifest_sha256"]:
        raise ValueError("delayed-entry plan uses the wrong manifest")
    trade_date = date.fromisoformat(str(snapshot["trade_date"])).isoformat()
    created = _timestamp(snapshot["created_at"], "created_at")
    if not _in_capture_window(created, trade_date):
        raise ValueError("delayed-entry plan was not reserved in the capture window")
    if not str(snapshot["symbol"]).strip() or not str(snapshot["uid"]).strip():
        raise ValueError("delayed-entry plan symbol/uid is missing")
    direction = str(snapshot["direction"])
    if direction not in {"long", "short"}:
        raise ValueError("delayed-entry direction must be long or short")
    target = _finite(snapshot["target_price"], "target_price")
    cost = _finite(snapshot["assumed_round_trip_cost_pct"], "cost")
    if target <= 0.0 or not 0.0 <= cost <= 20.0:
        raise ValueError("delayed-entry target/cost is invalid")
    control = snapshot["control"]
    if not isinstance(control, Mapping) or set(control) != {
        "actual_fill_price", "actual_fill_timestamp", "actual_filled_lots", "lot_size"
    }:
        raise ValueError("delayed-entry control schema mismatch")
    fill = _finite(control["actual_fill_price"], "control actual_fill_price")
    fill_time = _timestamp(control["actual_fill_timestamp"], "actual_fill_timestamp")
    _positive_int(control["actual_filled_lots"], "actual_filled_lots")
    _positive_int(control["lot_size"], "lot_size")
    if fill <= 0.0 or fill_time >= created:
        raise ValueError("actual control fill is not before plan reservation")
    if (direction == "long" and target <= fill) or (direction == "short" and target >= fill):
        raise ValueError("target is not ahead of the actual control fill")
    expected_challenger = {
        "capture_start": CAPTURE_START,
        "capture_end": CAPTURE_END,
        "book_depth": BOOK_DEPTH,
        "max_quote_age_ms": MAX_QUOTE_AGE_MS,
        "requested_lots": int(control["actual_filled_lots"]),
        "side": "asks" if direction == "long" else "bids",
    }
    if snapshot["challenger"] != expected_challenger:
        raise ValueError("delayed-entry challenger plan mismatch")
    for name in ("source_exit_plan_id", "source_exit_plan_sha256", "source_exit_manifest_sha256", "source_state_sha256"):
        value = str(snapshot[name])
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError(f"{name} is not a lowercase SHA-256 digest")
    return snapshot


def _quotation(value: Any, label: str) -> float:
    if isinstance(value, Mapping):
        try:
            result = float(value.get("units", 0)) + float(value.get("nano", 0)) / 1_000_000_000
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} quotation is invalid") from exc
    else:
        result = _finite(value, label)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{label} must be positive")
    return result


def _normalize_levels(raw: Any, label: str, *, descending: bool) -> list[dict[str, Any]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError(f"order book {label} must be a list")
    levels: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"{label}[{index}] must be an object")
        price = _quotation(item.get("price"), f"{label}[{index}].price")
        quantity = _positive_int(item.get("quantity"), f"{label}[{index}].quantity")
        levels.append({"price": price, "quantity": quantity})
    prices = [float(row["price"]) for row in levels]
    expected = sorted(prices, reverse=descending)
    if prices != expected or len(prices) != len(set(prices)):
        raise ValueError(f"order book {label} is not strictly price ordered")
    return levels


def normalize_orderbook(raw: Mapping[str, Any], expected_uid: str, expected_symbol: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("GetOrderBook response must be an object")
    uid = str(raw.get("instrumentUid", raw.get("instrument_uid", "")) or "").strip()
    ticker = str(raw.get("ticker", "") or "").strip().upper()
    if not uid or uid != expected_uid:
        raise ValueError("order book instrument uid mismatch")
    if ticker and ticker != expected_symbol.upper():
        raise ValueError("order book ticker mismatch")
    depth = _positive_int(raw.get("depth"), "order book depth")
    if depth != BOOK_DEPTH:
        raise ValueError(f"order book depth must be exactly {BOOK_DEPTH}")
    primary_timestamps = [
        raw[key] for key in ("orderbookTs", "orderbook_ts") if raw.get(key)
    ]
    if not primary_timestamps:
        raise ValueError("GetOrderBook response lacks mandatory orderbookTs")
    timestamp_values = primary_timestamps + ([raw["time"]] if raw.get("time") else [])
    parsed_times = [_timestamp(value, "orderbookTs") for value in timestamp_values]
    if any(value != parsed_times[0] for value in parsed_times[1:]):
        raise ValueError("conflicting order book timestamps")
    bids = _normalize_levels(raw.get("bids"), "bids", descending=True)
    asks = _normalize_levels(raw.get("asks"), "asks", descending=False)
    if not bids or not asks:
        raise ValueError("order book must contain both bids and asks")
    if float(bids[0]["price"]) >= float(asks[0]["price"]):
        raise ValueError("order book is crossed or locked")
    return {
        "depth": depth,
        "instrument_uid": uid,
        "ticker": ticker or expected_symbol.upper(),
        "orderbook_ts": parsed_times[0].isoformat(timespec="milliseconds"),
        "bids": bids,
        "asks": asks,
    }


def depth_vwap(levels: Sequence[Mapping[str, Any]], requested_lots: int) -> dict[str, Any]:
    requested = _positive_int(requested_lots, "requested_lots")
    remaining = requested
    notional = 0.0
    covered = 0
    worst: float | None = None
    for item in levels:
        if remaining <= 0:
            break
        quantity = _positive_int(item.get("quantity"), "book level quantity")
        price = _quotation(item.get("price"), "book level price")
        used = min(quantity, remaining)
        notional += used * price
        covered += used
        remaining -= used
        worst = price
    sufficient = covered == requested
    return {
        "requested_lots": requested,
        "covered_lots": covered,
        "depth_sufficient": sufficient,
        "executable_vwap": notional / requested if sufficient else None,
        "worst_price": worst,
    }


def build_capture(
    plan: Mapping[str, Any],
    orderbook: Mapping[str, Any],
    *,
    request_started_at: datetime | str,
    response_received_at: datetime | str,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize one and only one point-in-time read-only order-book response."""
    manifest_value = _manifest_or_default(manifest)
    frozen = validate_plan(plan, manifest=manifest_value)
    trade_date = str(frozen["trade_date"])
    requested_at = _timestamp(request_started_at, "request_started_at")
    received_at = _timestamp(response_received_at, "response_received_at")
    plan_created = _timestamp(frozen["created_at"], "plan.created_at")
    if requested_at < plan_created or received_at < requested_at:
        raise ValueError("quote request timing precedes its immutable plan")
    normalized_book = normalize_orderbook(orderbook, str(frozen["uid"]), str(frozen["symbol"]))
    book_time = _timestamp(normalized_book["orderbook_ts"], "orderbook_ts")
    quote_age_ms = (received_at - book_time).total_seconds() * 1000.0
    capture_window = all(
        _in_capture_window(item, trade_date)
        for item in (plan_created, requested_at, received_at, book_time)
    )
    freshness = 0.0 <= quote_age_ms <= MAX_QUOTE_AGE_MS
    side = str(frozen["challenger"]["side"])
    levels = normalized_book[side]
    fill = depth_vwap(levels, int(frozen["control"]["actual_filled_lots"]))
    best_touch = float(levels[0]["price"]) if levels else None
    worst = fill["worst_price"]
    direction = str(frozen["direction"])
    target = float(frozen["target_price"])
    target_ahead = bool(
        fill["depth_sufficient"]
        and worst is not None
        and ((direction == "long" and target > float(worst)) or (direction == "short" and target < float(worst)))
    )
    fill_before = _timestamp(
        frozen["control"]["actual_fill_timestamp"], "actual_fill_timestamp"
    ) < book_time
    gates = {
        "capture_window": capture_window,
        "quote_freshness": freshness,
        "depth_coverage": bool(fill["depth_sufficient"]),
        "target_ahead": target_ahead,
        "control_fill_before_quote": fill_before,
    }
    challenger = {
        "entry_price_source": "executable_orderbook_vwap",
        "executable_vwap": fill["executable_vwap"],
        "requested_lots": fill["requested_lots"],
        "covered_lots": fill["covered_lots"],
        "depth_sufficient": fill["depth_sufficient"],
        "side": side,
        "book_depth": BOOK_DEPTH,
        "best_touch": best_touch,
        "worst_price": worst,
        "orderbook_ts": book_time.isoformat(timespec="milliseconds"),
        "request_started_at": requested_at.isoformat(timespec="milliseconds"),
        "response_received_at": received_at.isoformat(timespec="milliseconds"),
        "captured_at": received_at.isoformat(timespec="milliseconds"),
        "quote_age_ms": quote_age_ms,
        "freshness_pass": freshness,
        "target_ahead": target_ahead,
        "evaluation_start_time": _next_complete_five_minute(book_time),
    }
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": CAPTURE_RECORD_TYPE,
        "capture_mode": "forward",
        "policy": POLICY,
        "shadow_only": True,
        "production_effect": "none",
        "execution_claim": False,
        "created_at": received_at.isoformat(timespec="milliseconds"),
        "trade_date": trade_date,
        "symbol": frozen["symbol"],
        "uid": frozen["uid"],
        "direction": direction,
        "plan_id": frozen["plan_id"],
        "source_exit_plan_id": frozen["source_exit_plan_id"],
        "source_exit_plan_sha256": frozen["source_exit_plan_sha256"],
        "manifest_sha256": manifest_value["manifest_sha256"],
        "target_price": target,
        "assumed_round_trip_cost_pct": frozen["assumed_round_trip_cost_pct"],
        "control": dict(frozen["control"]),
        "challenger": challenger,
        "gates": gates,
        "promotion_eligible": all(gates.values()),
        "orderbook": normalized_book,
        "book_sha256": canonical_sha256(normalized_book),
    }
    record["capture_id"] = canonical_sha256(record)
    return record


def validate_capture(
    capture: Mapping[str, Any], *, manifest: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Reject any mutation of quote inputs, VWAP, timing gates, or identity."""
    if not isinstance(capture, Mapping):
        raise ValueError("delayed-entry capture must be a mapping")
    snapshot = json.loads(canonical_json(dict(capture)))
    manifest_value = _manifest_or_default(manifest)
    capture_id = str(snapshot.pop("capture_id", ""))
    if len(capture_id) != 64 or capture_id != canonical_sha256(snapshot):
        raise ValueError("delayed-entry capture_id is invalid")
    snapshot["capture_id"] = capture_id
    required = {
        "schema_version", "record_type", "capture_mode", "policy", "shadow_only",
        "production_effect", "execution_claim", "created_at", "trade_date", "symbol",
        "uid", "direction", "plan_id", "source_exit_plan_id", "source_exit_plan_sha256",
        "manifest_sha256", "target_price", "assumed_round_trip_cost_pct", "control",
        "challenger", "gates", "promotion_eligible", "orderbook", "book_sha256", "capture_id",
    }
    if set(snapshot) != required:
        raise ValueError(f"delayed-entry capture field set mismatch: {sorted(set(snapshot) ^ required)}")
    if snapshot["schema_version"] != SCHEMA_VERSION or snapshot["record_type"] != CAPTURE_RECORD_TYPE:
        raise ValueError("delayed-entry capture schema/type mismatch")
    if snapshot["capture_mode"] != "forward" or snapshot["policy"] != POLICY:
        raise ValueError("capture is not registered forward evidence")
    if snapshot["shadow_only"] is not True or snapshot["production_effect"] != "none":
        raise ValueError("capture must have zero production effect")
    if snapshot["execution_claim"] is not False:
        raise ValueError("an order-book snapshot cannot claim execution")
    if snapshot["manifest_sha256"] != manifest_value["manifest_sha256"]:
        raise ValueError("capture uses the wrong manifest")
    for name in ("plan_id", "source_exit_plan_id", "source_exit_plan_sha256"):
        value = str(snapshot[name])
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError(f"capture {name} is not a lowercase SHA-256 digest")
    normalized_book = normalize_orderbook(
        snapshot["orderbook"], str(snapshot["uid"]), str(snapshot["symbol"])
    )
    if canonical_json(normalized_book) != canonical_json(snapshot["orderbook"]):
        raise ValueError("capture order book is not canonical")
    if snapshot["book_sha256"] != canonical_sha256(normalized_book):
        raise ValueError("capture book_sha256 is invalid")
    direction = str(snapshot["direction"])
    if direction not in {"long", "short"}:
        raise ValueError("capture direction is invalid")
    control = snapshot["control"]
    if not isinstance(control, Mapping) or set(control) != {
        "actual_fill_price", "actual_fill_timestamp", "actual_filled_lots", "lot_size"
    }:
        raise ValueError("capture control schema mismatch")
    requested_lots = _positive_int(control["actual_filled_lots"], "actual_filled_lots")
    _positive_int(control["lot_size"], "lot_size")
    control_fill_price = _finite(control["actual_fill_price"], "actual_fill_price")
    control_fill_time = _timestamp(control["actual_fill_timestamp"], "actual_fill_timestamp")
    target = _finite(snapshot["target_price"], "target_price")
    if (direction == "long" and target <= control_fill_price) or (
        direction == "short" and target >= control_fill_price
    ):
        raise ValueError("capture target is not ahead of control fill")
    challenger = snapshot["challenger"]
    expected_fields = {
        "entry_price_source", "executable_vwap", "requested_lots", "covered_lots",
        "depth_sufficient", "side", "book_depth", "best_touch", "worst_price",
        "orderbook_ts", "request_started_at", "response_received_at", "captured_at",
        "quote_age_ms", "freshness_pass", "target_ahead", "evaluation_start_time",
    }
    if not isinstance(challenger, Mapping) or set(challenger) != expected_fields:
        raise ValueError("capture challenger schema mismatch")
    side = "asks" if direction == "long" else "bids"
    if challenger["entry_price_source"] != "executable_orderbook_vwap" or challenger["side"] != side:
        raise ValueError("capture entry source/side mismatch")
    if challenger["book_depth"] != BOOK_DEPTH or challenger["requested_lots"] != requested_lots:
        raise ValueError("capture book depth/requested lots mismatch")
    recomputed = depth_vwap(normalized_book[side], requested_lots)
    for field in ("covered_lots", "depth_sufficient", "executable_vwap", "worst_price"):
        if challenger[field] != recomputed[field]:
            raise ValueError(f"capture {field} does not match order-book depth")
    best_touch = float(normalized_book[side][0]["price"]) if normalized_book[side] else None
    if challenger["best_touch"] != best_touch:
        raise ValueError("capture best_touch mismatch")
    trade_date = date.fromisoformat(str(snapshot["trade_date"])).isoformat()
    created = _timestamp(snapshot["created_at"], "created_at")
    book_time = _timestamp(challenger["orderbook_ts"], "orderbook_ts")
    if book_time != _timestamp(normalized_book["orderbook_ts"], "book.orderbook_ts"):
        raise ValueError("capture orderbook timestamps conflict")
    requested_at = _timestamp(challenger["request_started_at"], "request_started_at")
    received_at = _timestamp(challenger["response_received_at"], "response_received_at")
    captured_at = _timestamp(challenger["captured_at"], "captured_at")
    if created != received_at or captured_at != received_at or received_at < requested_at:
        raise ValueError("capture request/response timing mismatch")
    quote_age_ms = (received_at - book_time).total_seconds() * 1000.0
    if not math.isclose(_finite(challenger["quote_age_ms"], "quote_age_ms"), quote_age_ms, rel_tol=0.0, abs_tol=1e-6):
        raise ValueError("capture quote age mismatch")
    freshness = 0.0 <= quote_age_ms <= MAX_QUOTE_AGE_MS
    capture_window = all(_in_capture_window(item, trade_date) for item in (requested_at, received_at, book_time))
    worst = recomputed["worst_price"]
    target_ahead = bool(
        recomputed["depth_sufficient"] and worst is not None
        and ((direction == "long" and target > float(worst)) or (direction == "short" and target < float(worst)))
    )
    expected_gates = {
        "capture_window": capture_window,
        "quote_freshness": freshness,
        "depth_coverage": bool(recomputed["depth_sufficient"]),
        "target_ahead": target_ahead,
        "control_fill_before_quote": control_fill_time < book_time,
    }
    if snapshot["gates"] != expected_gates:
        raise ValueError("capture gate values are inconsistent")
    if challenger["freshness_pass"] is not freshness or challenger["target_ahead"] is not target_ahead:
        raise ValueError("capture challenger gate aliases are inconsistent")
    if challenger["evaluation_start_time"] != _next_complete_five_minute(book_time):
        raise ValueError("capture evaluation_start_time mismatch")
    if snapshot["promotion_eligible"] is not all(expected_gates.values()):
        raise ValueError("capture promotion_eligible is inconsistent")
    return snapshot


def write_first_writer_wins(
    record: Mapping[str, Any],
    directory: str | os.PathLike[str],
    *,
    current_date: date | None = None,
    manifest: Mapping[str, Any] | None = None,
) -> tuple[Path, bool]:
    """Publish by atomic no-overwrite hard link; existing dates never retry."""
    manifest_value = _manifest_or_default(manifest)
    record_type = str(record.get("record_type") or "")
    if record_type == PLAN_RECORD_TYPE:
        frozen = validate_plan(record, manifest=manifest_value)
        identity_field = "plan_id"
    elif record_type == CAPTURE_RECORD_TYPE:
        frozen = validate_capture(record, manifest=manifest_value)
        identity_field = "capture_id"
    else:
        raise ValueError("unknown delayed-entry record_type")
    trade_date = date.fromisoformat(str(frozen["trade_date"]))
    today = current_date or datetime.now(MOSCOW).date()
    if trade_date != today:
        raise ValueError("first-writer evidence requires same Moscow trade date")
    output = Path(directory) / POLICY / record_type
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"{trade_date.isoformat()}.json"
    payload = json.dumps(
        frozen, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        dir=output,
        prefix=f".{trade_date.isoformat()}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # link(2) is an atomic no-overwrite publication: exactly one
            # contender can create destination and readers never see partial JSON.
            os.link(temporary, destination)
            created = True
        except FileExistsError:
            created = False
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    if not created:
        try:
            existing = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"existing immutable evidence is unreadable: {destination}") from exc
        if existing.get(identity_field) != frozen.get(identity_field):
            raise RuntimeError(f"immutable delayed-entry identity conflict: {destination}")
        return destination, False
    directory_fd = os.open(output, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return destination, True


__all__ = [
    "BOOK_DEPTH", "CAPTURE_END", "CAPTURE_RECORD_TYPE", "CAPTURE_START",
    "MAX_QUOTE_AGE_MS", "PLAN_RECORD_TYPE", "POLICY", "STOP_PCT", "STRESS_BPS",
    "TIME_EXIT", "build_capture", "build_plan", "canonical_sha256", "depth_vwap",
    "load_manifest", "normalize_orderbook", "require_capture_window", "validate_capture", "validate_plan",
    "write_first_writer_wins",
]
