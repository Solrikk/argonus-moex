#!/usr/bin/env python3
"""Pure, broker-free forward shadow research primitives for Argonus.

This module pre-registers two *shadow-only* challengers:

* ``sl1.25_t1.25_x1700`` exit policy;
* ``max_vol_expansion_top6`` selector policy.

It accepts already available candidate/candle rows, produces deterministic
decision records, and can publish first-writer-wins JSON audits.  It has no
broker, order, account, network, or production-bot imports and cannot mutate a
live selection.  Optional execution evidence is merely copied into an audit;
callers must collect it after the live entry is stop-protected or in a
standalone command, never synchronously before a live order.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
EXIT_POLICY = "sl1.25_t1.25_x1700"
SELECTOR_POLICY = "max_vol_expansion_top6"
from argonus.paths import CONFIG_DIR, PROJECT_ROOT

DEFAULT_MANIFEST_PATH = CONFIG_DIR / "forward_shadow_manifest.json"
DEFAULT_EVALUATOR_PATH = Path(__file__).with_name("evaluate_forward_shadows.py")
MOSCOW = ZoneInfo("Europe/Moscow")

MANIFEST_RELEASE = "forward-shadow-2026-07-20-v1"
MANIFEST_REGISTERED_AT = "2026-07-18"
EVIDENCE_NOT_BEFORE = "2026-07-20"

CONTROL_EXIT = {
    "stop_pct": 1.0,
    "target_multiple": 1.0,
    "time_exit": "18:35",
}
CHALLENGER_EXIT = {
    "stop_pct": 1.25,
    "target_multiple": 1.25,
    "time_exit": "17:00",
}
SELECTOR_TOP_K = 6
SELECTOR_RS5_FLOOR_PP = -2.39

EXIT_MIN_NEW_TRADES = 50
SELECTOR_MIN_ELIGIBLE_SESSIONS = 40
SELECTOR_MIN_DIVERGENCES = 10
MAX_POSITIVE_CONTRIBUTOR_SHARE_PCT = 35.0
PAIRED_SIGN_FLIP_P_MAX = 0.10
PAIRED_SIGN_FLIP_SAMPLES = 100_000
PAIRED_SIGN_FLIP_SEED = 20_260_718

SELECTOR_EXECUTION_EVIDENCE_FIELDS = (
    "captured_at_or_before_decision",
    "point_in_time",
    "control_executable",
    "challenger_executable",
    "spread_depth_captured",
    "max_lots_captured",
)

T = TypeVar("T")


def canonical_json(value: Any) -> bytes:
    """Return the single canonical encoding used by every hash/identity."""
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


def _require_sha256(value: Any, label: str) -> str:
    text = str(value or "")
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")
    return text


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _finite(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _iso_date(value: Any) -> str:
    if isinstance(value, datetime):
        result = value.date()
    elif isinstance(value, date):
        result = value
    else:
        result = date.fromisoformat(str(value)[:10])
    return result.isoformat()


def _clock(value: Any, label: str) -> str:
    text = str(value)
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError(f"{label} must use HH:MM")
    hour, minute = map(int, parts)
    normalized = f"{hour:02d}:{minute:02d}"
    if text != normalized or not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid {label}: {text!r}")
    return normalized


def _created_at(value: str | None) -> str:
    if value is None:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("timestamp must be timezone-aware ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return parsed.isoformat(timespec="seconds")


def _actual_entry_boundary(
    value: datetime | str,
    trade_date_text: str,
) -> tuple[str, str, str, str]:
    """Normalize a fill timestamp and freeze the next complete 5-minute bar."""
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("actual_entry_timestamp must be ISO-8601") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("actual_entry_timestamp must include a timezone offset")
    local = stamp.astimezone(MOSCOW)
    if local.date().isoformat() != trade_date_text:
        raise ValueError("actual_entry_timestamp must fall on trade_date in Europe/Moscow")
    entry_bar = local.replace(
        minute=(local.minute // 5) * 5,
        second=0,
        microsecond=0,
    )
    evaluation_start = entry_bar + timedelta(minutes=5)
    if evaluation_start.date() != local.date():
        raise ValueError("actual entry is too late to start a same-day 5-minute evaluation")
    return (
        local.isoformat(timespec="seconds"),
        local.strftime("%H:%M:%S"),
        entry_bar.strftime("%H:%M"),
        evaluation_start.strftime("%H:%M"),
    )


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Hash the registered manifest body, excluding runtime hash fields."""
    body = dict(manifest)
    body.pop("manifest_sha256", None)
    body.pop("manifest_file_sha256", None)
    return canonical_sha256(body)


def _validate_manifest_payload(
    payload: Mapping[str, Any],
    *,
    module_path: str | os.PathLike[str] = __file__,
    require_attached_hash: bool,
) -> dict[str, Any]:
    """Validate both file-loaded and caller-supplied manifest objects.

    Caller-supplied dictionaries are not a trust shortcut: they must carry the
    canonical body hash returned by :func:`load_manifest`, and every frozen
    policy, gate, and artifact hash is checked again.
    """
    if not isinstance(payload, Mapping):
        raise RuntimeError("shadow manifest must be a JSON object")
    try:
        snapshot = json.loads(canonical_json(dict(payload)))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"shadow manifest is not canonical JSON: {exc}") from exc
    supplied_hash = snapshot.pop("manifest_sha256", None)
    snapshot.pop("manifest_file_sha256", None)
    canonical_hash = canonical_sha256(snapshot)
    if require_attached_hash and supplied_hash is None:
        raise RuntimeError("supplied shadow manifest requires manifest_sha256")
    if supplied_hash is not None and supplied_hash != canonical_hash:
        raise RuntimeError(
            "shadow manifest_sha256 is inconsistent with its canonical body: "
            f"expected {canonical_hash}, got {supplied_hash}"
        )
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("unsupported shadow manifest schema")
    if snapshot.get("artifact_type") != "argonus_forward_shadow_research_manifest":
        raise RuntimeError("unexpected shadow manifest artifact_type")
    if snapshot.get("mode") != "shadow_only":
        raise RuntimeError("manifest is not shadow_only")
    if snapshot.get("production_activation_allowed") is not False:
        raise RuntimeError("shadow manifest must forbid production activation")
    frozen_metadata = {
        "release": MANIFEST_RELEASE,
        "registered_at": MANIFEST_REGISTERED_AT,
        "evidence_not_before": EVIDENCE_NOT_BEFORE,
    }
    for field, expected in frozen_metadata.items():
        if snapshot.get(field) != expected:
            raise RuntimeError(
                f"manifest {field} mismatch: expected {expected!r}, "
                f"got {snapshot.get(field)!r}"
            )

    policies = snapshot.get("policies")
    if not isinstance(policies, Mapping):
        raise RuntimeError("manifest policies are missing")
    expected_exit = {"name": EXIT_POLICY, **CHALLENGER_EXIT}
    actual_exit = policies.get("exit")
    if actual_exit != expected_exit:
        raise RuntimeError(f"exit policy mismatch: expected {expected_exit}, got {actual_exit}")
    expected_selector = {
        "name": SELECTOR_POLICY,
        "feature": "vol_expansion",
        "top_k": SELECTOR_TOP_K,
        "directional_rs5_floor_pp": SELECTOR_RS5_FLOOR_PP,
        "order": "maximum_finite_feature_then_existing_rank",
        "preserve_baseline_day_set": True,
    }
    actual_selector = policies.get("selector")
    if actual_selector != expected_selector:
        raise RuntimeError(
            f"selector policy mismatch: expected {expected_selector}, got {actual_selector}"
        )

    gates = snapshot.get("promotion_gates")
    expected_gates = {
        "exit_min_new_trades": EXIT_MIN_NEW_TRADES,
        "selector_min_eligible_sessions": SELECTOR_MIN_ELIGIBLE_SESSIONS,
        "selector_min_divergences": SELECTOR_MIN_DIVERGENCES,
        "positive_delta_both_chronological_halves": True,
        "mdd_nonworse": True,
        "max_positive_contributor_share_pct": MAX_POSITIVE_CONTRIBUTOR_SHARE_PCT,
        "paired_sign_flip_p_max": PAIRED_SIGN_FLIP_P_MAX,
        "paired_sign_flip_samples": PAIRED_SIGN_FLIP_SAMPLES,
        "paired_sign_flip_seed": PAIRED_SIGN_FLIP_SEED,
    }
    if gates != expected_gates:
        raise RuntimeError(f"promotion gates mismatch: expected {expected_gates}, got {gates}")

    expected_control = {
        "selector": (
            "actual Engine-A+RS5 selected symbol, never legacy rank1 unless they are identical"
        ),
        "exit": dict(CONTROL_EXIT),
    }
    if snapshot.get("control") != expected_control:
        raise RuntimeError("manifest control definition mismatch")
    expected_audit = {
        "identity": "sha256 canonical JSON; UUID and wall-clock timestamps excluded",
        "publication": "atomic first-writer-wins per policy/record_type/trade_date",
        "capture_mode": "forward",
        "optional_execution_evidence": (
            "collect only after protected live entry or in standalone command"
        ),
    }
    if snapshot.get("audit") != expected_audit:
        raise RuntimeError("manifest audit definition mismatch")

    expected_source_research = {
        "data/backtests/FLAT_MONTH_EXIT_RISK_RESEARCH_2026-07-18.md": (
            "5f3a5ce1b1c03d0afdf9dd3c9b082f4985d61be28aed14bcf5104beb1f2d4bea"
        ),
        "data/backtests/FLAT_MONTH_SELECTOR_WALKFORWARD_2026-07-18.md": (
            "ec97b795fb08546afbe15e17d2fcc6948be3c5211f6518c2afbb7d0791487304"
        ),
    }
    if snapshot.get("source_research") != expected_source_research:
        raise RuntimeError("manifest source_research mismatch")

    artifacts = snapshot.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise RuntimeError("manifest artifacts are missing")
    if set(artifacts) != {"forward_shadow_research.py", "evaluate_forward_shadows.py", "argonus/paths.py"}:
        raise RuntimeError("manifest must pin exactly the research module and evaluator")
    expected_module_hash = artifacts.get("forward_shadow_research.py")
    actual_module_hash = sha256_file(module_path)
    if expected_module_hash != actual_module_hash:
        raise RuntimeError(
            "shadow module hash mismatch: "
            f"expected {expected_module_hash}, got {actual_module_hash}"
        )
    expected_evaluator_hash = artifacts.get("evaluate_forward_shadows.py")
    actual_evaluator_hash = sha256_file(DEFAULT_EVALUATOR_PATH)
    if expected_evaluator_hash != actual_evaluator_hash:
        raise RuntimeError(
            "shadow evaluator hash mismatch: "
            f"expected {expected_evaluator_hash}, got {actual_evaluator_hash}"
        )
    if artifacts["argonus/paths.py"] != sha256_file(PROJECT_ROOT / "argonus/paths.py"):
        raise RuntimeError("shadow project paths hash mismatch")
    snapshot["manifest_sha256"] = canonical_hash
    return snapshot


