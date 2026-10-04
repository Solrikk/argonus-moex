#!/usr/bin/env python3
"""Network/broker-free tests for pre-registered forward shadow research."""
from __future__ import annotations

import ast
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

from argonus.shadow import forward_shadow_research as subject


from argonus.paths import PROJECT_ROOT as ROOT


def candidate(
    symbol: str,
    vol: float | None,
    reason: str | None = None,
    *,
    rs5: float | None = 0.0,
) -> dict:
    return {
        "symbol": symbol,
        "direction": "short",
        "vol_expansion": vol,
        "overall_score": 42.0,
        "directional_rs5_pp": rs5,
        "target_label": "T4",
        "target_price": 95.0,
        "target_move_pct": 5.0,
        "regime": "level_rejection",
        "short_rally_10d_pct": -2.0,
        "runner_day_prob": 0.6,
        "eligibility_reason": reason,
    }


def outcome_record(
    index: int,
    *,
    policy: str,
    manifest: dict,
    control: float,
    challenger: float,
    divergence: bool = False,
    eligible: bool = True,
) -> dict:
    trade_date = (date(2026, 7, 20) + timedelta(days=index)).isoformat()
    evaluator_hash = manifest["artifacts"]["evaluate_forward_shadows.py"]
    control_result = {"net_return_pct": control}
    challenger_result = {"net_return_pct": challenger}
    common = {
        "schema_version": 1,
        "trade_date": trade_date,
        "capture_mode": "forward",
        "policy": policy,
        "record_type": (
            "exit_shadow_outcome"
            if policy == subject.EXIT_POLICY
            else "selector_shadow_outcome"
        ),
        "shadow_only": True,
        "production_effect": "none",
        "manifest_sha256": manifest["manifest_sha256"],
        "evaluator_sha256": evaluator_hash,
        "control": control_result,
        "challenger": challenger_result,
        "comparison": {"challenger_minus_control_pp": challenger - control},
    }
    if policy == subject.EXIT_POLICY:
        decision_id = subject.canonical_sha256(["exit", trade_date])
        record = {
            **common,
            "plan_id": decision_id,
            "decision_id": decision_id,
            "symbol": "TEST",
            "direction": "long",
            "plan_sha256": subject.canonical_sha256(["plan", trade_date]),
            "candles_sha256": subject.canonical_sha256(["candles", trade_date]),
            "evaluation_start_time": "07:05",
            "actual_round_trip_cost_pct": None,
            "entry_price": 100.0,
            "entry_price_source": "actual_frozen_fill",
            "entry_bar_excluded": True,
        }
        identity = {
            "schema_version": 1,
            "record_type": record["record_type"],
            "policy": policy,
            "manifest_sha256": record["manifest_sha256"],
            "trade_date": trade_date,
            "symbol": "TEST",
            "direction": "long",
            "plan_id": record["plan_id"],
            "decision_id": record["decision_id"],
            "plan_sha256": record["plan_sha256"],
            "candles_sha256": record["candles_sha256"],
            "evaluator_sha256": evaluator_hash,
            "evaluation_start_time": "07:05",
            "entry_price": 100.0,
            "entry_price_source": "actual_frozen_fill",
            "entry_bar_excluded": True,
            "actual_round_trip_cost_pct": None,
            "control": control_result,
            "challenger": challenger_result,
        }
        record["provenance"] = {
            field: record[field]
            for field in (
                "decision_id",
                "plan_id",
                "plan_sha256",
                "candles_sha256",
                "manifest_sha256",
                "evaluator_sha256",
            )
        }
    else:
        decision_id = subject.canonical_sha256(["selector", trade_date])
        control_symbol = "CONTROL"
        recommended_symbol = "CHALLENGER" if divergence else control_symbol
        execution = {"valid": bool(eligible), "reasons": [] if eligible else ["test"]}
        record = {
            **common,
            "decision_id": decision_id,
            "decision_sha256": subject.canonical_sha256(["decision", trade_date]),
            "control_candles_sha256": subject.canonical_sha256(["control", trade_date]),
            "challenger_candles_sha256": subject.canonical_sha256(["challenger", trade_date]),
            "control_selected_symbol": control_symbol,
            "recommended_symbol": recommended_symbol,
            "eligible_session": bool(eligible),
            "divergence": bool(divergence),
            "selector_execution_evidence": execution,
            "promotion_eligible": bool(eligible),
        }
        identity = {
            "schema_version": 1,
            "record_type": record["record_type"],
            "policy": policy,
            "manifest_sha256": record["manifest_sha256"],
            "trade_date": trade_date,
            "decision_id": record["decision_id"],
            "decision_sha256": record["decision_sha256"],
            "evaluator_sha256": evaluator_hash,
            "control_candles_sha256": record["control_candles_sha256"],
            "challenger_candles_sha256": record["challenger_candles_sha256"],
            "control_selected_symbol": control_symbol,
            "recommended_symbol": recommended_symbol,
            "eligible_session": bool(eligible),
            "divergence": bool(divergence),
            "selector_execution_evidence": execution,
            "promotion_eligible": bool(eligible),
            "control": control_result,
            "challenger": challenger_result,
        }
        record["provenance"] = {
            field: record[field]
            for field in (
                "decision_id",
                "decision_sha256",
                "manifest_sha256",
                "evaluator_sha256",
                "control_candles_sha256",
                "challenger_candles_sha256",
            )
        }
    record["outcome_identity"] = identity
    record["outcome_id"] = subject.canonical_sha256(identity)
    return record


