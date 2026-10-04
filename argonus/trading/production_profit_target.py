"""Frozen target125 exit geometry and its production activation contract.

No broker calls. The original Engine-A T4 is frozen before order submission;
the new target is calculated once from the confirmed average fill.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path


POLICY = "target125"
MULTIPLIER = 1.25
REQUIRED_ARTIFACTS = {
    "argonus/paths.py", "argonus/serialization.py",
    "argonus/trading/trade_bot.py", "scripts/run_tick.sh", "argonus/trading/production_profit_target.py",
    "data/backtests/profit_first_2026-10-03/study_manifest.json",
    "data/backtests/profit_first_2026-10-03/selected_candidate.json",
    "data/backtests/profit_first_2026-10-03/report.json",
}


def _positive(value: float | str, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be positive and finite") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return number


def scaled_target_price(direction: str, entry_price: float, original_t4: float,
                        tick_size: float, multiplier: float = MULTIPLIER) -> float:
    if direction not in ("long", "short"):
        raise ValueError("Direction must be long or short")
    if multiplier != MULTIPLIER:
        raise ValueError("Only the frozen target125 multiplier is supported")
    entry = _positive(entry_price, "Entry price")
    t4 = _positive(original_t4, "Original T4")
    tick = _positive(tick_size, "Tick size")
    sign = Decimal(1 if direction == "long" else -1)
    if sign * (t4 - entry) <= 0:
        raise ValueError("Original T4 is behind the confirmed entry")
    target = entry + Decimal("1.25") * (t4 - entry)
    # Round toward entry, so a coarse tick does not enlarge the intended goal.
    rounding = ROUND_FLOOR if direction == "long" else ROUND_CEILING
    target = (target / tick).to_integral_value(rounding=rounding) * tick
    if target <= 0 or sign * (target - entry) <= 0:
        raise ValueError("Tick size cannot represent a profitable target")
    return float(target)


def validate_activation(manifest_path: str, *, project_dir: str,
                        runtime_config: dict, trade_date: date) -> tuple[bool, str, str | None]:
    """Fail closed on changed code, research artifacts, approval or config."""
    def invalid(reason: str) -> tuple[bool, str, None]:
        return False, reason, None

    try:
        raw = Path(manifest_path).read_bytes()
        manifest = json.loads(raw)
    except (OSError, ValueError) as exc:
        return invalid(f"target activation manifest unreadable: {exc}")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        return invalid("target activation schema_version is unsupported")
    if (manifest.get("policy") != POLICY or manifest.get("approved") is not True
            or manifest.get("production_activation_allowed") is not True):
        return invalid("target activation policy/approval mismatch")
    try:
        effective = date.fromisoformat(str(manifest.get("effective_from")))
    except ValueError:
        return invalid("target activation effective_from is invalid")
    if trade_date < effective:
        return invalid(f"target activation is effective from {effective.isoformat()}")
    evidence = manifest.get("activation_evidence") or {}
    if not isinstance(evidence, dict) or not (
        evidence.get("status") == "explicit_user_override"
        and evidence.get("forward_gate_passed") is False
        and evidence.get("approved_by") == "workspace_user_explicit_instruction"
        and evidence.get("approval_date") == effective.isoformat()
    ):
        return invalid("target activation explicit user instruction is missing")
    source = manifest.get("source_research") or {}
    if not isinstance(source, dict) or source.get("verdict") != "HISTORICAL_CANDIDATE_NOT_CONFIRMED":
        return invalid("target activation must preserve the unconfirmed research verdict")
    if manifest.get("required_config") != runtime_config:
        return invalid("target activation runtime config mismatch")
    if runtime_config.get("target_distance_multiplier") != MULTIPLIER:
        return invalid("target activation multiplier mismatch")
    artifacts = manifest.get("runtime_artifacts")
    if not isinstance(artifacts, dict) or REQUIRED_ARTIFACTS - set(artifacts):
        return invalid("target activation artifact pins are incomplete")
    root = Path(project_dir).resolve()
    for relative, expected in artifacts.items():
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root):
            return invalid(f"target activation artifact outside project: {relative}")
        try:
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            return invalid(f"target activation artifact missing: {relative}")
        if actual != expected:
            return invalid(f"target activation artifact hash mismatch: {relative}")
    return True, "valid_explicit_user_override", hashlib.sha256(raw).hexdigest()
