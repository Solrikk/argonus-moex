#!/usr/bin/env python3
"""Network- and broker-free tests for the delayed-entry evaluator."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from argonus.shadow import evaluate_delayed_entry_shadow as subject


from argonus.paths import PROJECT_ROOT as ROOT
NOW = datetime(2026, 7, 21, 12, 0, tzinfo=subject.MOSCOW)


def capture(**updates):
    trade_date = updates.pop("trade_date", "2026-07-20")
    value = {
        "schema_version": 1,
        "record_type": "delayed_entry_shadow_capture",
        "capture_mode": "forward",
        "policy": subject.POLICY,
        "shadow_only": True,
        "production_effect": "none",
        "execution_claim": False,
        "created_at": f"{trade_date}T07:05:06+03:00",
        "trade_date": trade_date,
        "symbol": "TEST",
        "uid": "uid-test",
        "direction": "long",
        "plan_id": "5" * 64,
        "source_exit_plan_id": "source-test",
        "source_exit_plan_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "target_price": 102.0,
        "assumed_round_trip_cost_pct": 0.08,
        "control": {
            "actual_fill_price": 100.0,
            "actual_fill_timestamp": f"{trade_date}T07:02:00+03:00",
            "actual_filled_lots": 10,
            "lot_size": 1,
        },
        "challenger": {
            "entry_price_source": "executable_orderbook_vwap",
            "executable_vwap": 100.0,
            "requested_lots": 10,
            "covered_lots": 10,
            "depth_sufficient": True,
            "side": "asks",
            "book_depth": 50,
            "best_touch": 99.9,
            "worst_price": 100.1,
            "orderbook_ts": f"{trade_date}T07:05:04+03:00",
            "request_started_at": f"{trade_date}T07:05:04+03:00",
            "response_received_at": f"{trade_date}T07:05:05+03:00",
            "captured_at": f"{trade_date}T07:05:05+03:00",
            "quote_age_ms": 1000,
            "freshness_pass": True,
            "target_ahead": True,
            "evaluation_start_time": "07:10",
        },
        "gates": {
            "capture_window": True,
            "quote_freshness": True,
            "depth_coverage": True,
            "target_ahead": True,
            "control_fill_before_quote": True,
        },
        "promotion_eligible": True,
        "book_sha256": "3" * 64,
        "capture_id": "4" * 64,
    }
    value.update(updates)
    return value


def candles(*middle):
    cursor = datetime.strptime("07:05", "%H:%M")
    finish = datetime.strptime("18:35", "%H:%M")
    by_time = {}
    while cursor <= finish:
        stamp = cursor.strftime("%H:%M")
        by_time[stamp] = [stamp, 100.0, 100.4, 99.6, 100.1]
        cursor += timedelta(minutes=5)
    by_time["18:35"] = ["18:35", 100.2, 100.4, 100.0, 100.3]
    for row in middle:
        by_time[row[0]] = list(row)
    return {"candles": [by_time[key] for key in sorted(by_time)]}


def evaluated(raw=None, document=None, *, now=NOW):
    with patch.object(
        subject.registry,
        "validate_capture",
        side_effect=lambda item: copy.deepcopy(dict(item)),
        create=True,
    ):
        return subject.evaluate_capture(
            raw or capture(),
            document or candles(),
            {"kind": "offline_test"},
            now=now,
        )


def report_manifest():
    return {
        "manifest_sha256": "2" * 64,
        "production_activation_allowed": False,
        "evidence_not_before": "2026-07-20",
        "promotion_gates": {
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
        },
    }


class TimingAndSimulationTests(unittest.TestCase):
    def test_capture_bar_is_excluded_and_control_uses_its_own_fill_boundary(self) -> None:
        document = candles(
            ["07:05", 100.0, 103.0, 98.0, 100.0],
            ["07:10", 100.0, 102.1, 99.5, 101.8],
            ["18:35", 101.8, 101.9, 101.7, 101.8],
        )
        outcome = evaluated(document=document)

        self.assertEqual(outcome["control"]["evaluation_start_time"], "07:05")
        self.assertEqual(outcome["control"]["exit_reason"], "both_stop_first")
        self.assertAlmostEqual(outcome["control"]["net_return_pct"], -1.08)
        base = outcome["challenger"]["scenarios"]["0"]
        self.assertEqual(base["evaluation_start_time"], "07:10")
        self.assertEqual(base["exit_reason"], "target")
        self.assertAlmostEqual(base["net_return_pct"], 1.92)
        self.assertIn("historical research", outcome["contract"]["historical_proxy_difference"])

    def test_base_and_adverse_entry_stresses_are_recomputed_from_vwap(self) -> None:
        outcome = evaluated(document=candles(["09:00", 100.0, 102.2, 99.5, 102.0]))
        scenarios = outcome["challenger"]["scenarios"]

        self.assertEqual(sorted(scenarios), ["0", "10", "5"])
        self.assertAlmostEqual(scenarios["0"]["entry_price"], 100.0)
        self.assertAlmostEqual(scenarios["5"]["entry_price"], 100.05)
        self.assertAlmostEqual(scenarios["10"]["entry_price"], 100.1)
        self.assertGreater(
            scenarios["0"]["net_return_pct"], scenarios["5"]["net_return_pct"]
        )
        self.assertGreater(
            scenarios["5"]["net_return_pct"], scenarios["10"]["net_return_pct"]
        )

    def test_short_stress_is_adverse_and_stop_first_is_direction_correct(self) -> None:
        raw = capture(
            direction="short",
            target_price=98.0,
            challenger={
                **capture()["challenger"],
                "side": "bids",
                "best_touch": 100.1,
                "worst_price": 99.9,
            },
        )
        outcome = evaluated(
            raw,
            candles(
                ["07:05", 100.0, 100.4, 99.6, 100.0],
                ["07:10", 100.0, 101.2, 97.8, 100.0],
                ["18:35", 100.0, 100.1, 99.9, 100.0],
            ),
        )
        scenarios = outcome["challenger"]["scenarios"]

        self.assertAlmostEqual(scenarios["5"]["entry_price"], 99.95)
        self.assertEqual(scenarios["0"]["exit_reason"], "both_stop_first")
        self.assertAlmostEqual(scenarios["0"]["gross_return_pct"], -1.0)

    def test_target_crossed_only_under_stress_is_recorded_not_silently_traded(self) -> None:
        raw = capture(target_price=100.04)
        outcome = evaluated(raw)

        self.assertTrue(outcome["challenger"]["scenarios"]["0"]["evaluated"])
        self.assertFalse(outcome["challenger"]["scenarios"]["5"]["evaluated"])
        self.assertEqual(
            outcome["challenger"]["scenarios"]["5"]["exclusion_reason"],
            "frozen_absolute_target_not_ahead",
        )

    def test_next_complete_bar_always_excludes_containing_bar(self) -> None:
        self.assertEqual(
            subject.next_complete_bar("2026-07-20T07:05:00+03:00", "2026-07-20"),
            "07:10",
        )
        self.assertEqual(
            subject.next_complete_bar("2026-07-20T07:09:59+03:00", "2026-07-20"),
            "07:10",
        )


class EligibilityTests(unittest.TestCase):
    def test_stale_or_partial_depth_capture_remains_diagnostic_but_ineligible(self) -> None:
        raw = capture(
            challenger={
                **capture()["challenger"],
                "executable_vwap": None,
                "covered_lots": 5,
                "depth_sufficient": False,
                "quote_age_ms": 4000,
                "freshness_pass": False,
            },
            gates={
                **capture()["gates"],
                "quote_freshness": False,
                "depth_coverage": False,
            },
            promotion_eligible=False,
        )
        outcome = evaluated(raw)

        self.assertFalse(outcome["eligible_for_promotion_analysis"])
        self.assertIn("gate_failed:quote_freshness", outcome["ineligibility_reasons"])
        self.assertIn("insufficient_orderbook_depth", outcome["ineligibility_reasons"])
        self.assertFalse(outcome["challenger"]["scenarios"]["0"]["evaluated"])
        self.assertEqual(
            outcome["challenger"]["scenarios"]["0"]["exclusion_reason"],
            "no_full_depth_executable_vwap",
        )

    def test_candle_open_fallback_can_never_be_eligible(self) -> None:
        raw = capture(
            challenger={
                **capture()["challenger"],
                "entry_price_source": "candle_open_fallback",
            }
        )
        outcome = evaluated(raw)

        self.assertFalse(outcome["eligible_for_promotion_analysis"])
        self.assertIn("non_executable_entry_source", outcome["ineligibility_reasons"])
        self.assertFalse(outcome["execution_claim"])

    def test_execution_claim_and_wrong_start_are_rejected_even_after_registry(self) -> None:
        with patch.object(
            subject.registry,
            "validate_capture",
            side_effect=lambda item: copy.deepcopy(dict(item)),
            create=True,
        ):
            with self.assertRaisesRegex(subject.EvaluationError, "execution_claim=false"):
                subject.normalize_capture(capture(execution_claim=True), now=NOW)
            wrong = capture(
                challenger={
                    **capture()["challenger"],
                    "evaluation_start_time": "07:05",
                }
            )
            with self.assertRaisesRegex(subject.EvaluationError, "exclude"):
                subject.normalize_capture(wrong, now=NOW)

    def test_incomplete_session_is_rejected(self) -> None:
        with self.assertRaisesRegex(subject.EvaluationError, "five-minute candles"):
            evaluated(document={"candles": [["07:05", 100, 101, 99, 100], ["07:10", 100, 101, 99, 100]]})

    def test_missing_middle_bar_and_off_grid_bar_are_rejected(self) -> None:
        document = candles()
        document["candles"] = [row for row in document["candles"] if row[0] != "12:00"]
        with self.assertRaisesRegex(subject.EvaluationError, "12:00"):
            evaluated(document=document)
        with self.assertRaisesRegex(subject.EvaluationError, "five-minute boundary"):
            subject.normalize_candles(
                {"candles": [["07:07", 100, 101, 99, 100]]}, "2026-07-20"
            )


class PersistenceReportingAndCliTests(unittest.TestCase):
    def test_first_writer_wins_and_conflicting_rewrite_is_rejected(self) -> None:
        outcome = evaluated()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            path, created = subject.write_immutable_outcome(outcome, directory, ledger)
            same_path, same_created = subject.write_immutable_outcome(outcome, directory, ledger)
            self.assertTrue(created)
            self.assertFalse(same_created)
            self.assertEqual(path, same_path)
            self.assertEqual(len(ledger.read_text(encoding="utf-8").splitlines()), 1)

            conflict = copy.deepcopy(outcome)
            conflict["evaluated_at"] = "2026-07-21T12:00:01+03:00"
            body = dict(conflict)
            body.pop("outcome_id")
            conflict["outcome_id"] = subject.canonical_sha256(body)
            with self.assertRaisesRegex(subject.EvaluationError, "conflict"):
                subject.write_immutable_outcome(conflict, directory, ledger)

    def test_report_uses_only_eligible_comparable_rows_and_never_activates(self) -> None:
        eligible = evaluated(document=candles(["09:00", 100, 102.2, 99.5, 102]))
        ineligible_capture = capture(trade_date="2026-07-21")
        ineligible_capture["gates"]["quote_freshness"] = False
        ineligible_capture["challenger"]["freshness_pass"] = False
        ineligible_capture["promotion_eligible"] = False
        ineligible_capture["capture_id"] = "6" * 64
        ineligible = evaluated(
            ineligible_capture,
            now=datetime(2026, 7, 22, 12, 0, tzinfo=subject.MOSCOW),
        )
        broken = copy.deepcopy(eligible)
        broken["control"]["net_return_pct"] = 99.0
        report = subject.summarize_outcomes(
            [eligible, ineligible, broken], manifest=report_manifest()
        )

        self.assertEqual(report["accepted_outcomes"], 2)
        self.assertEqual(report["eligible_outcomes"], 1)
        self.assertEqual(report["stress"]["0"]["comparable_observations"], 1)
        self.assertEqual(len(report["rejected_outcomes"]), 1)
        self.assertTrue(report["manifest_gate_status"]["manifest_verified"])
        self.assertEqual(report["manifest_gate_status"]["eligible_outcomes"], 1)
        self.assertFalse(report["production_activation_allowed"])
        self.assertFalse(report["promotion_ready"])
        self.assertFalse(report["manual_review_ready"])
        self.assertFalse(report["statistical_gates"]["minimum_eligible_captures"])

    def test_rehashed_internal_tampering_is_still_rejected(self) -> None:
        outcome = evaluated(document=candles(["09:00", 100, 102.2, 99.5, 102]))

        bad_return = copy.deepcopy(outcome)
        bad_return["challenger"]["scenarios"]["0"]["net_return_pct"] += 1.0
        body = dict(bad_return)
        body.pop("outcome_id")
        bad_return["outcome_id"] = subject.canonical_sha256(body)
        with self.assertRaisesRegex(subject.EvaluationError, "net return"):
            subject.validate_outcome(bad_return)

        bad_gate = copy.deepcopy(outcome)
        bad_gate["evidence_gates"]["quote_freshness"] = False
        body = dict(bad_gate)
        body.pop("outcome_id")
        bad_gate["outcome_id"] = subject.canonical_sha256(body)
        with self.assertRaisesRegex(subject.EvaluationError, "ineligibility reasons"):
            subject.validate_outcome(bad_gate)

        bad_delta = copy.deepcopy(outcome)
        bad_delta["comparison"]["0"]["challenger_minus_control_pp"] += 0.5
        body = dict(bad_delta)
        body.pop("outcome_id")
        bad_delta["outcome_id"] = subject.canonical_sha256(body)
        with self.assertRaisesRegex(subject.EvaluationError, "delta"):
            subject.validate_outcome(bad_delta)

    def test_fifty_robust_pairs_raise_manual_review_only_never_promotion(self) -> None:
        outcomes = []
        first_day = date(2026, 7, 20)
        evaluation_now = datetime(2026, 10, 1, 12, 0, tzinfo=subject.MOSCOW)
        target_session = candles(["09:00", 100.0, 102.2, 99.6, 102.0])
        for index in range(50):
            trade_date = (first_day + timedelta(days=index)).isoformat()
            raw = capture(trade_date=trade_date)
            raw["control"]["actual_fill_price"] = 100.5
            raw["plan_id"] = subject.canonical_sha256(
                {"kind": "test_plan", "trade_date": trade_date}
            )
            raw["capture_id"] = subject.canonical_sha256(
                {"kind": "test_capture", "trade_date": trade_date}
            )
            outcomes.append(
                evaluated(raw, target_session, now=evaluation_now)
            )

        report = subject.summarize_outcomes(
            outcomes, manifest=report_manifest()
        )

        self.assertEqual(report["eligible_outcomes"], 50)
        self.assertTrue(all(report["statistical_gates"].values()))
        self.assertTrue(report["evidence_gate_passed"])
        self.assertTrue(report["manual_review_ready"])
        self.assertFalse(report["promotion_ready"])
        self.assertFalse(report["production_activation_allowed"])

    def test_cli_runs_offline_and_writes_immutable_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture_path = Path(directory) / "capture.json"
            candles_path = Path(directory) / "candles.json"
            capture_path.write_text(json.dumps(capture()), encoding="utf-8")
            candles_path.write_text(json.dumps(candles()), encoding="utf-8")
            output = io.StringIO()
            with patch.object(
                subject.registry,
                "validate_capture",
                side_effect=lambda item: copy.deepcopy(dict(item)),
                create=True,
            ), patch.object(
                subject.registry, "load_manifest", return_value=report_manifest()
            ), patch.object(subject, "_now_moscow", return_value=NOW), contextlib.redirect_stdout(output):
                code = subject.main(
                    [
                        "--captures",
                        str(capture_path),
                        "--candles-file",
                        str(candles_path),
                        "--outcomes-dir",
                        directory,
                    ]
                )
            result = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(result["evaluated"], 1)
        self.assertTrue(result["outcomes"][0]["created"])
        self.assertFalse(result["report"]["production_activation_allowed"])

    def test_module_has_no_live_bot_or_order_dependency(self) -> None:
        source = (ROOT / "argonus/shadow/evaluate_delayed_entry_shadow.py").read_text(encoding="utf-8")
        self.assertNotIn("import trade_bot", source)
        self.assertNotIn("post_order", source)
        self.assertNotIn("cancel_order", source)


if __name__ == "__main__":
    unittest.main()
