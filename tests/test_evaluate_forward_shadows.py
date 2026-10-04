#!/usr/bin/env python3
"""Network-free tests for the standalone post-trade shadow evaluator."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from argonus.shadow import evaluate_forward_shadows as subject
from argonus.shadow import forward_shadow_research as registry


from argonus.paths import PROJECT_ROOT as ROOT
NOW = datetime(2026, 7, 21, 12, 0, tzinfo=subject.MOSCOW)


def plan(**updates):
    trade_date = updates.pop("trade_date", "2026-07-20")
    entry_price = updates.pop("entry_price", 100.0)
    actual_stamp = updates.pop(
        "actual_entry_timestamp",
        f"{trade_date}T07:02:00+03:00" if entry_price is not None else None,
    )
    value = registry.build_exit_shadow_plan(
        trade_date=trade_date,
        symbol=updates.pop("symbol", "TEST"),
        direction=updates.pop("direction", "long"),
        entry_price=entry_price,
        actual_entry_timestamp=actual_stamp,
        target_price=updates.pop("target_price", 102.0),
        assumed_round_trip_cost_pct=updates.pop(
            "assumed_round_trip_cost_pct", 0.08
        ),
        created_at=updates.pop("created_at", f"{trade_date}T07:03:00+03:00"),
        manifest=registry.load_manifest(),
        optional_evidence=updates.pop("optional_evidence", {}),
    )
    value.update(updates)  # tests use remaining keys only to simulate tampering
    return value


def candles(*middle):
    return {
        "candles": [
            ["07:00", 100.0, 100.4, 99.6, 100.1],
            ["07:05", 100.0, 100.4, 99.6, 100.1],
            *middle,
            ["17:00", 100.0, 100.5, 99.5, 100.2],
            ["18:35", 100.2, 100.4, 100.0, 100.3],
        ]
    }


def evaluated(raw_plan=None, document=None, *, actual=None):
    normalized_plan = subject.normalize_plan(raw_plan or plan(), now=NOW)
    normalized_candles = subject.normalize_candles(
        document or candles(), normalized_plan["trade_date"]
    )
    return subject.evaluate_plan(
        normalized_plan,
        normalized_candles,
        {"kind": "offline_test"},
        actual_cost_override=actual,
        now=NOW,
    )


class FixtureTests(unittest.TestCase):
    def test_checked_in_offline_fixture_has_expected_control_and_challenger(self) -> None:
        raw_plan = plan(entry_price=99.8, target_price=102.0)
        document = {
            "candles": [
                ["07:00", 100.0, 100.4, 99.6, 100.2],
                ["07:05", 100.0, 100.4, 99.6, 100.2],
                ["09:00", 100.2, 102.1, 99.2, 101.9],
                ["17:00", 101.9, 102.45, 101.8, 102.4],
                ["18:35", 102.4, 102.5, 102.3, 102.45],
            ]
        }
        outcome = evaluated(raw_plan, document)

        expected_control_gross = (102.0 / 99.8 - 1.0) * 100.0
        expected_challenger_gross = (102.4 / 99.8 - 1.0) * 100.0
        self.assertAlmostEqual(outcome["control"]["gross_return_pct"], expected_control_gross)
        self.assertAlmostEqual(outcome["control"]["net_return_pct"], expected_control_gross - 0.08)
        self.assertEqual(outcome["control"]["exit_reason"], "target")
        self.assertEqual(outcome["control"]["exit_time"], "09:00")
        self.assertAlmostEqual(
            outcome["challenger"]["gross_return_pct"], expected_challenger_gross
        )
        self.assertEqual(outcome["challenger"]["exit_reason"], "time_exit")
        self.assertEqual(outcome["challenger"]["exit_time"], "17:00")
        self.assertAlmostEqual(outcome["challenger"]["target_price"], 102.55)
        self.assertEqual(outcome["entry_price_source"], "actual_frozen_fill")
        self.assertAlmostEqual(outcome["first_candle_open"], 100.0)
        self.assertFalse(outcome["execution_claim"])
        self.assertEqual(outcome["capture_mode"], "forward")

    def test_actual_forward_shadow_research_plan_contract_is_accepted(self) -> None:
        from argonus.shadow import forward_shadow_research as registry

        registered = registry.build_exit_shadow_plan(
            trade_date="2026-07-20",
            symbol="TEST",
            direction="long",
            entry_price=100.0,
            actual_entry_timestamp="2026-07-20T07:02:00+03:00",
            target_price=104.0,
            created_at="2026-07-20T06:59:00+03:00",
            manifest=registry.load_manifest(),
        )
        after_session = datetime(2026, 7, 21, 12, 0, tzinfo=subject.MOSCOW)
        accepted = subject.normalize_plan(registered, now=after_session)
        rows = subject.normalize_candles(
            {
                "candles": [
                    ["07:00", 100.0, 100.5, 99.8, 100.2],
                    ["07:05", 100.0, 100.5, 99.8, 100.2],
                    ["16:55", 100.2, 104.5, 100.0, 104.0],
                    ["17:00", 104.0, 104.8, 103.8, 104.5],
                    ["18:35", 104.5, 105.1, 104.4, 105.0],
                ]
            },
            accepted["trade_date"],
        )
        outcome = subject.evaluate_plan(
            accepted, rows, {"kind": "offline_contract_test"}, now=after_session
        )

        self.assertEqual(outcome["plan_id"], registered["plan_id"])
        self.assertEqual(outcome["manifest_sha256"], registered["manifest_sha256"])
        self.assertEqual(outcome["capture_mode"], "forward")
        self.assertIn("net_return_pct", outcome["control"])
        self.assertIn("net_return_pct", outcome["challenger"])


class ExitPolicyTests(unittest.TestCase):
    def test_ambiguous_candle_is_stop_first_for_both_engines(self) -> None:
        document = candles(["08:00", 100.0, 103.0, 98.0, 100.0])
        outcome = evaluated(document=document)

        self.assertEqual(outcome["control"]["exit_reason"], "both_stop_first")
        self.assertTrue(outcome["control"]["ambiguous"])
        self.assertAlmostEqual(outcome["control"]["gross_return_pct"], -1.0)
        self.assertEqual(outcome["challenger"]["exit_reason"], "both_stop_first")
        self.assertTrue(outcome["challenger"]["ambiguous"])
        self.assertAlmostEqual(outcome["challenger"]["gross_return_pct"], -1.25)

    def test_short_target_and_target_distance_are_direction_aligned(self) -> None:
        raw = plan(direction="short", target_price=98.0)
        document = {
            "candles": [
                ["07:00", 100.0, 100.4, 99.5, 99.8],
                ["07:05", 100.0, 100.4, 99.5, 99.8],
                ["09:00", 99.8, 100.0, 97.9, 98.2],
                ["17:00", 98.2, 98.3, 97.6, 97.7],
                ["18:35", 97.7, 97.9, 97.5, 97.8],
            ]
        }
        outcome = evaluated(raw, document)

        self.assertEqual(outcome["control"]["exit_reason"], "target")
        self.assertAlmostEqual(outcome["control"]["gross_return_pct"], 2.0)
        self.assertAlmostEqual(outcome["challenger"]["target_price"], 97.5)
        self.assertEqual(outcome["challenger"]["exit_reason"], "time_exit")
        self.assertAlmostEqual(outcome["challenger"]["gross_return_pct"], 2.3)

    def test_gap_bar_uses_the_single_registered_stop_price_convention(self) -> None:
        document = candles(["09:00", 98.0, 98.5, 97.5, 98.2])
        outcome = evaluated(document=document)

        self.assertEqual(outcome["control"]["exit_reason"], "stop")
        self.assertAlmostEqual(outcome["control"]["exit_price"], 99.0)
        self.assertAlmostEqual(outcome["control"]["gross_return_pct"], -1.0)
        self.assertEqual(outcome["challenger"]["exit_reason"], "stop")
        self.assertAlmostEqual(outcome["challenger"]["gross_return_pct"], -1.25)

    def test_actual_cost_is_kept_alongside_assumption_and_becomes_primary(self) -> None:
        outcome = evaluated(actual=0.23)
        gross = outcome["control"]["gross_return_pct"]

        self.assertAlmostEqual(outcome["control"]["net_assumed_return_pct"], gross - 0.08)
        self.assertAlmostEqual(outcome["control"]["net_actual_return_pct"], gross - 0.23)
        self.assertAlmostEqual(outcome["control"]["net_return_pct"], gross - 0.23)
        self.assertEqual(outcome["control"]["cost_basis"], "actual_provided")
        self.assertEqual(outcome["costs"]["used_basis"], "actual_provided")

    def test_first_candle_open_is_explicit_non_fill_fallback(self) -> None:
        outcome = evaluated(plan(entry_price=None))

        self.assertEqual(outcome["entry_price"], 100.0)
        self.assertEqual(
            outcome["entry_price_source"], "first_candle_open_fallback"
        )
        self.assertFalse(outcome["entry_bar_excluded"])
        with self.assertRaisesRegex(ValueError, "actual frozen fill"):
            registry.validate_shadow_outcome(
                outcome, policy=registry.EXIT_POLICY
            )


class ValidationTests(unittest.TestCase):
    def test_incomplete_session_is_rejected(self) -> None:
        raw = subject.normalize_plan(plan(), now=NOW)
        incomplete = subject.normalize_candles(
            {
                "candles": [
                    ["07:00", 100, 101, 99, 100],
                    ["07:05", 100, 101, 99, 100],
                    ["17:00", 100, 101, 99, 100],
                ]
            },
            raw["trade_date"],
        )
        with self.assertRaisesRegex(subject.EvaluationError, "18:35"):
            subject.evaluate_plan(raw, incomplete, "offline", now=NOW)

    def test_future_plan_and_mutated_engine_config_are_rejected(self) -> None:
        with self.assertRaisesRegex(subject.EvaluationError, "future trade_date"):
            subject.normalize_plan(plan(trade_date="2026-07-22"), now=NOW)
        changed = plan()
        changed["challenger"] = dict(changed["challenger"], target_multiple=1.5)
        with self.assertRaisesRegex(subject.EvaluationError, "registered decision"):
            subject.normalize_plan(changed, now=NOW)

    def test_forward_plan_requires_manifest_and_shadow_only(self) -> None:
        with self.assertRaisesRegex(subject.EvaluationError, "registered decision"):
            subject.normalize_plan(plan(manifest_sha256=None), now=NOW)
        with self.assertRaisesRegex(subject.EvaluationError, "registered decision"):
            subject.normalize_plan(plan(shadow_only=False), now=NOW)

    def test_utc_candle_timestamps_are_converted_to_moscow(self) -> None:
        rows = subject.normalize_candles(
            {"rows": [["2026-07-17T04:00:00Z", 100, 101, 99, 100]]},
            "2026-07-17",
        )
        self.assertEqual(rows[0].time, "07:00")


class PersistenceAndCliTests(unittest.TestCase):
    def test_first_writer_wins_and_jsonl_append_is_idempotent(self) -> None:
        outcome = evaluated()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            path, created = subject.write_immutable_outcome(outcome, directory, ledger)
            same_path, same_created = subject.write_immutable_outcome(outcome, directory, ledger)

            self.assertTrue(created)
            self.assertFalse(same_created)
            self.assertEqual(path, same_path)
            self.assertEqual(len(ledger.read_text(encoding="utf-8").splitlines()), 1)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["outcome_id"], outcome["outcome_id"])
            self.assertEqual(list(Path(directory).rglob("*.tmp")), [])

            conflict = evaluated(actual=0.23)
            with self.assertRaisesRegex(subject.EvaluationError, "conflict"):
                subject.write_immutable_outcome(conflict, directory, ledger)

    def test_mixed_jsonl_loader_ignores_non_plan_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.jsonl"
            frozen = plan()
            journal.write_text(
                json.dumps({"record_type": "selector_shadow_decision"})
                + "\n"
                + json.dumps(frozen)
                + "\n",
                encoding="utf-8",
            )
            records = subject.load_plan_records(journal)
        self.assertEqual([item["plan_id"] for item in records], [frozen["plan_id"]])

    def test_cli_runs_entirely_offline_and_writes_immutable_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            candles_path = Path(directory) / "candles.json"
            plan_path.write_text(json.dumps(plan()), encoding="utf-8")
            candles_path.write_text(json.dumps(candles()), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch.object(
                subject, "_now_moscow", return_value=NOW
            ):
                code = subject.main(
                    [
                        "--plans",
                        str(plan_path),
                        "--candles-file",
                        str(candles_path),
                        "--outcomes-dir",
                        directory,
                    ]
                )
            summary = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(summary["evaluated"], 1)
            self.assertTrue(summary["outcomes"][0]["created"])
            self.assertTrue(Path(summary["outcomes"][0]["path"]).is_file())
            self.assertTrue((Path(directory) / "outcomes.jsonl").is_file())

    def test_module_has_no_live_bot_or_order_dependency(self) -> None:
        source = (ROOT / "argonus/shadow/evaluate_forward_shadows.py").read_text(encoding="utf-8")
        self.assertNotIn("import trade_bot", source)
        self.assertNotIn("post_order", source)
        self.assertNotIn("cancel_order", source)


if __name__ == "__main__":
    unittest.main()
