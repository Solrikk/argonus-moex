#!/usr/bin/env python3
"""Read-only promotion report for immutable Argonus shadow outcomes."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from argonus.shadow import forward_shadow_research as registry


class ReportError(ValueError):
    pass


def _records_from_json(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        if str(value.get("record_type", "")).endswith("_outcome"):
            return [dict(value)]
        for key in ("outcomes", "records", "journal"):
            rows = value.get(key)
            if isinstance(rows, list):
                return [dict(row) for row in rows if isinstance(row, Mapping)]
        raise ReportError("JSON object contains no outcome record/list")
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    raise ReportError("outcome input must be a JSON object or array")


def load_outcome_file(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    artifact = Path(path)
    if not artifact.is_file():
        raise ReportError(f"outcome artifact does not exist: {artifact}")
    text = artifact.read_text(encoding="utf-8")
    if artifact.suffix.lower() == ".jsonl":
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReportError(
                    f"invalid JSONL line {line_number} in {artifact}: {exc}"
                ) from exc
            if isinstance(value, Mapping) and str(
                value.get("record_type", "")
            ).endswith("_outcome"):
                records.append(dict(value))
        return records
    try:
        return _records_from_json(json.loads(text))
    except json.JSONDecodeError as exc:
        raise ReportError(f"invalid JSON in {artifact}: {exc}") from exc


def load_outcomes(paths: Sequence[str | os.PathLike[str]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            # Per-outcome JSON is canonical.  JSONL ledgers are intentionally
            # skipped here so passing a directory cannot double-count both.
            files = sorted(
                item
                for item in path.rglob("*.json")
                if item.is_file() and not item.name.startswith(".")
            )
            for item in files:
                try:
                    records.extend(load_outcome_file(item))
                except ReportError as exc:
                    # A shadow root also contains decision/plan JSON.  Those
                    # are not outcomes and are safely ignored by directory mode.
                    if "contains no outcome record/list" in str(exc):
                        continue
                    raise
        else:
            records.extend(load_outcome_file(path))
    return records


def write_atomic_report(report: Mapping[str, Any], path: str | os.PathLike[str]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        dict(report), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n"
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
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate forward shadow outcomes and print the frozen promotion gates; "
            "never changes production settings."
        )
    )
    parser.add_argument(
        "--policy", choices=("exit", "selector"), required=True
    )
    parser.add_argument(
        "--outcomes",
        action="append",
        required=True,
        help="Outcome JSON/JSONL file or directory; repeatable.",
    )
    parser.add_argument("--output", help="Optional atomically replaced JSON report.")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="Return status 3 when gates are not ready (still never activates live trading).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        policy = (
            registry.EXIT_POLICY if args.policy == "exit" else registry.SELECTOR_POLICY
        )
        records = load_outcomes(args.outcomes)
        report = registry.promotion_metrics(records, policy=policy)
        if args.output:
            write_atomic_report(report, args.output)
        print(
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                indent=2 if args.pretty else None,
                allow_nan=False,
            )
        )
        return 3 if args.require_ready and not report["promotion_ready"] else 0
    except (OSError, ReportError, RuntimeError, TypeError, ValueError) as exc:
        print(f"shadow report failed: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