def load_manifest(
    path: str | os.PathLike[str] = DEFAULT_MANIFEST_PATH,
    *,
    module_path: str | os.PathLike[str] = __file__,
) -> dict[str, Any]:
    """Load and strictly validate the pre-registered shadow manifest."""
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"shadow manifest is missing: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid shadow manifest JSON: {exc}") from exc
    result = _validate_manifest_payload(
        payload,
        module_path=module_path,
        require_attached_hash=False,
    )
    result["manifest_file_sha256"] = sha256_file(manifest_path)
    return result


def _manifest_or_default(manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    if manifest is None:
        return load_manifest()
    return _validate_manifest_payload(
        manifest,
        module_path=__file__,
        require_attached_hash=True,
    )


def _validate_evidence_date(trade_date_text: str, manifest: Mapping[str, Any]) -> None:
    not_before = date.fromisoformat(str(manifest["evidence_not_before"]))
    value = date.fromisoformat(trade_date_text)
    if value < not_before:
        raise ValueError(
            f"trade_date {value.isoformat()} precedes registered evidence window "
            f"{not_before.isoformat()}"
        )


def attach_decision_id(record: Mapping[str, Any], identity_fields: Mapping[str, Any]) -> dict[str, Any]:
    """Attach a deterministic identity; timestamps are intentionally excluded."""
    result = dict(record)
    basis = {
        "schema_version": SCHEMA_VERSION,
        "record_type": result.get("record_type"),
        "policy": result.get("policy"),
        "manifest_sha256": result.get("manifest_sha256"),
        "identity": dict(identity_fields),
    }
    result["decision_id"] = canonical_sha256(basis)
    result["decision_id_basis"] = "sha256(canonical-json(schema,record_type,policy,manifest,identity))"
    return result


def build_exit_shadow_plan(
    *,
    trade_date: date | str,
    symbol: str,
    direction: str,
    target_price: float,
    entry_time: str | None = None,
    entry_price: float | None = None,
    actual_entry_timestamp: datetime | str | None = None,
    assumed_round_trip_cost_pct: float = 0.08,
    created_at: str | None = None,
    manifest: Mapping[str, Any] | None = None,
    optional_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the immutable, evaluator-compatible exit shadow plan.

    When supplied, ``entry_price`` is the actual frozen live fill and requires
    an explicit timezone-aware ``actual_entry_timestamp``.  The fill-containing
    five-minute bar is excluded and both paths start at the next complete bar.
    When the price is absent, a standalone evaluator falls back to the observed
    first candle at/after ``entry_time`` (07:00 by default).
    """
    manifest_value = _manifest_or_default(manifest)
    trade_date_text = _iso_date(trade_date)
    _validate_evidence_date(trade_date_text, manifest_value)
    ticker = str(symbol).strip().upper()
    if not ticker:
        raise ValueError("symbol is empty")
    if direction not in {"long", "short"}:
        raise ValueError("direction must be long or short")
    target = _finite(target_price, "target_price")
    if target <= 0.0:
        raise ValueError("target_price must be positive")
    actual_stamp: str | None = None
    actual_time: str | None = None
    entry_bar_time: str | None = None
    entry_bar_excluded = False
    if entry_price is not None:
        reference_entry = _finite(entry_price, "entry_price")
        if reference_entry <= 0.0:
            raise ValueError("entry_price must be positive")
        if actual_entry_timestamp is None:
            raise ValueError(
                "entry_price requires explicit actual_entry_timestamp; "
                "the fill-containing bar must be excluded"
            )
        actual_stamp, actual_time, entry_bar_time, evaluation_start_time = (
            _actual_entry_boundary(actual_entry_timestamp, trade_date_text)
        )
        if evaluation_start_time > str(CHALLENGER_EXIT["time_exit"]):
            raise ValueError(
                "actual fill is too late for the registered challenger window"
            )
        derived_entry_clock = actual_time[:5]
        if entry_time is not None and _clock(entry_time, "entry_time") != derived_entry_clock:
            raise ValueError("entry_time conflicts with actual_entry_timestamp")
        clock = derived_entry_clock
        entry_bar_excluded = True
        if direction == "long" and target <= reference_entry:
            raise ValueError("long target must be above entry_price")
        if direction == "short" and target >= reference_entry:
            raise ValueError("short target must be below entry_price")
    else:
        reference_entry = None
        if actual_entry_timestamp is not None:
            raise ValueError("actual_entry_timestamp requires entry_price")
        clock = _clock(entry_time or "07:00", "entry_time")
        evaluation_start_time = clock
    cost = _finite(assumed_round_trip_cost_pct, "assumed_round_trip_cost_pct")
    if cost < 0.0:
        raise ValueError("assumed cost cannot be negative")
    manifest_hash = str(manifest_value.get("manifest_sha256") or manifest_sha256(manifest_value))
    evidence_snapshot = dict(optional_evidence or {})
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "exit_shadow_plan",
        "capture_mode": "forward",
        "policy": EXIT_POLICY,
        "shadow_only": True,
        "production_effect": "none",
        "created_at": _created_at(created_at),
        "trade_date": trade_date_text,
        "symbol": ticker,
        "direction": direction,
        "entry_time": clock,
        "entry_price": reference_entry,
        "actual_entry_timestamp": actual_stamp,
        "actual_entry_time": actual_time,
        "entry_bar_time": entry_bar_time,
        "entry_bar_excluded": entry_bar_excluded,
        "evaluation_start_time": evaluation_start_time,
        "evaluation_start_rule": (
            "next_complete_5m_bar_after_actual_fill"
            if reference_entry is not None
            else "first_candle_at_or_after_entry_time_fallback"
        ),
        "target_price": target,
        "control": dict(CONTROL_EXIT),
        "challenger": dict(CHALLENGER_EXIT),
        "target_formula": (
            "entry + direction_sign * target_multiple * abs(frozen_target-entry)"
        ),
        "assumed_round_trip_cost_pct": cost,
        "manifest_sha256": manifest_hash,
        "optional_evidence": evidence_snapshot,
        "optional_evidence_sha256": canonical_sha256(evidence_snapshot),
    }
    identity = {
        key: record[key]
        for key in (
            "trade_date",
            "symbol",
            "direction",
            "entry_time",
            "entry_price",
            "actual_entry_timestamp",
            "actual_entry_time",
            "entry_bar_time",
            "entry_bar_excluded",
            "evaluation_start_time",
            "target_price",
            "control",
            "challenger",
            "assumed_round_trip_cost_pct",
            "optional_evidence_sha256",
        )
    }
    result = attach_decision_id(record, identity)
    result["plan_id"] = result["decision_id"]
    return result


def validate_exit_shadow_plan(
    plan: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebuild and byte-compare an immutable registered exit decision."""
    if not isinstance(plan, Mapping):
        raise ValueError("exit shadow plan must be a mapping")
    try:
        snapshot = json.loads(canonical_json(dict(plan)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"exit shadow plan is not canonical JSON: {exc}") from exc
    required = {
        "created_at",
        "trade_date",
        "symbol",
        "direction",
        "entry_time",
        "entry_price",
        "actual_entry_timestamp",
        "target_price",
        "assumed_round_trip_cost_pct",
        "optional_evidence",
        "decision_id",
        "plan_id",
    }
    missing = sorted(required - set(snapshot))
    if missing:
        raise ValueError(f"exit shadow plan is missing frozen fields: {missing}")
    manifest_value = _manifest_or_default(manifest)
    expected = build_exit_shadow_plan(
        trade_date=snapshot["trade_date"],
        symbol=snapshot["symbol"],
        direction=snapshot["direction"],
        entry_time=snapshot["entry_time"],
        entry_price=snapshot["entry_price"],
        actual_entry_timestamp=snapshot["actual_entry_timestamp"],
        target_price=snapshot["target_price"],
        assumed_round_trip_cost_pct=snapshot["assumed_round_trip_cost_pct"],
        created_at=snapshot["created_at"],
        manifest=manifest_value,
        optional_evidence=snapshot["optional_evidence"],
    )
    if canonical_json(snapshot) != canonical_json(expected):
        differing = sorted(
            key
            for key in set(snapshot) | set(expected)
            if snapshot.get(key) != expected.get(key)
        )
        raise ValueError(
            "exit shadow plan does not match its registered decision: "
            f"{differing}"
        )
    return snapshot


def _candidate_row(
    item: Any,
    rank: int,
    reason_override: Any,
    eligibility_evidence_present: bool,
) -> dict[str, Any]:
    if eligibility_evidence_present:
        reason = reason_override
    else:
        reason = "missing explicit eligibility evidence"
    raw_vol = _value(item, "vol_expansion")
    vol: float | None
    try:
        vol = float(raw_vol)
        if not math.isfinite(vol):
            vol = None
    except (TypeError, ValueError):
        vol = None
    raw_rs5 = _value(item, "directional_rs5_pp")
    try:
        rs5 = float(raw_rs5)
        if not math.isfinite(rs5):
            rs5 = None
    except (TypeError, ValueError):
        rs5 = None
    exit_target = _value(item, "exit_target")
    target_label = _value(item, "target_label")
    target_price = _value(item, "target_price")
    target_move = _value(item, "target_move_pct")
    if exit_target is not None:
        target_label = _value(exit_target, "label", target_label)
        target_price = _value(exit_target, "price", target_price)
        target_move = _value(exit_target, "move_pct_from_close", target_move)
    skipped_reason = _value(item, "skipped_reason")
    if reason is None and skipped_reason:
        reason = f"analysis: {skipped_reason}"
    passes_gates = reason is None
    return {
        "rank": rank,
        "symbol": str(_value(item, "symbol", "")).strip().upper(),
        "direction": str(_value(item, "direction", "unknown")),
        "vol_expansion": vol,
        "overall_score": _value(item, "overall_score"),
        "directional_rs5_pp": rs5,
        "passes_directional_rs5_floor": bool(
            rs5 is not None and rs5 >= SELECTOR_RS5_FLOOR_PP
        ),
        "target_label": target_label,
        "target_price": target_price,
        "target_move_pct": target_move,
        "regime": _value(item, "regime"),
        "market_bullish": _value(item, "market_bullish"),
        "short_rally_10d_pct": _value(item, "short_rally_10d_pct"),
        "runner_day_prob": _value(item, "runner_day_prob"),
        "skipped_reason": skipped_reason,
        "eligibility_reason": reason,
        "eligibility_evidence_present": eligibility_evidence_present,
        "passes_existing_gates": passes_gates,
    }


def evaluate_max_vol_expansion_top6(
    candidates: Sequence[Any],
    *,
    trade_date: date | str,
    control_selected_symbol: str,
    eligibility_reasons: Mapping[int, str | None] | None = None,
    legacy_rank1_symbol: str | None = None,
    created_at: str | None = None,
    manifest: Mapping[str, Any] | None = None,
    optional_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the pre-registered selector using caller-supplied rows only.

    ``control_selected_symbol`` is the actual Engine-A+RS5 choice and is the
    only comparison baseline.  ``legacy_rank1_symbol`` is provenance, never a
    performance control.  The actual control is mandatory and every supplied
    candidate rank needs an explicit ``eligibility_reasons`` entry (``None``
    means it passed).  The caller sequence and its candidate objects are never
    sorted or mutated.
    """
    manifest_value = _manifest_or_default(manifest)
    control_symbol = str(control_selected_symbol).strip().upper()
    if not control_symbol:
        raise ValueError("control_selected_symbol is required and cannot be empty")
    reasons = eligibility_reasons or {}
    rows = [
        _candidate_row(item, rank, reasons.get(rank), rank in reasons)
        for rank, item in enumerate(list(candidates)[:SELECTOR_TOP_K], start=1)
    ]
    trade_date_text = _iso_date(trade_date)
    _validate_evidence_date(trade_date_text, manifest_value)
    manifest_hash = str(manifest_value.get("manifest_sha256") or manifest_sha256(manifest_value))
    legacy_symbol = (
        str(legacy_rank1_symbol).strip().upper()
        if legacy_rank1_symbol is not None
        else (rows[0]["symbol"] if rows else None)
    )
    baseline = next((row for row in rows if row["symbol"] == control_symbol), None)
    decision = "no_candidates_fail_open"
    reason = "candidate list is empty"
    recommended = baseline
    eligible_session = False

    if rows and baseline is None:
        decision = "control_not_in_top6_fail_open"
        reason = "actual Engine-A+RS5 control symbol is absent from supplied top-6"
        recommended = None
    elif baseline is not None:
        baseline_gates_ok = bool(baseline["passes_existing_gates"])
        baseline_rs5_ok = bool(baseline["passes_directional_rs5_floor"])
        eligible_session = bool(baseline_gates_ok and baseline_rs5_ok)
        if not baseline_gates_ok:
            decision = "baseline_ineligible_fail_open"
            reason = str(baseline["eligibility_reason"])
        elif not baseline_rs5_ok:
            decision = "control_rs5_evidence_fail_open"
            reason = (
                "actual control lacks finite directional_rs5_pp at or above "
                f"{SELECTOR_RS5_FLOOR_PP:.2f}"
            )
            recommended = baseline
        else:
            eligible = [
                row
                for row in rows
                if row["passes_existing_gates"]
                and row["passes_directional_rs5_floor"]
                and row["vol_expansion"] is not None
            ]
            if baseline["vol_expansion"] is None:
                decision = "missing_baseline_feature_fail_open"
                reason = "baseline vol_expansion is missing or non-finite"
            elif not eligible:
                decision = "no_finite_feature_fail_open"
                reason = "no eligible top-6 row has finite vol_expansion"
            else:
                recommended = max(
                    eligible,
                    key=lambda row: (float(row["vol_expansion"]), -int(row["rank"])),
                )
                if recommended["rank"] == baseline["rank"]:
                    decision = "keep"
                    reason = "baseline has maximum finite vol_expansion"
                else:
                    decision = "shadow_divergence"
                    reason = "challenger has maximum finite vol_expansion"

    divergence = bool(
        baseline is not None
        and recommended is not None
        and recommended["rank"] != baseline["rank"]
    )
    evidence_snapshot = dict(optional_evidence or {})
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "selector_shadow_decision",
        "capture_mode": "forward",
        "policy": SELECTOR_POLICY,
        "shadow_only": True,
        "production_effect": "none",
        "created_at": _created_at(created_at),
        "trade_date": trade_date_text,
        "feature": "vol_expansion",
        "top_k": SELECTOR_TOP_K,
        "directional_rs5_floor_pp": SELECTOR_RS5_FLOOR_PP,
        "eligible_session": eligible_session,
        "decision": decision,
        "decision_reason": reason,
        "legacy_rank1_symbol": legacy_symbol,
        "control_selected_rank": baseline["rank"] if baseline else None,
        "control_selected_symbol": baseline["symbol"] if baseline else control_symbol,
        # Compatibility aliases are explicit about pointing at actual control.
        "baseline_rank": baseline["rank"] if baseline else None,
        "baseline_symbol": baseline["symbol"] if baseline else control_symbol,
        "recommended_rank": recommended["rank"] if recommended else None,
        "recommended_symbol": recommended["symbol"] if recommended else None,
        "divergence": divergence,
        "live_selected_rank": baseline["rank"] if baseline else None,
        "live_selected_symbol": baseline["symbol"] if baseline else None,
        "candidates": rows,
        "manifest_sha256": manifest_hash,
        "optional_evidence": evidence_snapshot,
        "optional_evidence_sha256": canonical_sha256(evidence_snapshot),
        "evidence_collection_constraint": (
            "optional broker/order-book evidence only after protected live entry or standalone"
        ),
    }
    identity = {
        key: record[key]
        for key in (
            "trade_date",
            "feature",
            "top_k",
            "directional_rs5_floor_pp",
            "legacy_rank1_symbol",
            "control_selected_rank",
            "control_selected_symbol",
            "baseline_rank",
            "baseline_symbol",
            "recommended_rank",
            "recommended_symbol",
            "candidates",
            "optional_evidence_sha256",
        )
    }
    return attach_decision_id(record, identity)


def validate_selector_shadow_decision(
    decision: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-run the selector and reject any mutation of inputs, result, or ID."""
    if not isinstance(decision, Mapping):
        raise ValueError("selector shadow decision must be a mapping")
    try:
        snapshot = json.loads(canonical_json(dict(decision)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"selector decision is not canonical JSON: {exc}") from exc
    candidates = snapshot.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("selector decision candidates must be a list")
    if len(candidates) > SELECTOR_TOP_K:
        raise ValueError("selector decision exceeds registered top_k")
    reasons: dict[int, str | None] = {}
    for expected_rank, row in enumerate(candidates, start=1):
        if not isinstance(row, Mapping) or row.get("rank") != expected_rank:
            raise ValueError("selector candidate ranks must be consecutive from 1")
        if row.get("eligibility_evidence_present") is not True:
            raise ValueError("selector candidate lacks explicit eligibility evidence")
        reason = row.get("eligibility_reason")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("selector eligibility_reason must be string or null")
        reasons[expected_rank] = reason
    required = {
        "created_at",
        "trade_date",
        "control_selected_symbol",
        "legacy_rank1_symbol",
        "optional_evidence",
        "decision_id",
    }
    missing = sorted(required - set(snapshot))
    if missing:
        raise ValueError(f"selector decision is missing frozen fields: {missing}")
    manifest_value = _manifest_or_default(manifest)
    expected = evaluate_max_vol_expansion_top6(
        candidates,
        trade_date=snapshot["trade_date"],
        control_selected_symbol=snapshot["control_selected_symbol"],
        eligibility_reasons=reasons,
        legacy_rank1_symbol=snapshot["legacy_rank1_symbol"],
        created_at=snapshot["created_at"],
        manifest=manifest_value,
        optional_evidence=snapshot["optional_evidence"],
    )
    if canonical_json(snapshot) != canonical_json(expected):
        differing = sorted(
            key
            for key in set(snapshot) | set(expected)
            if snapshot.get(key) != expected.get(key)
        )
        raise ValueError(
            "selector shadow decision does not match registered evaluation: "
            f"{differing}"
        )
    return snapshot


def fail_open(
    baseline: T,
    operation: Callable[..., Any],
    /,
    *args: Any,
    **kwargs: Any,
) -> tuple[Any, dict[str, Any]]:
    """Run shadow research and return a pre-operation canonical snapshot.

    This helper deliberately cannot return the challenger as its first tuple
    element.  The baseline must be canonical-JSON compatible; snapshotting it
    before the operation also prevents operation-side mutation from changing
    the value returned to the caller.
    """
    try:
        baseline_snapshot = json.loads(canonical_json(baseline))
    except (TypeError, ValueError) as exc:
        raise TypeError("fail_open baseline must be canonical-JSON compatible") from exc
    try:
        report = operation(*args, **kwargs)
        if not isinstance(report, Mapping):
            raise TypeError("shadow operation did not return a mapping")
        audit = dict(report)
        audit["fail_open_applied"] = False
        return baseline_snapshot, audit
    except Exception as exc:  # noqa: BLE001 - shadow failure must never affect live path
        return baseline_snapshot, {
            "schema_version": SCHEMA_VERSION,
            "record_type": "shadow_fail_open",
            "shadow_only": True,
            "production_effect": "none",
            "fail_open_applied": True,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _normalize_candle(item: Any) -> dict[str, float | str]:
    if isinstance(item, Mapping):
        raw = [item.get(name) for name in ("time", "open", "high", "low", "close")]
    elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) >= 5:
        raw = list(item[:5])
    else:
        raw = [
            _value(item, "time"),
            _value(item, "open"),
            _value(item, "high"),
            _value(item, "low"),
            _value(item, "close"),
        ]
    clock = _clock(raw[0], "candle time")
    opening, high, low, close = (
        _finite(raw[1], "open"),
        _finite(raw[2], "high"),
        _finite(raw[3], "low"),
        _finite(raw[4], "close"),
    )
    if min(opening, high, low, close) <= 0.0 or high < max(opening, low, close) or low > min(opening, high, close):
        raise ValueError(f"invalid OHLC candle at {clock}")
    return {"time": clock, "open": opening, "high": high, "low": low, "close": close}


def simulate_exit_policy(
    *,
    direction: str,
    entry_price: float,
    frozen_target_price: float,
    candles: Sequence[Any],
    stop_pct: float,
    target_multiple: float,
    time_exit: str,
    round_trip_cost_pct: float = 0.08,
) -> dict[str, Any]:
    """Pure stop-first 5-minute simulation for an already frozen trade."""
    if direction not in {"long", "short"}:
        raise ValueError("direction must be long or short")
    entry = _finite(entry_price, "entry_price")
    frozen_target = _finite(frozen_target_price, "frozen_target_price")
    stop_distance = _finite(stop_pct, "stop_pct")
    target_factor = _finite(target_multiple, "target_multiple")
    cost = _finite(round_trip_cost_pct, "round_trip_cost_pct")
    end = _clock(time_exit, "time_exit")
    if min(entry, frozen_target, stop_distance, target_factor) <= 0.0 or cost < 0.0:
        raise ValueError("prices/multipliers must be positive and cost non-negative")
    sign = 1.0 if direction == "long" else -1.0
    base_distance = sign * (frozen_target - entry)
    if base_distance <= 0.0:
        raise ValueError("frozen target is on the wrong side of entry")
    target = entry + sign * target_factor * base_distance
    stop = entry * (1.0 - sign * stop_distance / 100.0)
    normalized = [_normalize_candle(item) for item in candles]
    window = [row for row in normalized if row["time"] <= end]
    if not window:
        raise ValueError("no candles at or before time_exit")
    clocks = [str(row["time"]) for row in window]
    if clocks != sorted(clocks) or len(clocks) != len(set(clocks)):
        raise ValueError("candles must be sorted and unique")

    exit_price = float(window[-1]["close"])
    exit_time = str(window[-1]["time"])
    reason = "time_exit"
    ambiguous = False
    for row in window:
        if direction == "long":
            hit_stop = float(row["low"]) <= stop
            hit_target = float(row["high"]) >= target
        else:
            hit_stop = float(row["high"]) >= stop
            hit_target = float(row["low"]) <= target
        if hit_stop:
            ambiguous = hit_target
            exit_price = stop
            exit_time = str(row["time"])
            reason = "both_stop_first" if ambiguous else "stop"
            break
        if hit_target:
            exit_price = target
            exit_time = str(row["time"])
            reason = "target"
            break
    gross = sign * (exit_price / entry - 1.0) * 100.0
    return {
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "exit_price": exit_price,
        "exit_time": exit_time,
        "exit_reason": reason,
        "ambiguous": ambiguous,
        "gross_return_pct": gross,
        "round_trip_cost_pct": cost,
        "net_return_pct": gross - cost,
    }


def evaluate_exit_shadow(
    plan: Mapping[str, Any],
    candles: Sequence[Any],
    *,
    candle_source: Any = "caller_supplied",
    actual_round_trip_cost_pct: float | None = None,
    evaluated_at: str | None = None,
    evaluator_sha256: str | None = None,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate both exits with the one canonical, registered OHLC engine."""
    manifest_value = _manifest_or_default(manifest)
    frozen_plan = validate_exit_shadow_plan(plan, manifest=manifest_value)
    expected_evaluator_hash = str(
        manifest_value["artifacts"]["evaluate_forward_shadows.py"]
    )
    evaluator_hash = evaluator_sha256 or sha256_file(DEFAULT_EVALUATOR_PATH)
    if evaluator_hash != expected_evaluator_hash:
        raise ValueError(
            "evaluator_sha256 does not match registered evaluator: "
            f"{evaluator_hash} != {expected_evaluator_hash}"
        )
    normalized = [_normalize_candle(item) for item in candles]
    entry_clock = _clock(frozen_plan["entry_time"], "entry_time")
    evaluation_start = _clock(
        frozen_plan["evaluation_start_time"],
        "evaluation_start_time",
    )
    plan_entry = frozen_plan["entry_price"]
    if plan_entry is not None:
        # validate_exit_shadow_plan already derived and byte-compared the fill
        # boundary.  Keeping the branch explicit documents the causal window.
        if frozen_plan["entry_bar_excluded"] is not True:
            raise ValueError("actual fill plan must exclude its containing bar")
    entry_rows = [row for row in normalized if str(row["time"]) >= evaluation_start]
    if not entry_rows:
        raise ValueError("no candle at or after evaluation_start_time")
    candle_entry = float(entry_rows[0]["open"])
    if plan_entry is None:
        entry = candle_entry
        entry_source = "first_candle_open_fallback"
    else:
        entry = _finite(plan_entry, "plan entry_price")
        if entry <= 0.0:
            raise ValueError("plan entry_price must be positive")
        entry_source = "actual_frozen_fill"
    assumed_cost = _finite(
        frozen_plan["assumed_round_trip_cost_pct"], "assumed cost"
    )
    control = simulate_exit_policy(
        direction=str(frozen_plan["direction"]),
        entry_price=entry,
        frozen_target_price=float(frozen_plan["target_price"]),
        candles=entry_rows,
        stop_pct=float(CONTROL_EXIT["stop_pct"]),
        target_multiple=float(CONTROL_EXIT["target_multiple"]),
        time_exit=str(CONTROL_EXIT["time_exit"]),
        round_trip_cost_pct=assumed_cost,
    )
    challenger = simulate_exit_policy(
        direction=str(frozen_plan["direction"]),
        entry_price=entry,
        frozen_target_price=float(frozen_plan["target_price"]),
        candles=entry_rows,
        stop_pct=float(CHALLENGER_EXIT["stop_pct"]),
        target_multiple=float(CHALLENGER_EXIT["target_multiple"]),
        time_exit=str(CHALLENGER_EXIT["time_exit"]),
        round_trip_cost_pct=assumed_cost,
    )
    actual_cost = None
    if actual_round_trip_cost_pct is not None:
        actual_cost = _finite(actual_round_trip_cost_pct, "actual cost")
        if actual_cost < 0.0 or actual_cost > 20.0:
            raise ValueError("actual cost must be between 0% and 20%")
    for result in (control, challenger):
        result["net_assumed_return_pct"] = (
            float(result["gross_return_pct"]) - assumed_cost
        )
        if actual_cost is not None:
            result["net_actual_return_pct"] = (
                float(result["gross_return_pct"]) - actual_cost
            )
            result["net_return_pct"] = result["net_actual_return_pct"]
            result["cost_basis"] = "actual_provided"
        else:
            result["net_return_pct"] = result["net_assumed_return_pct"]
            result["cost_basis"] = "assumed"
    try:
        source_snapshot = json.loads(canonical_json(candle_source))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"candle_source is not canonical JSON: {exc}") from exc
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "exit_shadow_outcome",
        "capture_mode": frozen_plan["capture_mode"],
        "policy": EXIT_POLICY,
        "shadow_only": True,
        "production_effect": "none",
        "execution_claim": False,
        "evaluation_mode": "post_trade_completed_5m",
        "evaluated_at": _created_at(evaluated_at),
        "trade_date": str(frozen_plan["trade_date"]),
        "symbol": str(frozen_plan["symbol"]),
        "direction": str(frozen_plan["direction"]),
        "plan_id": str(frozen_plan["plan_id"]),
        "decision_id": str(frozen_plan["decision_id"]),
        "manifest_sha256": str(frozen_plan["manifest_sha256"]),
        "plan_sha256": canonical_sha256(frozen_plan),
        "candles_sha256": canonical_sha256(normalized),
        "candle_source": source_snapshot,
        "candle_count": len(normalized),
        "evaluator_sha256": evaluator_hash,
        "entry_price": entry,
        "entry_price_source": entry_source,
        "actual_entry_timestamp": frozen_plan["actual_entry_timestamp"],
        "actual_entry_time": frozen_plan["actual_entry_time"],
        "entry_bar_time": frozen_plan["entry_bar_time"],
        "entry_bar_excluded": bool(frozen_plan["entry_bar_excluded"]),
        "evaluation_start_time": evaluation_start,
        "first_candle_open": candle_entry,
        "first_evaluation_candle_open": candle_entry,
        "entry_price_delta_vs_first_candle": entry - candle_entry,
        "control": control,
        "challenger": challenger,
        "actual_round_trip_cost_pct": actual_cost,
        "costs": {
            "assumed_round_trip_pct": assumed_cost,
            "actual_round_trip_pct": actual_cost,
            "used_basis": "actual_provided" if actual_cost is not None else "assumed",
        },
        "comparison": {
            "challenger_minus_control_pp": (
                challenger["net_return_pct"] - control["net_return_pct"]
            )
        },
    }
    identity = {
        "schema_version": SCHEMA_VERSION,
        "record_type": record["record_type"],
        "policy": EXIT_POLICY,
        "manifest_sha256": record["manifest_sha256"],
        "trade_date": record["trade_date"],
        "symbol": record["symbol"],
        "direction": record["direction"],
        "plan_id": record["plan_id"],
        "decision_id": record["decision_id"],
        "plan_sha256": record["plan_sha256"],
        "candles_sha256": record["candles_sha256"],
        "evaluator_sha256": evaluator_hash,
        "evaluation_start_time": evaluation_start,
        "entry_price": record["entry_price"],
        "entry_price_source": record["entry_price_source"],
        "entry_bar_excluded": record["entry_bar_excluded"],
        "actual_round_trip_cost_pct": actual_cost,
        "control": control,
        "challenger": challenger,
    }
    record["outcome_identity"] = identity
    record["outcome_id"] = canonical_sha256(identity)
    record["provenance"] = {
        "decision_id": record["decision_id"],
        "plan_id": record["plan_id"],
        "plan_sha256": record["plan_sha256"],
        "candles_sha256": record["candles_sha256"],
        "manifest_sha256": record["manifest_sha256"],
        "evaluator_sha256": evaluator_hash,
    }
    return record


def _selector_execution_evidence(
    decision: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Validate evidence committed before the selector decision was published."""
    optional = decision.get("optional_evidence")
    bundle = optional.get("selector_execution_evidence") if isinstance(optional, Mapping) else None
    reasons: list[str] = []
    if not isinstance(bundle, Mapping):
        return False, {
            "valid": False,
            "reasons": ["missing_selector_execution_evidence"],
        }
    for field in SELECTOR_EXECUTION_EVIDENCE_FIELDS:
        if bundle.get(field) is not True:
            reasons.append(f"{field}_not_true")
    try:
        created = datetime.fromisoformat(str(decision["created_at"]))
        captured = datetime.fromisoformat(str(bundle["captured_at"]))
        if created.tzinfo is None or captured.tzinfo is None:
            raise ValueError("timestamps require timezone")
        if captured > created:
            reasons.append("captured_after_decision")
    except (KeyError, TypeError, ValueError):
        reasons.append("invalid_captured_at")
    expected_symbols = {
        "control": decision.get("control_selected_symbol"),
        "challenger": decision.get("recommended_symbol"),
    }
    normalized_legs: dict[str, Any] = {}
    for name, expected_symbol in expected_symbols.items():
        leg = bundle.get(name)
        if not isinstance(leg, Mapping):
            reasons.append(f"missing_{name}_leg")
            continue
        symbol = str(leg.get("symbol") or "").strip().upper()
        if not expected_symbol or symbol != expected_symbol:
            reasons.append(f"{name}_symbol_mismatch")
        try:
            price = _finite(leg.get("entry_price"), f"{name}.entry_price")
            if price <= 0.0:
                raise ValueError("non-positive")
            stamp, _, entry_bar, evaluation_start = _actual_entry_boundary(
                leg.get("entry_timestamp"), str(decision["trade_date"])
            )
        except (TypeError, ValueError):
            reasons.append(f"invalid_{name}_entry")
            continue
        normalized_legs[name] = {
            "symbol": symbol,
            "entry_price": price,
            "entry_timestamp": stamp,
            "entry_bar_time": entry_bar,
            "evaluation_start_time": evaluation_start,
        }
    return not reasons, {
        "valid": not reasons,
        "reasons": reasons,
        "committed_in_decision_id": True,
        "legs": normalized_legs,
    }


def evaluate_selector_shadow(
    decision: Mapping[str, Any],
    control_candles: Sequence[Any],
    challenger_candles: Sequence[Any],
    *,
    candle_source: Any = "caller_supplied",
    assumed_round_trip_cost_pct: float = 0.08,
    evaluated_at: str | None = None,
    evaluator_sha256: str | None = None,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the selector pair; weak execution evidence remains diagnostic only."""
    manifest_value = _manifest_or_default(manifest)
    frozen = validate_selector_shadow_decision(decision, manifest=manifest_value)
    expected_evaluator_hash = str(
        manifest_value["artifacts"]["evaluate_forward_shadows.py"]
    )
    evaluator_hash = evaluator_sha256 or sha256_file(DEFAULT_EVALUATOR_PATH)
    if evaluator_hash != expected_evaluator_hash:
        raise ValueError("selector outcome evaluator is not the registered artifact")
    control_symbol = str(frozen["control_selected_symbol"])
    challenger_symbol = str(frozen.get("recommended_symbol") or "")
    if not control_symbol or not challenger_symbol:
        raise ValueError("selector outcome requires both frozen symbols")
    by_symbol = {
        str(row.get("symbol")): row
        for row in frozen["candidates"]
        if isinstance(row, Mapping)
    }
    try:
        control_candidate = by_symbol[control_symbol]
        challenger_candidate = by_symbol[challenger_symbol]
    except KeyError as exc:
        raise ValueError("selector outcome symbol is absent from frozen candidates") from exc
    evidence_ok, evidence = _selector_execution_evidence(frozen)
    cost = _finite(assumed_round_trip_cost_pct, "assumed_round_trip_cost_pct")
    if cost < 0.0 or cost > 20.0:
        raise ValueError("assumed selector cost must be between 0% and 20%")

    def run_leg(
        name: str,
        candidate: Mapping[str, Any],
        raw_candles: Sequence[Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        rows = [_normalize_candle(item) for item in raw_candles]
        leg = evidence.get("legs", {}).get(name) if evidence_ok else None
        if isinstance(leg, Mapping):
            entry = float(leg["entry_price"])
            evaluation_start = str(leg["evaluation_start_time"])
            entry_source = "frozen_point_in_time_executable_quote"
        else:
            evaluation_start = "07:00"
            eligible_rows = [row for row in rows if str(row["time"]) >= evaluation_start]
            if not eligible_rows:
                raise ValueError(f"no {name} candle at or after 07:00")
            entry = float(eligible_rows[0]["open"])
            entry_source = "diagnostic_first_candle_open"
        window = [row for row in rows if str(row["time"]) >= evaluation_start]
        target = _finite(candidate.get("target_price"), f"{name}.target_price")
        result = simulate_exit_policy(
            direction=str(candidate.get("direction")),
            entry_price=entry,
            frozen_target_price=target,
            candles=window,
            stop_pct=float(CONTROL_EXIT["stop_pct"]),
            target_multiple=float(CONTROL_EXIT["target_multiple"]),
            time_exit=str(CONTROL_EXIT["time_exit"]),
            round_trip_cost_pct=cost,
        )
        result["entry_price_source"] = entry_source
        result["evaluation_start_time"] = evaluation_start
        return result, rows

    control, normalized_control = run_leg(
        "control", control_candidate, control_candles
    )
    challenger, normalized_challenger = run_leg(
        "challenger", challenger_candidate, challenger_candles
    )
    try:
        source_snapshot = json.loads(canonical_json(candle_source))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"candle_source is not canonical JSON: {exc}") from exc
    divergence = control_symbol != challenger_symbol
    promotion_eligible = bool(frozen["eligible_session"] and evidence_ok)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "selector_shadow_outcome",
        "capture_mode": "forward",
        "policy": SELECTOR_POLICY,
        "shadow_only": True,
        "production_effect": "none",
        "execution_claim": False,
        "evaluated_at": _created_at(evaluated_at),
        "trade_date": frozen["trade_date"],
        "decision_id": frozen["decision_id"],
        "decision_sha256": canonical_sha256(frozen),
        "manifest_sha256": frozen["manifest_sha256"],
        "evaluator_sha256": evaluator_hash,
        "control_selected_symbol": control_symbol,
        "recommended_symbol": challenger_symbol,
        "eligible_session": bool(frozen["eligible_session"]),
        "divergence": divergence,
        "selector_execution_evidence": evidence,
        "promotion_eligible": promotion_eligible,
        "control_candles_sha256": canonical_sha256(normalized_control),
        "challenger_candles_sha256": canonical_sha256(normalized_challenger),
        "candle_source": source_snapshot,
        "control": control,
        "challenger": challenger,
    }
    record["comparison"] = {
        "challenger_minus_control_pp": (
            float(challenger["net_return_pct"]) - float(control["net_return_pct"])
        )
    }
    identity = {
        "schema_version": SCHEMA_VERSION,
        "record_type": record["record_type"],
        "policy": SELECTOR_POLICY,
        "manifest_sha256": record["manifest_sha256"],
        "trade_date": record["trade_date"],
        "decision_id": record["decision_id"],
        "decision_sha256": record["decision_sha256"],
        "evaluator_sha256": evaluator_hash,
        "control_candles_sha256": record["control_candles_sha256"],
        "challenger_candles_sha256": record["challenger_candles_sha256"],
        "control_selected_symbol": control_symbol,
        "recommended_symbol": challenger_symbol,
        "eligible_session": record["eligible_session"],
        "divergence": divergence,
        "selector_execution_evidence": evidence,
        "promotion_eligible": promotion_eligible,
        "control": control,
        "challenger": challenger,
    }
    record["outcome_identity"] = identity
    record["outcome_id"] = canonical_sha256(identity)
    record["provenance"] = {
        "decision_id": record["decision_id"],
        "decision_sha256": record["decision_sha256"],
        "manifest_sha256": record["manifest_sha256"],
        "evaluator_sha256": evaluator_hash,
        "control_candles_sha256": record["control_candles_sha256"],
        "challenger_candles_sha256": record["challenger_candles_sha256"],
    }
    return record


def validate_shadow_outcome(
    record: Mapping[str, Any],
    *,
    policy: str,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate immutable outcome IDs and all promotion-critical provenance."""
    if not isinstance(record, Mapping):
        raise ValueError("shadow outcome must be a mapping")
    try:
        snapshot = json.loads(canonical_json(dict(record)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"shadow outcome is not canonical JSON: {exc}") from exc
    manifest_value = _manifest_or_default(manifest)
    expected_manifest = str(manifest_value["manifest_sha256"])
    expected_evaluator = str(
        manifest_value["artifacts"]["evaluate_forward_shadows.py"]
    )
    expected_type = (
        "exit_shadow_outcome" if policy == EXIT_POLICY else "selector_shadow_outcome"
    )
    fixed = {
        "schema_version": SCHEMA_VERSION,
        "record_type": expected_type,
        "capture_mode": "forward",
        "policy": policy,
        "shadow_only": True,
        "production_effect": "none",
        "manifest_sha256": expected_manifest,
        "evaluator_sha256": expected_evaluator,
    }
    for field, expected in fixed.items():
        if snapshot.get(field) != expected:
            raise ValueError(f"outcome {field} mismatch")
    _validate_evidence_date(_iso_date(snapshot.get("trade_date")), manifest_value)
    identity = snapshot.get("outcome_identity")
    if not isinstance(identity, Mapping):
        raise ValueError("outcome_identity is missing")
    outcome_id = _require_sha256(snapshot.get("outcome_id"), "outcome_id")
    if outcome_id != canonical_sha256(identity):
        raise ValueError("outcome_id does not match outcome_identity")
    if identity.get("record_type") != expected_type:
        raise ValueError("outcome identity record_type mismatch")
    if identity.get("policy") != policy:
        raise ValueError("outcome identity policy mismatch")
    common_pairs = (
        "manifest_sha256",
        "evaluator_sha256",
        "control",
        "challenger",
    )
    for field in common_pairs:
        if identity.get(field) != snapshot.get(field):
            raise ValueError(f"outcome identity {field} mismatch")
    if identity.get("trade_date") != snapshot.get("trade_date"):
        raise ValueError("outcome identity trade_date mismatch")
    provenance = snapshot.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("outcome provenance is missing")
    for field, value in provenance.items():
        if snapshot.get(field) != value:
            raise ValueError(f"outcome provenance {field} mismatch")
    _paired_returns(snapshot)
    if policy == EXIT_POLICY:
        for field in (
            "plan_id",
            "decision_id",
            "plan_sha256",
            "candles_sha256",
        ):
            _require_sha256(snapshot.get(field), field)
        for field in (
            "symbol",
            "direction",
            "plan_id",
            "decision_id",
            "plan_sha256",
            "candles_sha256",
            "evaluation_start_time",
            "entry_price",
            "entry_price_source",
            "entry_bar_excluded",
            "actual_round_trip_cost_pct",
        ):
            if identity.get(field) != snapshot.get(field):
                raise ValueError(f"exit outcome identity {field} mismatch")
        if snapshot.get("plan_id") != snapshot.get("decision_id"):
            raise ValueError("exit plan_id and decision_id must match")
        if snapshot.get("entry_price_source") != "actual_frozen_fill":
            raise ValueError("exit promotion requires an actual frozen fill")
        if snapshot.get("entry_bar_excluded") is not True:
            raise ValueError("exit promotion requires entry-bar exclusion")
    else:
        for field in (
            "decision_id",
            "decision_sha256",
            "control_candles_sha256",
            "challenger_candles_sha256",
        ):
            _require_sha256(snapshot.get(field), field)
        for field in (
            "decision_id",
            "decision_sha256",
            "control_candles_sha256",
            "challenger_candles_sha256",
            "control_selected_symbol",
            "recommended_symbol",
            "eligible_session",
            "divergence",
            "selector_execution_evidence",
            "promotion_eligible",
        ):
            if identity.get(field) != snapshot.get(field):
                raise ValueError(f"selector outcome identity {field} mismatch")
        computed_divergence = (
            snapshot.get("control_selected_symbol") != snapshot.get("recommended_symbol")
        )
        if snapshot.get("divergence") is not computed_divergence:
            raise ValueError("selector divergence is inconsistent with symbols")
        execution = snapshot.get("selector_execution_evidence")
        execution_valid = isinstance(execution, Mapping) and execution.get("valid") is True
        expected_eligible = bool(snapshot.get("eligible_session") and execution_valid)
        if snapshot.get("promotion_eligible") is not expected_eligible:
            raise ValueError("selector promotion_eligible is inconsistent")
    comparison = snapshot.get("comparison")
    if not isinstance(comparison, Mapping):
        raise ValueError("outcome comparison is missing")
    control_return, challenger_return = _paired_returns(snapshot)
    expected_delta = challenger_return - control_return
    actual_delta = _finite(
        comparison.get("challenger_minus_control_pp"),
        "comparison.challenger_minus_control_pp",
    )
    if not math.isclose(actual_delta, expected_delta, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("outcome comparison is inconsistent with paired returns")
    return snapshot


def write_first_writer_wins(
    report: Mapping[str, Any],
    directory: str | os.PathLike[str],
    *,
    current_date: date | None = None,
    manifest: Mapping[str, Any] | None = None,
) -> Path:
    """Atomically publish the first forward record for policy/type/date."""
    trade_date = date.fromisoformat(_iso_date(report.get("trade_date")))
    today = current_date or date.today()
    if trade_date != today:
        raise ValueError(
            "forward first-writer audit requires trade_date == current_date: "
            f"{trade_date.isoformat()} != {today.isoformat()}"
        )
    if report.get("capture_mode") != "forward" or report.get("shadow_only") is not True:
        raise ValueError("only forward shadow records may be published")
    manifest_value = _manifest_or_default(manifest)
    _validate_evidence_date(trade_date.isoformat(), manifest_value)
    expected_manifest_hash = str(
        manifest_value.get("manifest_sha256") or manifest_sha256(manifest_value)
    )
    if report.get("manifest_sha256") != expected_manifest_hash:
        raise ValueError("record manifest_sha256 does not match registered manifest")
    policy = str(report.get("policy") or "")
    record_type = str(report.get("record_type") or "")
    if policy not in {EXIT_POLICY, SELECTOR_POLICY} or not record_type:
        raise ValueError("unknown policy or missing record_type")
    if record_type == "exit_shadow_plan" and policy == EXIT_POLICY:
        validated_report = validate_exit_shadow_plan(report, manifest=manifest_value)
    elif record_type == "selector_shadow_decision" and policy == SELECTOR_POLICY:
        validated_report = validate_selector_shadow_decision(
            report, manifest=manifest_value
        )
    elif record_type in {"exit_shadow_outcome", "selector_shadow_outcome"}:
        validated_report = validate_shadow_outcome(
            report, policy=policy, manifest=manifest_value
        )
    else:
        raise ValueError("record_type does not belong to registered policy")
    identity_field = "outcome_id" if record_type.endswith("_outcome") else "decision_id"
    identity = validated_report.get(identity_field)
    if not isinstance(identity, str) or len(identity) != 64:
        raise ValueError(f"record requires a 64-character {identity_field}")
    output_dir = Path(directory) / policy / record_type
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{trade_date.isoformat()}.json"
    descriptor, temporary = tempfile.mkstemp(
        dir=output_dir,
        prefix=f".{trade_date.isoformat()}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                validated_report,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        linked = False
        try:
            os.link(temporary, destination)
            linked = True
        except FileExistsError:
            pass
        os.unlink(temporary)
        if not linked:
            try:
                existing = json.loads(destination.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"existing immutable audit is unreadable: {destination}") from exc
            if existing.get(identity_field) != identity:
                raise RuntimeError(
                    f"immutable audit identity conflict for {destination}: "
                    f"existing {existing.get(identity_field)!r}, new {identity!r}"
                )
        directory_fd = os.open(output_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return destination


def compound(returns_pct: Iterable[float]) -> float:
    return (math.prod(1.0 + float(value) / 100.0 for value in returns_pct) - 1.0) * 100.0


def trade_close_mdd(returns_pct: Iterable[float]) -> float:
    equity = peak = 1.0
    result = 0.0
    for value in returns_pct:
        equity *= 1.0 + float(value) / 100.0
        if equity <= 0.0:
            return -100.0
        peak = max(peak, equity)
        result = min(result, equity / peak - 1.0)
    return result * 100.0


def _paired_returns(record: Mapping[str, Any]) -> tuple[float, float]:
    if "control" in record and "challenger" in record:
        control = record["control"]
        challenger = record["challenger"]
        if isinstance(control, Mapping) and isinstance(challenger, Mapping):
            baseline_value = control.get("net_return_pct", control.get("net_assumed_return_pct"))
            shadow_value = challenger.get("net_return_pct", challenger.get("net_assumed_return_pct"))
            return _finite(baseline_value, "control net return"), _finite(
                shadow_value, "challenger net return"
            )
    return _finite(record.get("baseline_net_return_pct"), "baseline net return"), _finite(
        record.get("shadow_net_return_pct"), "shadow net return"
    )


def _paired_log_delta(control: float, challenger: float) -> float:
    if control <= -100.0 or challenger <= -100.0:
        raise ValueError("paired returns must be greater than -100%")
    return math.log1p(challenger / 100.0) - math.log1p(control / 100.0)


def paired_sign_flip_p_one_sided(log_deltas: Sequence[float]) -> dict[str, Any]:
    """Predeclared one-sided paired randomization test on log-return deltas."""
    values = [abs(float(value)) for value in log_deltas if abs(float(value)) > 1e-15]
    observed = sum(float(value) for value in log_deltas)
    if not values:
        return {"p_value": 1.0, "method": "degenerate_no_nonzero_pairs", "samples": 1}
    if len(values) <= 20:
        total = 1 << len(values)
        exceed = 0
        for mask in range(total):
            sampled = sum(
                value if mask & (1 << index) else -value
                for index, value in enumerate(values)
            )
            exceed += sampled >= observed - 1e-15
        return {
            "p_value": exceed / total,
            "method": "exact_paired_sign_flip",
            "samples": total,
        }
    rng = random.Random(PAIRED_SIGN_FLIP_SEED)
    exceed = 0
    for _ in range(PAIRED_SIGN_FLIP_SAMPLES):
        sampled = sum(value if rng.getrandbits(1) else -value for value in values)
        exceed += sampled >= observed - 1e-15
    return {
        "p_value": (exceed + 1.0) / (PAIRED_SIGN_FLIP_SAMPLES + 1.0),
        "method": "deterministic_monte_carlo_paired_sign_flip",
        "samples": PAIRED_SIGN_FLIP_SAMPLES,
        "seed": PAIRED_SIGN_FLIP_SEED,
    }


def promotion_metrics(
    records: Sequence[Mapping[str, Any]],
    *,
    policy: str,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the pre-registered forward-only promotion gate.

    The function reports evidence only; it cannot activate either policy.
    Every accepted row must be a registered evaluator outcome with a valid
    immutable ID and provenance.  Raw hand-written returns are never evidence.
    """
    manifest_value = _manifest_or_default(manifest)
    if policy not in {EXIT_POLICY, SELECTOR_POLICY}:
        raise ValueError("unknown shadow policy")
    not_before = date.fromisoformat(str(manifest_value["evidence_not_before"]))
    expected_manifest_hash = str(
        manifest_value.get("manifest_sha256") or manifest_sha256(manifest_value)
    )
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    seen_dates: set[str] = set()
    seen_outcomes: set[str] = set()
    for raw in sorted(records, key=lambda row: str(row.get("trade_date", ""))):
        try:
            trade_date_text = _iso_date(raw.get("trade_date"))
        except Exception:  # noqa: BLE001 - invalid evidence is reported, never fatal
            rejected.append({"trade_date": "invalid", "reason": "invalid_trade_date"})
            continue
        reason: str | None = None
        detail: str | None = None
        if raw.get("capture_mode") != "forward":
            reason = "not_forward"
        elif raw.get("policy") != policy:
            reason = "wrong_policy"
        elif raw.get("manifest_sha256") != expected_manifest_hash:
            reason = "wrong_manifest"
        elif date.fromisoformat(trade_date_text) < not_before:
            reason = "before_evidence_window"
        else:
            try:
                validated = validate_shadow_outcome(
                    raw, policy=policy, manifest=manifest_value
                )
            except (RuntimeError, TypeError, ValueError) as exc:
                reason = "invalid_outcome"
                detail = str(exc)
        outcome_id = str(raw.get("outcome_id") or "")
        if reason is None and trade_date_text in seen_dates:
            reason = "duplicate_date"
        elif reason is None and outcome_id in seen_outcomes:
            reason = "duplicate_outcome_id"
        if reason is not None:
            item = {"trade_date": trade_date_text, "reason": reason}
            if detail:
                item["detail"] = detail
            rejected.append(item)
            continue
        seen_dates.add(trade_date_text)
        seen_outcomes.add(outcome_id)
        accepted.append(validated)

    if policy == SELECTOR_POLICY:
        eligible = [
            row
            for row in accepted
            if row.get("eligible_session") is True
            and row.get("promotion_eligible") is True
        ]
    else:
        eligible = accepted
    baseline = []
    challenger = []
    divergences = 0
    for row in eligible:
        control, shadow = _paired_returns(row)
        baseline.append(control)
        challenger.append(shadow)
        if policy == SELECTOR_POLICY:
            divergences += bool(
                row["control_selected_symbol"] != row["recommended_symbol"]
            )

    split = len(eligible) // 2
    first_baseline, second_baseline = baseline[:split], baseline[split:]
    first_challenger, second_challenger = challenger[:split], challenger[split:]
    first_delta = (
        compound(first_challenger) - compound(first_baseline) if first_baseline else 0.0
    )
    second_delta = (
        compound(second_challenger) - compound(second_baseline) if second_baseline else 0.0
    )
    deltas = [shadow - control for control, shadow in zip(baseline, challenger, strict=True)]
    log_deltas = [
        _paired_log_delta(control, shadow)
        for control, shadow in zip(baseline, challenger, strict=True)
    ]
    positive = [value for value in log_deltas if value > 0.0]
    positive_sum = sum(positive)
    max_share = max(positive) / positive_sum * 100.0 if positive_sum > 0.0 else 100.0
    sign_flip = paired_sign_flip_p_one_sided(log_deltas)
    baseline_mdd = trade_close_mdd(baseline)
    challenger_mdd = trade_close_mdd(challenger)
    sample_ok = (
        len(eligible) >= EXIT_MIN_NEW_TRADES
        if policy == EXIT_POLICY
        else len(eligible) >= SELECTOR_MIN_ELIGIBLE_SESSIONS
    )
    gates = {
        "minimum_sample": sample_ok,
        "minimum_divergences": (
            True if policy == EXIT_POLICY else divergences >= SELECTOR_MIN_DIVERGENCES
        ),
        "causal_point_in_time_executability": (
            True
            if policy == EXIT_POLICY
            else bool(eligible)
            and all(row.get("promotion_eligible") is True for row in eligible)
        ),
        "positive_delta_first_chronological_half": first_delta > 0.0,
        "positive_delta_second_chronological_half": second_delta > 0.0,
        "mdd_nonworse": challenger_mdd >= baseline_mdd - 1e-12,
        "max_positive_contributor_share_at_most_35pct": (
            max_share <= MAX_POSITIVE_CONTRIBUTOR_SHARE_PCT + 1e-12
        ),
        "paired_sign_flip_p_at_most_0_10": (
            float(sign_flip["p_value"]) <= PAIRED_SIGN_FLIP_P_MAX + 1e-12
        ),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "policy": policy,
        "shadow_only": True,
        "production_activation_allowed": False,
        "accepted_records": len(accepted),
        "diagnostic_only_records": (
            0
            if policy == EXIT_POLICY
            else sum(row.get("promotion_eligible") is not True for row in accepted)
        ),
        "eligible_sessions_or_trades": len(eligible),
        "divergences": divergences if policy == SELECTOR_POLICY else None,
        "rejected_records": rejected,
        "chronological_split_index": split,
        "first_half_delta_pp": first_delta,
        "second_half_delta_pp": second_delta,
        "full_baseline_return_pct": compound(baseline),
        "full_challenger_return_pct": compound(challenger),
        "full_delta_pp": compound(challenger) - compound(baseline),
        "baseline_mdd_pct": baseline_mdd,
        "challenger_mdd_pct": challenger_mdd,
        "max_positive_contributor_share_pct": max_share,
        "concentration_basis": "positive paired log-return deltas",
        "paired_sign_flip_one_sided": sign_flip,
        "holm_adjusted_p_single_registered_challenger": sign_flip["p_value"],
        "gates": gates,
        "promotion_ready": all(gates.values()),
        "warning": (
            "promotion_ready is a review signal only; manual review is required and "
            "this module cannot activate production"
        ),
    }


__all__ = [
    "EXIT_POLICY",
    "SELECTOR_POLICY",
    "attach_decision_id",
    "build_exit_shadow_plan",
    "canonical_sha256",
    "evaluate_exit_shadow",
    "evaluate_max_vol_expansion_top6",
    "evaluate_selector_shadow",
    "fail_open",
    "load_manifest",
    "promotion_metrics",
    "simulate_exit_policy",
    "validate_exit_shadow_plan",
    "validate_selector_shadow_decision",
    "validate_shadow_outcome",
    "write_first_writer_wins",
]