class ManifestAndIsolationTests(unittest.TestCase):
    def test_checked_manifest_loads_and_forbids_production(self) -> None:
        manifest = subject.load_manifest()
        self.assertEqual(manifest["mode"], "shadow_only")
        self.assertFalse(manifest["production_activation_allowed"])
        self.assertEqual(manifest["policies"]["exit"]["name"], subject.EXIT_POLICY)
        self.assertEqual(
            manifest["policies"]["selector"]["name"], subject.SELECTOR_POLICY
        )

    def test_tampered_module_hash_is_rejected(self) -> None:
        manifest = json.loads(
            (ROOT / "config/forward_shadow_manifest.json").read_text(encoding="utf-8")
        )
        manifest["artifacts"]["forward_shadow_research.py"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "module hash mismatch"):
                subject.load_manifest(path)

    def test_caller_supplied_manifest_mutation_cannot_bypass_validation(self) -> None:
        manifest = subject.load_manifest()
        manifest["evidence_not_before"] = "2025-01-01"
        with self.assertRaisesRegex(RuntimeError, "manifest_sha256"):
            subject.build_exit_shadow_plan(
                trade_date="2026-07-20",
                symbol="TEST",
                direction="long",
                entry_price=100.0,
                actual_entry_timestamp="2026-07-20T07:02:00+03:00",
                target_price=103.0,
                manifest=manifest,
            )

    def test_module_has_no_broker_network_or_production_imports(self) -> None:
        path = ROOT / "argonus/shadow/forward_shadow_research.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {
            "trade_bot",
            "tbank_market_data",
            "requests",
            "urllib",
            "httpx",
            "aiohttp",
            "socket",
            "subprocess",
        }
        self.assertTrue(forbidden.isdisjoint(imported), imported & forbidden)


class ExitPlanAndSimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = subject.load_manifest()

    def make_plan(self, **overrides) -> dict:
        values = {
            "trade_date": "2026-07-20",
            "symbol": "TEST",
            "direction": "long",
            "entry_price": 100.0,
            "actual_entry_timestamp": "2026-07-20T07:02:00+03:00",
            "target_price": 104.0,
            "created_at": "2026-07-20T06:59:00+03:00",
            "manifest": self.manifest,
        }
        values.update(overrides)
        return subject.build_exit_shadow_plan(**values)

    def test_plan_schema_matches_standalone_evaluator_contract(self) -> None:
        plan = self.make_plan(optional_evidence={"spread": None})
        self.assertEqual(plan["record_type"], "exit_shadow_plan")
        self.assertEqual(plan["policy"], subject.EXIT_POLICY)
        self.assertEqual(plan["control"]["stop_pct"], 1.0)
        self.assertEqual(plan["control"]["time_exit"], "18:35")
        self.assertEqual(plan["challenger"]["stop_pct"], 1.25)
        self.assertEqual(plan["challenger"]["target_multiple"], 1.25)
        self.assertEqual(plan["challenger"]["time_exit"], "17:00")
        self.assertEqual(plan["plan_id"], plan["decision_id"])
        self.assertEqual(len(plan["plan_id"]), 64)
        self.assertTrue(plan["shadow_only"])
        self.assertEqual(plan["production_effect"], "none")

    def test_decision_id_excludes_wall_clock_but_commits_optional_evidence(self) -> None:
        first = self.make_plan(
            created_at="2026-07-20T06:59:00+03:00",
            optional_evidence={"spread": 0.01},
        )
        second = self.make_plan(
            created_at="2026-07-20T07:00:00+03:00",
            optional_evidence={"spread": 0.01},
        )
        self.assertEqual(first["decision_id"], second["decision_id"])
        changed = self.make_plan(optional_evidence={"spread": 0.02})
        self.assertNotEqual(first["decision_id"], changed["decision_id"])

    def test_plan_before_registered_evidence_window_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "precedes registered evidence window"):
            self.make_plan(trade_date="2026-07-19")

    def test_challenger_uses_1_25x_target_distance_and_17_time_exit(self) -> None:
        plan = self.make_plan()
        candles = [
            ["07:00", 100.0, 100.5, 99.8, 100.2],
            ["16:55", 100.2, 104.5, 100.0, 104.0],
            ["17:00", 104.0, 104.8, 103.8, 104.5],
            ["18:35", 104.5, 105.1, 104.4, 105.0],
        ]
        result = subject.evaluate_exit_shadow(plan, candles)
        self.assertEqual(result["control"]["exit_reason"], "target")
        self.assertAlmostEqual(result["control"]["target_price"], 104.0)
        self.assertEqual(result["challenger"]["exit_reason"], "time_exit")
        self.assertAlmostEqual(result["challenger"]["target_price"], 105.0)
        self.assertEqual(result["challenger"]["exit_time"], "17:00")
        self.assertAlmostEqual(result["entry_price"], 100.0)
        self.assertEqual(result["entry_price_source"], "actual_frozen_fill")
        self.assertEqual(len(result["plan_sha256"]), 64)
        self.assertEqual(len(result["candles_sha256"]), 64)

    def test_stop_wins_ambiguous_bar(self) -> None:
        result = subject.simulate_exit_policy(
            direction="long",
            entry_price=100.0,
            frozen_target_price=104.0,
            candles=[["07:00", 100.0, 106.0, 98.0, 101.0]],
            stop_pct=1.25,
            target_multiple=1.25,
            time_exit="17:00",
        )
        self.assertEqual(result["exit_reason"], "both_stop_first")
        self.assertTrue(result["ambiguous"])
        self.assertAlmostEqual(result["gross_return_pct"], -1.25)

    def test_actual_plan_fill_is_authoritative_over_candle_open(self) -> None:
        plan = self.make_plan(entry_price=99.0)
        result = subject.evaluate_exit_shadow(
            plan,
            [["07:00", 100.0, 100.2, 99.8, 100.0], ["17:00", 100.0, 100.1, 99.9, 100.0]],
        )
        self.assertEqual(result["entry_price"], 99.0)
        self.assertEqual(result["entry_price_source"], "actual_frozen_fill")
        self.assertEqual(result["first_candle_open"], 100.0)


class SelectorPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = subject.load_manifest()

    def test_actual_rs5_control_is_distinct_from_legacy_rank1(self) -> None:
        rows = [candidate("LEGACY", 9.0), candidate("CONTROL", 2.0), candidate("ALT", 5.0)]
        original = deepcopy(rows)
        report = subject.evaluate_max_vol_expansion_top6(
            rows,
            trade_date="2026-07-20",
            eligibility_reasons={1: None, 2: None, 3: None},
            control_selected_symbol="CONTROL",
            legacy_rank1_symbol="LEGACY",
            created_at="2026-07-20T06:50:00+03:00",
            manifest=self.manifest,
        )
        self.assertEqual(report["legacy_rank1_symbol"], "LEGACY")
        self.assertEqual(report["control_selected_symbol"], "CONTROL")
        self.assertEqual(report["recommended_symbol"], "LEGACY")
        self.assertTrue(report["divergence"])
        self.assertEqual(report["live_selected_symbol"], "CONTROL")
        self.assertEqual(rows, original, "caller candidate rows were mutated")

    def test_keep_is_relative_to_actual_control_not_legacy_rank1(self) -> None:
        report = subject.evaluate_max_vol_expansion_top6(
            [candidate("LEGACY", 1.0), candidate("CONTROL", 3.0)],
            trade_date="2026-07-20",
            eligibility_reasons={1: None, 2: None},
            control_selected_symbol="CONTROL",
            legacy_rank1_symbol="LEGACY",
            manifest=self.manifest,
        )
        self.assertEqual(report["decision"], "keep")
        self.assertFalse(report["divergence"])
        self.assertEqual(report["recommended_symbol"], "CONTROL")

    def test_tie_preserves_existing_rank(self) -> None:
        report = subject.evaluate_max_vol_expansion_top6(
            [candidate("A", 3.0), candidate("B", 3.0)],
            trade_date="2026-07-20",
            eligibility_reasons={1: None, 2: None},
            control_selected_symbol="B",
            manifest=self.manifest,
        )
        self.assertEqual(report["recommended_symbol"], "A")
        self.assertTrue(report["divergence"])

    def test_missing_control_feature_fails_open_even_if_alternative_is_finite(self) -> None:
        report = subject.evaluate_max_vol_expansion_top6(
            [candidate("CONTROL", None), candidate("ALT", 99.0)],
            trade_date="2026-07-20",
            eligibility_reasons={1: None, 2: None},
            control_selected_symbol="CONTROL",
            manifest=self.manifest,
        )
        self.assertEqual(report["decision"], "missing_baseline_feature_fail_open")
        self.assertFalse(report["divergence"])
        self.assertEqual(report["live_selected_symbol"], "CONTROL")

    def test_absent_actual_control_fails_open(self) -> None:
        report = subject.evaluate_max_vol_expansion_top6(
            [candidate("A", 1.0), candidate("B", 2.0)],
            trade_date="2026-07-20",
            eligibility_reasons={1: None, 2: None},
            control_selected_symbol="MISSING",
            manifest=self.manifest,
        )
        self.assertEqual(report["decision"], "control_not_in_top6_fail_open")
        self.assertFalse(report["divergence"])
        self.assertEqual(report["control_selected_symbol"], "MISSING")

    def test_optional_evidence_is_passive_data(self) -> None:
        report = subject.evaluate_max_vol_expansion_top6(
            [candidate("A", 1.0), candidate("B", 2.0)],
            trade_date="2026-07-20",
            eligibility_reasons={1: None, 2: None},
            control_selected_symbol="A",
            manifest=self.manifest,
            optional_evidence={"collection_stage": "after_stop_protection"},
        )
        self.assertEqual(
            report["optional_evidence"]["collection_stage"], "after_stop_protection"
        )
        self.assertIn("protected live entry", report["evidence_collection_constraint"])

    def test_rs5_floor_is_admissibility_guard_and_audit_fields_are_preserved(self) -> None:
        report = subject.evaluate_max_vol_expansion_top6(
            [candidate("CONTROL", 2.0), candidate("HIGHVOL_WEAKRS", 99.0, rs5=-2.40)],
            trade_date="2026-07-20",
            eligibility_reasons={1: None, 2: None},
            control_selected_symbol="CONTROL",
            manifest=self.manifest,
        )
        self.assertEqual(report["recommended_symbol"], "CONTROL")
        self.assertEqual(report["directional_rs5_floor_pp"], -2.39)
        weak = report["candidates"][1]
        self.assertFalse(weak["passes_directional_rs5_floor"])
        for field in (
            "overall_score",
            "directional_rs5_pp",
            "target_label",
            "target_price",
            "target_move_pct",
            "regime",
            "short_rally_10d_pct",
            "runner_day_prob",
            "skipped_reason",
        ):
            self.assertIn(field, weak)


class SelectorOutcomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = subject.load_manifest()

    def decision(self, *, with_execution: bool) -> dict:
        evidence = {}
        if with_execution:
            evidence["selector_execution_evidence"] = {
                "captured_at": "2026-07-20T06:59:00+03:00",
                "captured_at_or_before_decision": True,
                "point_in_time": True,
                "control_executable": True,
                "challenger_executable": True,
                "spread_depth_captured": True,
                "max_lots_captured": True,
                "control": {
                    "symbol": "CONTROL",
                    "entry_price": 100.0,
                    "entry_timestamp": "2026-07-20T06:59:00+03:00",
                },
                "challenger": {
                    "symbol": "ALT",
                    "entry_price": 100.0,
                    "entry_timestamp": "2026-07-20T06:59:00+03:00",
                },
            }
        return subject.evaluate_max_vol_expansion_top6(
            [candidate("CONTROL", 1.0), candidate("ALT", 3.0)],
            trade_date="2026-07-20",
            control_selected_symbol="CONTROL",
            eligibility_reasons={1: None, 2: None},
            created_at="2026-07-20T07:00:00+03:00",
            manifest=self.manifest,
            optional_evidence=evidence,
        )

    @staticmethod
    def candles(close: float) -> list[list[float | str]]:
        return [
            ["07:00", 100.0, 100.2, 99.8, 100.0],
            ["17:00", 100.0, 100.2, close - 0.2, close],
            ["18:35", close, close + 0.2, close - 0.2, close],
        ]

    def test_causal_executable_pair_produces_valid_selector_outcome(self) -> None:
        outcome = subject.evaluate_selector_shadow(
            self.decision(with_execution=True),
            self.candles(99.0),
            self.candles(98.0),
            manifest=self.manifest,
        )
        self.assertTrue(outcome["promotion_eligible"])
        self.assertTrue(outcome["selector_execution_evidence"]["valid"])
        self.assertTrue(outcome["divergence"])
        self.assertEqual(
            subject.validate_shadow_outcome(
                outcome, policy=subject.SELECTOR_POLICY, manifest=self.manifest
            )["outcome_id"],
            outcome["outcome_id"],
        )

    def test_missing_challenger_execution_is_diagnostic_only(self) -> None:
        outcome = subject.evaluate_selector_shadow(
            self.decision(with_execution=False),
            self.candles(99.0),
            self.candles(98.0),
            manifest=self.manifest,
        )
        self.assertFalse(outcome["promotion_eligible"])
        self.assertEqual(
            outcome["control"]["entry_price_source"],
            "diagnostic_first_candle_open",
        )

    def test_selector_decision_mutation_breaks_immutable_id(self) -> None:
        frozen = self.decision(with_execution=True)
        frozen["recommended_symbol"] = "CONTROL"
        with self.assertRaisesRegex(ValueError, "registered evaluation"):
            subject.validate_selector_shadow_decision(frozen, manifest=self.manifest)


class FailOpenAndPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = subject.load_manifest()

    def test_fail_open_always_returns_original_baseline(self) -> None:
        baseline = {"symbol": "LIVE"}

        def explode():
            raise OSError("disk unavailable")

        selected, audit = subject.fail_open(baseline, explode)
        self.assertEqual(selected, baseline)
        self.assertIsNot(selected, baseline)
        self.assertTrue(audit["fail_open_applied"])
        self.assertEqual(audit["production_effect"], "none")

    def test_fail_open_returns_pre_operation_snapshot_after_mutation(self) -> None:
        baseline = {"symbols": ["LIVE"]}

        def mutate_then_fail():
            baseline["symbols"].append("SHADOW")
            raise RuntimeError("failure after mutation")

        selected, audit = subject.fail_open(baseline, mutate_then_fail)
        self.assertEqual(selected, {"symbols": ["LIVE"]})
        self.assertEqual(baseline, {"symbols": ["LIVE", "SHADOW"]})
        self.assertTrue(audit["fail_open_applied"])

    def test_first_writer_wins_and_temp_is_removed(self) -> None:
        today = date(2026, 7, 20)
        base = subject.evaluate_max_vol_expansion_top6(
            [candidate("A", 1.0), candidate("B", 2.0)],
            trade_date=today,
            control_selected_symbol="A",
            eligibility_reasons={1: None, 2: None},
            created_at="2026-07-20T06:50:00+03:00",
            manifest=self.manifest,
        )
        conflict = subject.evaluate_max_vol_expansion_top6(
            [candidate("A", 2.0), candidate("B", 1.0)],
            trade_date=today,
            control_selected_symbol="A",
            eligibility_reasons={1: None, 2: None},
            created_at="2026-07-20T06:50:00+03:00",
            manifest=self.manifest,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = subject.write_first_writer_wins(
                base, directory, current_date=today
            )
            subject.write_first_writer_wins(
                base, directory, current_date=today
            )
            with self.assertRaisesRegex(RuntimeError, "identity conflict"):
                subject.write_first_writer_wins(
                    conflict, directory, current_date=today
                )
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["decision_id"], base["decision_id"])
            self.assertEqual(list(Path(directory).rglob("*.tmp")), [])
            self.assertEqual(
                path,
                Path(directory)
                / subject.SELECTOR_POLICY
                / "selector_shadow_decision"
                / "2026-07-20.json",
            )

    def test_historical_or_future_date_is_rejected_without_file(self) -> None:
        report = {
            "trade_date": "2026-07-19",
            "capture_mode": "forward",
            "shadow_only": True,
            "policy": subject.SELECTOR_POLICY,
            "record_type": "selector_shadow_decision",
            "decision_id": "a" * 64,
            "manifest_sha256": self.manifest["manifest_sha256"],
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "trade_date == current_date"):
                subject.write_first_writer_wins(
                    report, directory, current_date=date(2026, 7, 20)
                )
            self.assertEqual(list(Path(directory).rglob("*")), [])


class PromotionGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = subject.load_manifest()

    def test_exit_requires_50_new_forward_trades_and_all_robustness_gates(self) -> None:
        records = [
            outcome_record(
                index,
                policy=subject.EXIT_POLICY,
                manifest=self.manifest,
                control=0.0,
                challenger=0.10,
            )
            for index in range(50)
        ]
        passed = subject.promotion_metrics(
            records, policy=subject.EXIT_POLICY, manifest=self.manifest
        )
        self.assertTrue(passed["promotion_ready"])
        self.assertEqual(passed["eligible_sessions_or_trades"], 50)
        self.assertLessEqual(passed["max_positive_contributor_share_pct"], 35.0)
        self.assertLessEqual(
            passed["holm_adjusted_p_single_registered_challenger"], 0.10
        )
        self.assertEqual(passed["concentration_basis"], "positive paired log-return deltas")

        failed = subject.promotion_metrics(
            records[:49], policy=subject.EXIT_POLICY, manifest=self.manifest
        )
        self.assertFalse(failed["promotion_ready"])
        self.assertFalse(failed["gates"]["minimum_sample"])

    def test_selector_requires_40_sessions_and_10_actual_control_divergences(self) -> None:
        records = []
        for index in range(40):
            divergence = index % 4 == 0
            records.append(
                outcome_record(
                    index,
                    policy=subject.SELECTOR_POLICY,
                    manifest=self.manifest,
                    control=0.0,
                    challenger=0.10 if divergence else 0.0,
                    divergence=divergence,
                )
            )
        report = subject.promotion_metrics(
            records, policy=subject.SELECTOR_POLICY, manifest=self.manifest
        )
        self.assertEqual(report["eligible_sessions_or_trades"], 40)
        self.assertEqual(report["divergences"], 10)
        self.assertTrue(report["promotion_ready"])

        records = [
            outcome_record(
                index,
                policy=subject.SELECTOR_POLICY,
                manifest=self.manifest,
                control=0.0,
                challenger=0.10 if index < 9 else 0.0,
                divergence=index < 9,
            )
            for index in range(40)
        ]
        report = subject.promotion_metrics(
            records, policy=subject.SELECTOR_POLICY, manifest=self.manifest
        )
        self.assertFalse(report["gates"]["minimum_divergences"])

    def test_both_chronological_halves_and_concentration_are_real_gates(self) -> None:
        first_only = [
            outcome_record(
                index,
                policy=subject.EXIT_POLICY,
                manifest=self.manifest,
                control=0.0,
                challenger=0.1 if index < 25 else 0.0,
            )
            for index in range(50)
        ]
        report = subject.promotion_metrics(
            first_only, policy=subject.EXIT_POLICY, manifest=self.manifest
        )
        self.assertFalse(report["gates"]["positive_delta_second_chronological_half"])
        self.assertFalse(report["promotion_ready"])

        concentrated = [
            outcome_record(
                index,
                policy=subject.EXIT_POLICY,
                manifest=self.manifest,
                control=0.0,
                challenger=(1.0 if index == 0 else 0.01),
            )
            for index in range(50)
        ]
        report = subject.promotion_metrics(
            concentrated, policy=subject.EXIT_POLICY, manifest=self.manifest
        )
        self.assertGreater(report["max_positive_contributor_share_pct"], 35.0)
        self.assertFalse(
            report["gates"]["max_positive_contributor_share_at_most_35pct"]
        )

    def test_historical_wrong_manifest_and_duplicate_dates_are_rejected(self) -> None:
        good = outcome_record(
            0,
            policy=subject.EXIT_POLICY,
            manifest=self.manifest,
            control=0.0,
            challenger=0.1,
        )
        historical = {**good, "trade_date": "2026-07-21", "capture_mode": "historical"}
        wrong = {**good, "trade_date": "2026-07-22", "manifest_sha256": "bad"}
        duplicate = dict(good)
        report = subject.promotion_metrics(
            [good, historical, wrong, duplicate],
            policy=subject.EXIT_POLICY,
            manifest=self.manifest,
        )
        self.assertEqual(report["accepted_records"], 1)
        self.assertEqual(
            {row["reason"] for row in report["rejected_records"]},
            {"not_forward", "wrong_manifest", "duplicate_date"},
        )

    def test_return_or_identity_tampering_is_not_accepted(self) -> None:
        good = outcome_record(
            0,
            policy=subject.EXIT_POLICY,
            manifest=self.manifest,
            control=0.0,
            challenger=0.1,
        )
        tampered = deepcopy(good)
        tampered["challenger"]["net_return_pct"] = 99.0
        report = subject.promotion_metrics(
            [tampered], policy=subject.EXIT_POLICY, manifest=self.manifest
        )
        self.assertEqual(report["accepted_records"], 0)
        self.assertEqual(report["rejected_records"][0]["reason"], "invalid_outcome")

    def test_selector_without_causal_execution_evidence_is_diagnostic_only(self) -> None:
        row = outcome_record(
            0,
            policy=subject.SELECTOR_POLICY,
            manifest=self.manifest,
            control=0.0,
            challenger=0.1,
            divergence=True,
            eligible=False,
        )
        report = subject.promotion_metrics(
            [row], policy=subject.SELECTOR_POLICY, manifest=self.manifest
        )
        self.assertEqual(report["accepted_records"], 1)
        self.assertEqual(report["diagnostic_only_records"], 1)
        self.assertEqual(report["eligible_sessions_or_trades"], 0)
        self.assertFalse(report["gates"]["causal_point_in_time_executability"])


if __name__ == "__main__":
    unittest.main()
