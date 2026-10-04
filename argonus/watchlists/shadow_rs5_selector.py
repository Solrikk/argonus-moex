#!/usr/bin/env python3
"""Auditable RS5 overlay for Engine A candidate selection.

The module is intentionally broker-agnostic.  It only consumes already built
candidate objects and completed daily sessions, calculates a simple relative
strength feature, and writes an atomic JSON audit record.  No method in this
file can place or cancel an order.

RS5 is direction aligned against the market index:

* long:  five-session stock return minus five-session index return;
* short: five-session index return minus five-session stock return.

Every session on or after ``trade_date`` is discarded before the feature is
calculated.  This explicit filter protects the shadow study from look-ahead if
an upstream data source happens to include an incomplete trade-day candle.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 2
DEFAULT_THRESHOLD_PP = -2.39
DEFAULT_TOP_K = 3


def sha256_file(path: str | os.PathLike[str]) -> str | None:
    """Return a content hash, or ``None`` when an artifact is absent."""
    artifact = Path(path)
    if not artifact.is_file():
        return None
    digest = hashlib.sha256()
    with artifact.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def attach_decision_provenance(
    report: dict[str, Any],
    *,
    watchlist_text: str | None,
    analyzer_path: str | os.PathLike[str],
    model_paths: Mapping[str, str | os.PathLike[str]],
    selector_path: str | os.PathLike[str] = __file__,
) -> dict[str, Any]:
    """Attach reproducible source hashes and a stable canonical decision id."""
    watchlist_sha256 = (
        hashlib.sha256(watchlist_text.encode("utf-8")).hexdigest()
        if watchlist_text is not None
        else None
    )
    analyzer = {
        "name": Path(analyzer_path).name,
        "sha256": sha256_file(analyzer_path),
    }
    selector = {
        "name": Path(selector_path).name,
        "sha256": sha256_file(selector_path),
    }
    models = {
        name: {"name": Path(path).name, "sha256": sha256_file(path)}
        for name, path in sorted(model_paths.items())
    }
    artifacts_for_id = {
        "analyzer": analyzer["sha256"],
        "selector": selector["sha256"],
        "models": {name: value["sha256"] for name, value in models.items()},
    }
    decision_basis = {
        "trade_date": report.get("trade_date"),
        "watchlist_sha256": watchlist_sha256,
        "top3": report.get("candidates", [])[:DEFAULT_TOP_K],
        "threshold_pp": report.get("threshold_pp"),
        "artifacts": artifacts_for_id,
    }
    canonical = json.dumps(
        decision_basis,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    report["provenance"] = {
        "watchlist_sha256": watchlist_sha256,
        "watchlist_bytes": len(watchlist_text.encode("utf-8")) if watchlist_text is not None else None,
        "analyzer": analyzer,
        "selector": selector,
        "models": models,
        "decision_id_canonicalization": "sha256(canonical-json(date,watchlist,top3,threshold,artifacts))",
    }
    report["decision_id"] = hashlib.sha256(canonical).hexdigest()
    return report


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def capture_mode_for(trade_date: date, *, current_date: date | None = None) -> str:
    """Classify an audit as forward or historical; future dates are forbidden."""
    value = _as_date(trade_date)
    today = current_date or date.today()
    if value > today:
        raise ValueError(f"future trade_date is forbidden: {value.isoformat()} > {today.isoformat()}")
    return "forward" if value == today else "historical_reconstruction"


def completed_sessions(sessions: Iterable[Any], trade_date: date) -> list[Any]:
    """Return strictly pre-trade-date sessions in chronological order."""
    cutoff = _as_date(trade_date)
    completed = [
        session
        for session in sessions
        if _as_date(_value(session, "trade_date")) < cutoff
    ]
    completed.sort(key=lambda session: _as_date(_value(session, "trade_date")))
    return completed


def calculate_directional_rs5(
    direction: str,
    stock_sessions: Iterable[Any],
    index_sessions: Iterable[Any],
    trade_date: date,
) -> dict[str, Any] | None:
    """Calculate the exact pre-entry RS5 feature, or ``None`` without six bars."""
    if direction not in {"long", "short"}:
        return None
    stock = completed_sessions(stock_sessions, trade_date)
    index = completed_sessions(index_sessions, trade_date)
    if len(stock) < 6 or len(index) < 6:
        return None

    stock_start = float(_value(stock[-6], "close") or 0.0)
    stock_end = float(_value(stock[-1], "close") or 0.0)
    index_start = float(_value(index[-6], "close") or 0.0)
    index_end = float(_value(index[-1], "close") or 0.0)
    if min(stock_start, stock_end, index_start, index_end) <= 0.0:
        return None

    stock_return_pct = (stock_end / stock_start - 1.0) * 100.0
    index_return_pct = (index_end / index_start - 1.0) * 100.0
    aligned_rs5_pp = (
        stock_return_pct - index_return_pct
        if direction == "long"
        else index_return_pct - stock_return_pct
    )
    if not math.isfinite(aligned_rs5_pp):
        return None
    return {
        "stock_return_5d_pct": stock_return_pct,
        "index_return_5d_pct": index_return_pct,
        "aligned_rs5_pp": aligned_rs5_pp,
        "stock_window": [
            _as_date(_value(stock[-6], "trade_date")).isoformat(),
            _as_date(_value(stock[-1], "trade_date")).isoformat(),
        ],
        "index_window": [
            _as_date(_value(index[-6], "trade_date")).isoformat(),
            _as_date(_value(index[-1], "trade_date")).isoformat(),
        ],
    }


def evaluate_rs5_policy(
    analyses: Sequence[Any],
    trade_date: date,
    stock_sessions_by_rank: Mapping[int, Iterable[Any]],
    index_sessions: Iterable[Any],
    eligibility_reasons: Mapping[int, str | None],
    *,
    threshold_pp: float = DEFAULT_THRESHOLD_PP,
    top_k: int = DEFAULT_TOP_K,
    precomputed_metrics_by_rank: Mapping[int, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate the overlay without changing the caller's candidate list.

    The policy is deliberately narrow: it is considered only when the current
    rank-1 leader already passes the existing Engine-A gates.  It then scans
    rank 1 -> rank ``top_k`` and recommends the first otherwise-eligible idea
    with RS5 at or above the threshold.  Missing rank-1 history fails open and
    leaves live behaviour unchanged.
    """
    if not math.isfinite(threshold_pp):
        raise ValueError("threshold_pp must be finite")
    if top_k < 1:
        raise ValueError("top_k must be positive")

    rows: list[dict[str, Any]] = []
    for rank, item in enumerate(list(analyses)[:top_k], start=1):
        direction = str(_value(item, "direction", "unknown"))
        raw_precomputed = (precomputed_metrics_by_rank or {}).get(rank)
        precomputed = None
        invalid_precomputed = False
        if raw_precomputed is not None:
            try:
                stock_window = list(raw_precomputed["stock_window"])
                index_window = list(raw_precomputed["index_window"])
                windows_are_prior = (
                    len(stock_window) == 2
                    and len(index_window) == 2
                    and all(_as_date(value) < _as_date(trade_date) for value in stock_window)
                    and all(_as_date(value) < _as_date(trade_date) for value in index_window)
                )
                numeric_values = [
                    float(raw_precomputed["stock_return_5d_pct"]),
                    float(raw_precomputed["index_return_5d_pct"]),
                    float(raw_precomputed["aligned_rs5_pp"]),
                ]
                if not windows_are_prior or not all(math.isfinite(value) for value in numeric_values):
                    raise ValueError("precomputed RS5 violates the prior-session guard")
                precomputed = dict(raw_precomputed)
            except (KeyError, TypeError, ValueError):
                invalid_precomputed = True
        metrics = (
            dict(precomputed)
            if precomputed is not None
            else calculate_directional_rs5(
                direction,
                stock_sessions_by_rank.get(rank, ()),
                index_sessions,
                trade_date,
            )
        )
        reason = eligibility_reasons.get(rank)
        aligned = metrics.get("aligned_rs5_pp") if metrics is not None else None
        passes = bool(
            reason is None
            and aligned is not None
            and float(aligned) >= threshold_pp
        )
        exit_target = _value(item, "exit_target")
        row = {
            "rank": rank,
            "symbol": str(_value(item, "symbol", "")),
            "direction": direction,
            "overall_score": _value(item, "overall_score"),
            "target_label": _value(exit_target, "label") if exit_target is not None else None,
            "target_price": _value(exit_target, "price") if exit_target is not None else None,
            "target_move_pct": (
                _value(exit_target, "move_pct_from_close")
                if exit_target is not None
                else None
            ),
            "eligibility_reason": reason,
            "passes_existing_gates": reason is None,
            "passes_rs5": passes,
        }
        if metrics is None:
            row.update(
                {
                    "stock_return_5d_pct": None,
                    "index_return_5d_pct": None,
                    "aligned_rs5_pp": None,
                    "stock_window": None,
                    "index_window": None,
                    "data_status": (
                        "invalid_precomputed_history"
                        if invalid_precomputed
                        else "insufficient_completed_history"
                    ),
                }
            )
        else:
            row.update(metrics)
            row["data_status"] = "ok"
            row["data_source"] = (
                "analyze_watchlist_precomputed"
                if precomputed is not None
                else "selector_session_input"
            )
        rows.append(row)

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "trade_date": _as_date(trade_date).isoformat(),
        "evaluated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "threshold_pp": threshold_pp,
        "top_k": top_k,
        "feature": "directional_stock_return_5d_minus_index_return_5d",
        "lookahead_guard": "sessions.trade_date < trade_date",
        "candidates": rows,
        "current_leader_rank": rows[0]["rank"] if rows else None,
        "current_leader_symbol": rows[0]["symbol"] if rows else None,
        "recommended_rank": None,
        "recommended_symbol": None,
        "decision": "no_candidates",
        "decision_reason": "analysis list is empty",
    }
    if not rows:
        return report

    current = rows[0]
    if current["eligibility_reason"] is not None:
        report.update(
            {
                "recommended_rank": 1,
                "recommended_symbol": current["symbol"],
                "decision": "not_applicable_current_ineligible",
                "decision_reason": current["eligibility_reason"],
            }
        )
        return report
    if current["aligned_rs5_pp"] is None:
        report.update(
            {
                "recommended_rank": 1,
                "recommended_symbol": current["symbol"],
                "decision": "insufficient_data_fail_open",
                "decision_reason": "rank-1 has fewer than six completed stock/index sessions",
            }
        )
        return report

    selected = next((row for row in rows if row["passes_rs5"]), None)
    if selected is None:
        report.update(
            {
                "decision": "skip",
                "decision_reason": (
                    f"no eligible top-{len(rows)} candidate has aligned RS5 >= "
                    f"{threshold_pp:.2f} pp"
                ),
            }
        )
    elif selected["rank"] == 1:
        report.update(
            {
                "recommended_rank": 1,
                "recommended_symbol": selected["symbol"],
                "decision": "keep",
                "decision_reason": "current leader passes the RS5 floor",
            }
        )
    else:
        report.update(
            {
                "recommended_rank": selected["rank"],
                "recommended_symbol": selected["symbol"],
                "decision": "replacement",
                "decision_reason": (
                    f"rank-1 fails RS5; first passing candidate is rank-{selected['rank']}"
                ),
            }
        )
    return report


def write_atomic_report(report: Mapping[str, Any], directory: str | os.PathLike[str]) -> Path:
    """Persist an audit atomically; today's first forward decision is immutable."""
    trade_date = str(report.get("trade_date") or "")
    mode = capture_mode_for(_as_date(trade_date))  # validate before touching the filesystem
    supplied_mode = report.get("capture_mode")
    if supplied_mode is not None and supplied_mode != mode:
        raise ValueError(
            f"capture_mode {supplied_mode!r} does not match trade_date-derived mode {mode!r}"
        )
    payload = dict(report)
    payload["capture_mode"] = mode
    output_dir = Path(directory)
    if mode == "historical_reconstruction":
        output_dir = output_dir / "archive" / mode
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{trade_date}.json"
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{trade_date}.", suffix=".tmp", dir=output_dir
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if mode == "forward":
            # Atomic first-writer-wins publication.  A later tick must never
            # rewrite the first forward decision observed for this session.
            try:
                os.link(temporary_name, destination)
            except FileExistsError:
                pass
            os.unlink(temporary_name)
        else:
            os.replace(temporary_name, destination)
        directory_fd = os.open(output_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return destination
