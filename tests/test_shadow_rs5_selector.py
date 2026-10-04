#!/usr/bin/env python3
"""Network-free tests for the RS5 shadow/optional selection path."""
from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from argonus.watchlists import shadow_rs5_selector as selector
from argonus.trading import trade_bot


@dataclass(frozen=True)
class FakeSession:
    trade_date: date
    close: float


def sessions(closes: list[float], start: date = date(2026, 7, 1)) -> list[FakeSession]:
    return [FakeSession(start + timedelta(days=index), close) for index, close in enumerate(closes)]


def candidate(symbol: str, direction: str = "long", move: float = 3.0):
    return SimpleNamespace(
        symbol=symbol,
        direction=direction,
        overall_score=50.0,
        skipped_reason=None,
        market_bullish=direction == "long",
        runner_day_prob=None,
        directional_rs5_pp=None,
        rs5_stock_return_5d_pct=None,
        rs5_index_return_5d_pct=None,
        rs5_stock_window=None,
        rs5_index_window=None,
        short_rally_10d_pct=None,
        exit_target=SimpleNamespace(
            label="T4", price=100.0, move_pct_from_close=move
        ),
    )


def attach_precomputed_rs5(item, value: float, trade_date: date):
    item.directional_rs5_pp = value
    item.rs5_stock_return_5d_pct = value if item.direction == "long" else -value
    item.rs5_index_return_5d_pct = 0.0
    window = (
        (trade_date - timedelta(days=6)).isoformat(),
        (trade_date - timedelta(days=1)).isoformat(),
    )
    item.rs5_stock_window = window
    item.rs5_index_window = window
    return item


class RS5MathTests(unittest.TestCase):
    def test_trade_day_and_future_sessions_are_strictly_excluded(self) -> None:
        trade_date = date(2026, 7, 10)
        stock = sessions([100, 100, 100, 100, 100, 110])
        stock.extend(
            [
                FakeSession(trade_date, 1_000.0),
                FakeSession(trade_date + timedelta(days=1), 2_000.0),
            ]
        )
        index = sessions([100, 100, 100, 100, 100, 100])
        index.append(FakeSession(trade_date, 1.0))

        result = selector.calculate_directional_rs5("long", stock, index, trade_date)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["stock_return_5d_pct"], 10.0)
        self.assertAlmostEqual(result["index_return_5d_pct"], 0.0)
        self.assertAlmostEqual(result["aligned_rs5_pp"], 10.0)
        self.assertLess(date.fromisoformat(result["stock_window"][1]), trade_date)
        self.assertLess(date.fromisoformat(result["index_window"][1]), trade_date)

    def test_short_sign_is_inverse_of_long(self) -> None:
        trade_date = date(2026, 7, 10)
        stock = sessions([100, 100, 100, 100, 100, 110])
        index = sessions([100, 100, 100, 100, 100, 100])
        long_result = selector.calculate_directional_rs5("long", stock, index, trade_date)
        short_result = selector.calculate_directional_rs5("short", stock, index, trade_date)
        self.assertAlmostEqual(long_result["aligned_rs5_pp"], 10.0)
        self.assertAlmostEqual(short_result["aligned_rs5_pp"], -10.0)


class AnalyzerPrecomputeTests(unittest.TestCase):
    def test_analyze_watchlist_attaches_rs5_from_its_existing_fetches(self) -> None:
        trade_date = date.today()
        start = trade_date - timedelta(days=30)
        stock = [
            trade_bot.wbt.SessionData(
                trade_date=start + timedelta(days=index),
                open=100.0 + index,
                low=99.0 + index,
                high=101.0 + index,
                close=100.0 + index,
                volume=1_000_000.0,
            )
            for index in range(30)
        ]
        index = [
            trade_bot.wbt.SessionData(
                trade_date=start + timedelta(days=index),
                open=100.0,
                low=99.0,
                high=101.0,
                close=100.0,
                volume=1_000_000.0,
            )
            for index in range(30)
        ]
        close = stock[-1].close
        text = f"""\
Кандидаты в лонг

$TEST
Close {close}

Цель 1 - {close + 1.0}
Цель 2 - {close + 2.0}
Цель 3 - {close + 3.0}
Цель 4 - {close + 4.0}
В среднем за день проходит 4.0р. (3.0%)
Поддержки {close - 2.0}р.
"""
        with patch.object(
            trade_bot.wbt, "fetch_index_sessions", return_value=index
        ), patch.object(
            trade_bot.wbt, "fetch_stock_sessions", return_value=stock
        ):
            analyses = trade_bot.wbt.analyze_watchlist(text, trade_date)

        self.assertEqual(len(analyses), 1)
        item = analyses[0]
        expected = (stock[-1].close / stock[-6].close - 1.0) * 100.0
        self.assertAlmostEqual(item.directional_rs5_pp, expected)
        self.assertEqual(item.rs5_stock_window[1], stock[-1].trade_date.isoformat())
        self.assertLess(date.fromisoformat(item.rs5_stock_window[1]), trade_date)
        self.assertIsNotNone(item.short_rally_10d_pct)


class RS5PolicyTests(unittest.TestCase):
    trade_date = date(2026, 7, 10)
    index = sessions([100, 100, 100, 100, 100, 100])

    def evaluate(self, reasons=None):
        analyses = [candidate("WEAK"), candidate("MID"), candidate("STRONG")]
        stocks = {
            1: sessions([100, 100, 100, 100, 100, 95]),
            2: sessions([100, 100, 100, 100, 100, 96]),
            3: sessions([100, 100, 100, 100, 100, 102]),
        }
        return selector.evaluate_rs5_policy(
            analyses,
            self.trade_date,
            stocks,
            self.index,
            reasons or {1: None, 2: None, 3: None},
            threshold_pp=-2.39,
        )

    def test_weak_leader_is_replaced_by_first_passing_top3_candidate(self) -> None:
        report = self.evaluate()
        self.assertEqual(report["decision"], "replacement")
        self.assertEqual(report["recommended_rank"], 3)
        self.assertEqual(report["recommended_symbol"], "STRONG")
        self.assertTrue(report["candidates"][2]["passes_rs5"])
        self.assertEqual(report["candidates"][2]["target_price"], 100.0)

    def test_no_passing_candidate_produces_skip(self) -> None:
        analyses = [candidate("A"), candidate("B"), candidate("C")]
        weak = sessions([100, 100, 100, 100, 100, 95])
        report = selector.evaluate_rs5_policy(
            analyses,
            self.trade_date,
            {1: weak, 2: weak, 3: weak},
            self.index,
            {1: None, 2: None, 3: None},
            threshold_pp=-2.39,
        )
        self.assertEqual(report["decision"], "skip")
        self.assertIsNone(report["recommended_symbol"])

    def test_ineligible_current_leader_does_not_broaden_the_day_set(self) -> None:
        report = self.evaluate({1: "regime gate", 2: None, 3: None})
        self.assertEqual(report["decision"], "not_applicable_current_ineligible")
        self.assertEqual(report["recommended_symbol"], "WEAK")

    def test_missing_current_history_fails_open(self) -> None:
        analyses = [candidate("NEW"), candidate("STRONG")]
        report = selector.evaluate_rs5_policy(
            analyses,
            self.trade_date,
            {1: sessions([100, 101]), 2: sessions([100, 100, 100, 100, 100, 102])},
            self.index,
            {1: None, 2: None},
        )
        self.assertEqual(report["decision"], "insufficient_data_fail_open")
        self.assertEqual(report["recommended_symbol"], "NEW")

    def test_report_replace_is_atomic_and_leaves_no_temp_file(self) -> None:
        report = self.evaluate()
        with tempfile.TemporaryDirectory() as directory:
            path = selector.write_atomic_report(report, directory)
            self.assertEqual(
                path,
                Path(directory)
                / "archive"
                / "historical_reconstruction"
                / "2026-07-10.json",
            )
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["recommended_symbol"], "STRONG")
            self.assertEqual(stored["capture_mode"], "historical_reconstruction")
            self.assertEqual(list(Path(directory).rglob("*.tmp")), [])

    def test_future_audit_date_is_rejected_before_creating_files(self) -> None:
        report = self.evaluate()
        report["trade_date"] = (date.today() + timedelta(days=1)).isoformat()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "future trade_date"):
                selector.write_atomic_report(report, directory)
            self.assertEqual(list(Path(directory).rglob("*")), [])

    def test_first_forward_decision_is_immutable(self) -> None:
        today = date.today().isoformat()
        first = {"trade_date": today, "decision": "keep", "decision_id": "first"}
        second = {"trade_date": today, "decision": "replacement", "decision_id": "second"}
        with tempfile.TemporaryDirectory() as directory:
            path = selector.write_atomic_report(first, directory)
            selector.write_atomic_report(second, directory)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["decision_id"], "first")
            self.assertEqual(stored["decision"], "keep")
            self.assertEqual(list(Path(directory).rglob("*.tmp")), [])


class TradeBotShadowIntegrationTests(unittest.TestCase):
    def test_shadow_mode_records_replacement_but_keeps_live_leader(self) -> None:
        trade_date = date.today()
        analyses = [
            attach_precomputed_rs5(candidate("WEAK"), -5.0, trade_date),
            attach_precomputed_rs5(candidate("STRONG"), 2.0, trade_date),
        ]
        with tempfile.TemporaryDirectory() as directory, (
            patch.object(
                trade_bot.wbt,
                "fetch_index_sessions",
                side_effect=AssertionError("shadow must not fetch index data"),
            )
        ), patch.object(
            trade_bot.wbt,
            "fetch_stock_sessions",
            side_effect=AssertionError("shadow must not fetch stock data"),
        ):
            watchlist_text = "raw watchlist\n$WEAK\n$STRONG\n"
            report = trade_bot.run_rs5_shadow_selector(
                analyses,
                trade_date,
                selector_enabled=False,
                threshold_pp=-2.39,
                output_dir=directory,
                watchlist_text=watchlist_text,
            )

            self.assertEqual(report["decision"], "replacement")
            self.assertEqual(report["mode"], "shadow")
            self.assertFalse(report["activation_applied"])
            self.assertEqual(report["live_selected_symbol"], "WEAK")
            self.assertEqual(report["capture_mode"], "forward")
            self.assertEqual(
                report["provenance"]["watchlist_sha256"],
                hashlib.sha256(watchlist_text.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(len(report["decision_id"]), 64)
            self.assertEqual(len(report["provenance"]["selector"]["sha256"]), 64)
            self.assertEqual(len(report["provenance"]["analyzer"]["sha256"]), 64)
            self.assertTrue((Path(directory) / f"{trade_date.isoformat()}.json").is_file())

    def test_activation_request_cannot_select_replacement(self) -> None:
        trade_date = date.today()
        analyses = [
            attach_precomputed_rs5(candidate("WEAK"), -5.0, trade_date),
            attach_precomputed_rs5(candidate("STRONG"), 2.0, trade_date),
        ]
        with tempfile.TemporaryDirectory() as directory, (
            patch.object(
                trade_bot.wbt,
                "fetch_index_sessions",
                side_effect=AssertionError("shadow must not fetch index data"),
            )
        ), patch.object(
            trade_bot.wbt,
            "fetch_stock_sessions",
            side_effect=AssertionError("shadow must not fetch stock data"),
        ):
            report = trade_bot.run_rs5_shadow_selector(
                analyses,
                trade_date,
                selector_enabled=True,
                threshold_pp=-2.39,
                output_dir=directory,
            )

            self.assertEqual(report["decision"], "replacement")
            self.assertEqual(report["mode"], "shadow")
            self.assertFalse(report["activation_available"])
            self.assertFalse(report["activation_applied"])
            self.assertEqual(report["live_selected_symbol"], "WEAK")
            self.assertIn("manifest", report["activation_blocked_reason"])

    def test_audit_write_failure_fails_open_to_original_leader(self) -> None:
        trade_date = date.today()
        analyses = [
            attach_precomputed_rs5(candidate("WEAK"), -5.0, trade_date),
            attach_precomputed_rs5(candidate("STRONG"), 2.0, trade_date),
        ]
        with (
            patch.object(
                trade_bot.wbt,
                "fetch_index_sessions",
                side_effect=AssertionError("shadow must not fetch index data"),
            ),
            patch.object(
                trade_bot.wbt,
                "fetch_stock_sessions",
                side_effect=AssertionError("shadow must not fetch stock data"),
            ),
            patch.object(
                trade_bot.rs5_shadow,
                "write_atomic_report",
                side_effect=OSError("disk unavailable"),
            ),
        ):
            report = trade_bot.run_rs5_shadow_selector(
                analyses,
                trade_date,
                selector_enabled=True,
                threshold_pp=-2.39,
            )

        self.assertFalse(report["activation_applied"])
        self.assertEqual(report["mode"], "shadow")
        self.assertEqual(report["live_selected_symbol"], "WEAK")
        self.assertIn("audit write failed", report["audit_error"])

    def test_activation_request_cannot_apply_skip(self) -> None:
        trade_date = date.today()
        analyses = [
            attach_precomputed_rs5(candidate("WEAK"), -5.0, trade_date),
            attach_precomputed_rs5(candidate("ALSO_WEAK"), -4.0, trade_date),
        ]
        with tempfile.TemporaryDirectory() as directory, (
            patch.object(
                trade_bot.wbt,
                "fetch_index_sessions",
                side_effect=AssertionError("shadow must not fetch index data"),
            )
        ), patch.object(
            trade_bot.wbt,
            "fetch_stock_sessions",
            side_effect=AssertionError("shadow must not fetch stock data"),
        ):
            report = trade_bot.run_rs5_shadow_selector(
                analyses,
                trade_date,
                selector_enabled=True,
                threshold_pp=-2.39,
                output_dir=directory,
            )

        self.assertEqual(report["decision"], "skip")
        self.assertFalse(report["activation_applied"])
        self.assertEqual(report["live_selected_symbol"], "WEAK")
        self.assertEqual(report["mode"], "shadow")


from tests.local_artifacts import requires_local_files, synthetic_artifact_pins


class RS5ProductionActivationContractTests(unittest.TestCase):
    """Red/green contract for a future manifest-gated production resolver.

    These tests intentionally target a small pure orchestration API instead of
    ``cmd_enter``.  The broker boundary is represented by
    ``candidate_info_provider(candidate)`` so no network/order method is ever
    reachable from this suite.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        synthetic_artifact_pins(root, trade_bot.RS5_REQUIRED_RUNTIME_ARTIFACTS)
        synthetic_artifact_pins(root, ["models/" + filename for filename in (
            trade_bot.wbt.SAME_DAY_TOP_RERANKER_MODEL_FILENAME,
            trade_bot.wbt.SAME_DAY_TOP_WINNER_RERANKER_MODEL_FILENAME,
            trade_bot.wbt.SAME_DAY_TOP_T2PLUS_RERANKER_MODEL_FILENAME,
            trade_bot.wbt.RUNNER_DAY_CONFIDENCE_MODEL_FILENAME,
        )])
        for name, value in (("PROJECT_DIR", str(root)), ("MODEL_DIR", root / "models")):
            item = patch.object(trade_bot, name, value)
            item.start()
            self.addCleanup(item.stop)

    def build_case(self, directory: str, rs5_values: list[float]):
        trade_date = date.today()
        analyses = [
            attach_precomputed_rs5(candidate(f"RANK{rank}"), value, trade_date)
            for rank, value in enumerate(rs5_values, start=1)
        ]
        report = trade_bot.run_rs5_shadow_selector(
            analyses,
            trade_date,
            selector_enabled=False,
            threshold_pp=-2.39,
            output_dir=directory,
            watchlist_text="\n".join(item.symbol for item in analyses),
        )
        return analyses, report

    def write_valid_manifest(self, report: dict, directory: str) -> Path:
        provenance = report["provenance"]
        payload = {
            "schema_version": 1,
            "policy": "directional_rs5_top3",
            "approved": True,
            "effective_from": report["trade_date"],
            "feature": report["feature"],
            "threshold_pp": report["threshold_pp"],
            "top_k": report["top_k"],
            "artifacts": {
                "analyzer": provenance["analyzer"],
                "selector": provenance["selector"],
                "models": provenance["models"],
            },
            "runtime_artifacts": {
                relative_path: selector.sha256_file(
                    Path(trade_bot.PROJECT_DIR) / relative_path
                )
                for relative_path in trade_bot.RS5_REQUIRED_RUNTIME_ARTIFACTS
            },
            "required_config": {
                key: report["runtime_config"][key]
                for key in trade_bot.RS5_REQUIRED_CONFIG_KEYS
            },
            "forward_evidence": {
                "baseline_eligible_days": 30,
                "divergences": 8,
                "paired_net_delta_after_costs_pp": 1.0,
                "forward_mdd_not_worse": True,
                "single_replacement_dominated": False,
            },
        }
        path = Path(directory) / "config/rs5_activation_manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return path

    def resolve(
        self,
        analyses,
        report,
        *,
        activation_requested: bool,
        manifest_path: Path,
        candidate_info_provider,
    ):
        self.assertTrue(
            hasattr(trade_bot, "resolve_rs5_activation"),
            "production API must expose trade_bot.resolve_rs5_activation",
        )
        return trade_bot.resolve_rs5_activation(
            analyses,
            report,
            activation_requested=activation_requested,
            activation_manifest_path=str(manifest_path),
            candidate_info_provider=candidate_info_provider,
        )

    @staticmethod
    def tradable(_candidate):
        return {"api_trade_available": True, "short_enabled": True}

    def test_valid_manifest_and_activation_request_apply_keep(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyses, report = self.build_case(directory, [1.0, 2.0])
            manifest = self.write_valid_manifest(report, directory)
            selected, skip, resolved = self.resolve(
                analyses,
                report,
                activation_requested=True,
                manifest_path=manifest,
                candidate_info_provider=self.tradable,
            )

        self.assertIs(selected, analyses[0])
        self.assertFalse(skip)
        self.assertEqual(resolved["decision"], "keep")
        self.assertEqual(resolved["mode"], "active")
        self.assertTrue(resolved["activation_applied"])
        self.assertEqual(resolved["activation_manifest_status"], "valid")
        self.assertEqual(resolved["live_selected_symbol"], "RANK1")

    def test_valid_manifest_and_activation_request_apply_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyses, report = self.build_case(directory, [-5.0, 2.0])
            manifest = self.write_valid_manifest(report, directory)
            provider = Mock(return_value={"api_trade_available": True, "short_enabled": True})
            selected, skip, resolved = self.resolve(
                analyses,
                report,
                activation_requested=True,
                manifest_path=manifest,
                candidate_info_provider=provider,
            )

        self.assertIs(selected, analyses[1])
        self.assertFalse(skip)
        provider.assert_called_once_with(analyses[1])
        self.assertEqual(resolved["decision"], "replacement")
        self.assertEqual(resolved["mode"], "active")
        self.assertTrue(resolved["activation_applied"])
        self.assertEqual(resolved["live_selected_symbol"], "RANK2")

    def test_valid_manifest_and_activation_request_apply_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyses, report = self.build_case(directory, [-5.0, -4.0])
            manifest = self.write_valid_manifest(report, directory)
            provider = Mock(side_effect=AssertionError("skip must not query a replacement"))
            selected, skip, resolved = self.resolve(
                analyses,
                report,
                activation_requested=True,
                manifest_path=manifest,
                candidate_info_provider=provider,
            )

        self.assertIsNone(selected)
        self.assertTrue(skip)
        provider.assert_not_called()
        self.assertEqual(resolved["decision"], "skip")
        self.assertEqual(resolved["mode"], "active")
        self.assertTrue(resolved["activation_applied"])
        self.assertIsNone(resolved["live_selected_symbol"])

    def test_untradeable_replacement_falls_back_to_legacy_rank1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyses, report = self.build_case(directory, [-5.0, 2.0])
            manifest = self.write_valid_manifest(report, directory)
            provider = Mock(return_value={"api_trade_available": False, "short_enabled": True})
            selected, skip, resolved = self.resolve(
                analyses,
                report,
                activation_requested=True,
                manifest_path=manifest,
                candidate_info_provider=provider,
            )

        self.assertIs(selected, analyses[0])
        self.assertFalse(skip)
        self.assertFalse(resolved["activation_applied"])
        self.assertTrue(resolved["activation_fallback_applied"])
        self.assertEqual(resolved["live_selected_symbol"], "RANK1")
        self.assertIn("api_trade_available", resolved["activation_fallback_reason"])

    def test_missing_replacement_metadata_falls_back_to_legacy_rank1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyses, report = self.build_case(directory, [-5.0, 2.0])
            manifest = self.write_valid_manifest(report, directory)
            provider = Mock(return_value=None)
            selected, skip, resolved = self.resolve(
                analyses,
                report,
                activation_requested=True,
                manifest_path=manifest,
                candidate_info_provider=provider,
            )

        self.assertIs(selected, analyses[0])
        self.assertFalse(skip)
        self.assertFalse(resolved["activation_applied"])
        self.assertTrue(resolved["activation_fallback_applied"])
        self.assertEqual(resolved["live_selected_symbol"], "RANK1")
        self.assertIn("metadata", resolved["activation_fallback_reason"])

    def test_absent_manifest_fails_closed_to_legacy_rank1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyses, report = self.build_case(directory, [-5.0, 2.0])
            missing = Path(directory) / "absent.json"
            provider = Mock(side_effect=AssertionError("manifest must be checked first"))
            selected, skip, resolved = self.resolve(
                analyses,
                report,
                activation_requested=True,
                manifest_path=missing,
                candidate_info_provider=provider,
            )

        self.assertIs(selected, analyses[0])
        self.assertFalse(skip)
        provider.assert_not_called()
        self.assertFalse(resolved["activation_applied"])
        self.assertEqual(resolved["activation_manifest_status"], "absent")
        self.assertEqual(resolved["live_selected_symbol"], "RANK1")

    def test_artifact_mismatched_manifest_fails_closed_to_legacy_rank1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyses, report = self.build_case(directory, [-5.0, 2.0])
            manifest = self.write_valid_manifest(report, directory)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["artifacts"]["selector"]["sha256"] = "0" * 64
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            provider = Mock(side_effect=AssertionError("invalid manifest must fail closed first"))
            selected, skip, resolved = self.resolve(
                analyses,
                report,
                activation_requested=True,
                manifest_path=manifest,
                candidate_info_provider=provider,
            )

        self.assertIs(selected, analyses[0])
        self.assertFalse(skip)
        provider.assert_not_called()
        self.assertFalse(resolved["activation_applied"])
        self.assertEqual(resolved["activation_manifest_status"], "invalid")
        self.assertEqual(resolved["live_selected_symbol"], "RANK1")

    def test_manifest_cannot_drop_release_guards(self) -> None:
        for missing_key in ("effective_from", "runtime_artifacts", "required_config"):
            with self.subTest(missing_key=missing_key), tempfile.TemporaryDirectory() as directory:
                analyses, report = self.build_case(directory, [-5.0, 2.0])
                manifest = self.write_valid_manifest(report, directory)
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                payload.pop(missing_key)
                manifest.write_text(json.dumps(payload), encoding="utf-8")
                provider = Mock(
                    side_effect=AssertionError("downgraded manifest must fail before broker preflight")
                )
                selected, skip, resolved = self.resolve(
                    analyses,
                    report,
                    activation_requested=True,
                    manifest_path=manifest,
                    candidate_info_provider=provider,
                )

                self.assertIs(selected, analyses[0])
                self.assertFalse(skip)
                provider.assert_not_called()
                self.assertEqual(resolved["activation_manifest_status"], "invalid")
                self.assertFalse(resolved["activation_applied"])

    def test_shadow_zero_ignores_even_valid_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyses, report = self.build_case(directory, [-5.0, 2.0])
            manifest = self.write_valid_manifest(report, directory)
            provider = Mock(side_effect=AssertionError("shadow mode must not probe replacement"))
            selected, skip, resolved = self.resolve(
                analyses,
                report,
                activation_requested=False,
                manifest_path=manifest,
                candidate_info_provider=provider,
            )

        self.assertIs(selected, analyses[0])
        self.assertFalse(skip)
        provider.assert_not_called()
        self.assertFalse(resolved["activation_applied"])
        self.assertEqual(resolved["mode"], "shadow")
        self.assertEqual(resolved["live_selected_symbol"], "RANK1")


class RS5CmdEnterPreflightTests(unittest.TestCase):
    """Order-boundary tests for active RS5 orchestration in ``cmd_enter``."""

    class WeekdayDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 20)

    class RecordingBot:
        live = True

        def __init__(self) -> None:
            self.broker_calls: list[tuple] = []

        def estimate_order_price(self, uid, lots, direction, price):
            self.broker_calls.append(("estimate_order_price", uid, lots, direction, price))
            return {"commission_rub": 10.0, "fee_side_pct": 0.04}

        def __getattr__(self, name):
            def unexpected(*args, **kwargs):
                self.broker_calls.append((name, args, kwargs))
                raise AssertionError(f"unexpected broker call: {name}")

            return unexpected

    @staticmethod
    def make_analyses():
        items = [candidate("RANK1"), candidate("RANK2")]
        for item in items:
            item.exit_target.price = 103.0
            item.exit_target.move_pct_from_close = 3.0
        return items

    @staticmethod
    def ready_plan(item, *, price: float = 100.0, lots: int = 7):
        info = {
            "uid": f"uid-{item.symbol}",
            "lot": 10,
            "min_price_increment": 0.01,
            "api_trade_available": True,
            "short_enabled": True,
        }
        return {
            "symbol": item.symbol,
            "direction": item.direction,
            "ready": True,
            "retry": False,
            "reason": None,
            "info": info,
            "price": price,
            "target_price": 103.0,
            "target_move_at_quote": 3.0,
            "cash_before": 150_000.0,
            "budget": 150_000.0,
            "direction_risk_multiplier": 0.5,
            "confidence_risk_multiplier": 1.0,
            "risk": 0.5,
            "position_rub": 75_000.0,
            "lot_cost": 1_000.0,
            "lots": lots,
            "api_trade_available": True,
            "short_enabled": True,
        }

    @staticmethod
    def reject_plan(item, reason: str, *, retry: bool = False):
        return {
            "symbol": item.symbol,
            "direction": item.direction,
            "ready": False,
            "retry": retry,
            "reason": reason,
        }

    @staticmethod
    def base_report(decision: str, recommended_rank=None, recommended_symbol=None):
        return {
            "decision": decision,
            "decision_reason": f"fixture {decision}",
            "decision_id": "decision-fixture",
            "recommended_rank": recommended_rank,
            "recommended_symbol": recommended_symbol,
            "mode": "shadow",
            "activation_applied": False,
            "activation_fallback_applied": False,
            "live_selected_symbol": "RANK1",
        }

    def invoke(
        self,
        analyses,
        report,
        *,
        resolver,
        preflight,
        audit_ok: bool = True,
        force: bool = False,
        input_path: str | None = None,
        live: bool = True,
    ):
        bot = self.RecordingBot()
        bot.live = live
        journal_writes: list[dict] = []
        place = Mock(return_value=0)
        preflight_mock = Mock(side_effect=preflight)
        resolver_mock = Mock(side_effect=resolver)
        persist_mock = Mock(return_value=audit_ok)
        with (
            patch.object(trade_bot, "date", self.WeekdayDate),
            patch.object(trade_bot, "RS5_ACTIVATION_REQUESTED", True),
            patch.object(trade_bot, "load_journal", return_value=None),
            patch.object(trade_bot, "read_watchlist", return_value="raw watchlist"),
            patch.object(trade_bot.wbt, "analyze_watchlist", return_value=analyses),
            patch.object(trade_bot.wbt, "detect_reference_close_mismatch", return_value=None),
            patch.object(trade_bot.wbt, "detect_watchlist_mismatch", return_value=None),
            patch.object(trade_bot, "run_rs5_shadow_selector", return_value=dict(report)),
            patch.object(trade_bot, "resolve_rs5_activation", resolver_mock),
            patch.object(trade_bot, "preflight_engine_a_candidate", preflight_mock),
            patch.object(trade_bot, "persist_rs5_report", persist_mock),
            patch.object(trade_bot, "place_entry", place),
            patch.object(
                trade_bot,
                "upsert_trade",
                side_effect=lambda live, day, record: journal_writes.append(dict(record)),
            ),
        ):
            rc = trade_bot.cmd_enter(
                bot,
                SimpleNamespace(generate=False, force=force, input=input_path),
            )
        return SimpleNamespace(
            rc=rc,
            bot=bot,
            journal_writes=journal_writes,
            place=place,
            preflight=preflight_mock,
            resolver=resolver_mock,
            persist=persist_mock,
        )

    @staticmethod
    def active_result(report, selected, *, skip: bool = False):
        resolved = dict(report)
        resolved.update(
            {
                "mode": "active",
                "activation_applied": True,
                "activation_resolution_id": "resolution-fixture",
                "activation_manifest_sha256": "a" * 64,
                "live_selected_symbol": selected.symbol if selected is not None else None,
                "live_selected_rank": 2 if selected is not None and selected.symbol == "RANK2" else 1,
            }
        )
        return selected, skip, resolved

    def test_active_skip_makes_zero_broker_calls_and_one_skipped_journal(self) -> None:
        analyses = self.make_analyses()
        report = self.base_report("skip")

        def resolver(items, selector_report, **kwargs):
            return self.active_result(selector_report, None, skip=True)

        result = self.invoke(
            analyses,
            report,
            resolver=resolver,
            preflight=AssertionError("active skip must not preflight"),
        )

        self.assertEqual(result.rc, 0)
        self.assertEqual(result.bot.broker_calls, [])
        result.preflight.assert_not_called()
        result.place.assert_not_called()
        self.assertEqual(len(result.journal_writes), 1)
        self.assertEqual(result.journal_writes[0]["phase"], "skipped")
        self.assertEqual(result.journal_writes[0]["selector_decision"], "skip")

    def test_force_audit_uses_separate_suppressed_namespace(self) -> None:
        analyses = self.make_analyses()
        report = self.base_report("replacement", 2, "RANK2")

        def resolver(items, selector_report, **kwargs):
            self.assertFalse(kwargs["activation_requested"])
            resolved = dict(selector_report)
            resolved.update(
                {
                    "mode": "shadow",
                    "activation_applied": False,
                    "activation_resolution_id": "suppressed-resolution",
                    "live_selected_symbol": items[0].symbol,
                    "live_selected_rank": 1,
                }
            )
            return items[0], False, resolved

        result = self.invoke(
            analyses,
            report,
            resolver=resolver,
            preflight=lambda _bot, item, _cash: self.ready_plan(item),
            force=True,
        )

        self.assertEqual(result.rc, 0)
        audit_directory = result.persist.call_args.args[1]
        self.assertTrue(audit_directory.endswith("suppressed_manual"))

    def test_dry_run_cannot_activate_or_occupy_production_audit(self) -> None:
        analyses = self.make_analyses()
        report = self.base_report("replacement", 2, "RANK2")

        def resolver(items, selector_report, **kwargs):
            self.assertFalse(kwargs["activation_requested"])
            self.assertEqual(
                selector_report["activation_suppressed_reason"],
                "dry-run without --live",
            )
            resolved = dict(selector_report)
            resolved.update(
                {
                    "mode": "shadow",
                    "activation_applied": False,
                    "activation_resolution_id": "dry-run-resolution",
                    "live_selected_symbol": items[0].symbol,
                    "live_selected_rank": 1,
                }
            )
            return items[0], False, resolved

        result = self.invoke(
            analyses,
            report,
            resolver=resolver,
            preflight=lambda _bot, item, _cash: self.ready_plan(item),
            live=False,
        )

        self.assertEqual(result.rc, 0)
        audit_directory = result.persist.call_args.args[1]
        self.assertTrue(audit_directory.endswith("suppressed_manual"))

    def test_ready_replacement_places_exactly_the_replacement_plan(self) -> None:
        analyses = self.make_analyses()
        report = self.base_report("replacement", 2, "RANK2")
        replacement_plan = self.ready_plan(analyses[1], price=101.0, lots=5)

        def preflight(bot, item, cash_provider):
            self.assertIs(item, analyses[1])
            return replacement_plan

        def resolver(items, selector_report, candidate_info_provider, **kwargs):
            self.assertIs(candidate_info_provider(items[1]), replacement_plan)
            return self.active_result(selector_report, items[1])

        result = self.invoke(analyses, report, resolver=resolver, preflight=preflight)

        self.assertEqual(result.rc, 0)
        result.preflight.assert_called_once()
        result.place.assert_called_once()
        called_bot, base_state, price, lots, info = result.place.call_args.args
        self.assertIs(called_bot, result.bot)
        self.assertEqual(base_state["symbol"], "RANK2")
        self.assertEqual(base_state["selector_original_symbol"], "RANK1")
        self.assertEqual(base_state["selector_final_symbol"], "RANK2")
        self.assertEqual(price, replacement_plan["price"])
        self.assertEqual(lots, replacement_plan["lots"])
        self.assertIs(info, replacement_plan["info"])
        self.assertEqual(result.journal_writes, [])

    def test_deterministic_replacement_reject_uses_ready_rank1_once(self) -> None:
        analyses = self.make_analyses()
        report = self.base_report("replacement", 2, "RANK2")
        replacement_reject = self.reject_plan(analyses[1], "RANK2 deterministic reject")
        legacy_plan = self.ready_plan(analyses[0], lots=4)

        def preflight(bot, item, cash_provider):
            return replacement_reject if item is analyses[1] else legacy_plan

        def resolver(items, selector_report, candidate_info_provider, **kwargs):
            rejected = candidate_info_provider(items[1])
            self.assertFalse(rejected["ready"])
            resolved = dict(selector_report)
            resolved.update(
                {
                    "activation_applied": False,
                    "activation_fallback_applied": True,
                    "activation_fallback_reason": rejected["reason"],
                    "activation_resolution_id": "fallback-fixture",
                    "live_selected_symbol": "RANK1",
                    "live_selected_rank": 1,
                }
            )
            return items[0], False, resolved

        result = self.invoke(analyses, report, resolver=resolver, preflight=preflight)

        self.assertEqual(result.rc, 0)
        self.assertEqual(
            [call.args[1].symbol for call in result.preflight.call_args_list],
            ["RANK2", "RANK1"],
        )
        result.place.assert_called_once()
        self.assertEqual(result.place.call_args.args[1]["symbol"], "RANK1")
        self.assertEqual(result.place.call_args.args[3], legacy_plan["lots"])
        self.assertEqual(result.journal_writes, [])

    def test_both_deterministically_rejected_produces_one_skip(self) -> None:
        analyses = self.make_analyses()
        report = self.base_report("replacement", 2, "RANK2")
        plans = {
            "RANK2": self.reject_plan(analyses[1], "RANK2 deterministic reject"),
            "RANK1": self.reject_plan(analyses[0], "RANK1 deterministic reject"),
        }

        def preflight(bot, item, cash_provider):
            return plans[item.symbol]

        def resolver(items, selector_report, candidate_info_provider, **kwargs):
            candidate_info_provider(items[1])
            resolved = dict(selector_report)
            resolved.update(
                {
                    "activation_applied": False,
                    "activation_fallback_applied": True,
                    "activation_fallback_reason": plans["RANK2"]["reason"],
                    "live_selected_symbol": "RANK1",
                }
            )
            return items[0], False, resolved

        result = self.invoke(analyses, report, resolver=resolver, preflight=preflight)

        self.assertEqual(result.rc, 0)
        result.place.assert_not_called()
        self.assertEqual(len(result.journal_writes), 1)
        self.assertEqual(result.journal_writes[0]["phase"], "skipped")
        self.assertIn("RANK2", result.journal_writes[0]["reason"])
        self.assertIn("RANK1", result.journal_writes[0]["reason"])

    def test_any_retry_with_no_ready_returns_error_without_skip(self) -> None:
        analyses = self.make_analyses()
        report = self.base_report("replacement", 2, "RANK2")
        for retry_symbol in ("RANK1", "RANK2"):
            with self.subTest(retry_symbol=retry_symbol):
                plans = {
                    item.symbol: self.reject_plan(
                        item,
                        f"{item.symbol} unavailable",
                        retry=item.symbol == retry_symbol,
                    )
                    for item in analyses
                }

                def preflight(bot, item, cash_provider):
                    return plans[item.symbol]

                def resolver(items, selector_report, candidate_info_provider, **kwargs):
                    candidate_info_provider(items[1])
                    resolved = dict(selector_report)
                    resolved.update(
                        {
                            "activation_applied": False,
                            "activation_fallback_applied": True,
                            "activation_fallback_reason": plans["RANK2"]["reason"],
                            "live_selected_symbol": "RANK1",
                        }
                    )
                    return items[0], False, resolved

                result = self.invoke(
                    analyses,
                    report,
                    resolver=resolver,
                    preflight=preflight,
                )
                self.assertEqual(result.rc, 1)
                result.place.assert_not_called()
                self.assertEqual(result.journal_writes, [])

    def test_audit_identity_failure_forces_ready_rank1(self) -> None:
        analyses = self.make_analyses()
        report = self.base_report("replacement", 2, "RANK2")
        plans = {
            "RANK2": self.ready_plan(analyses[1], lots=5),
            "RANK1": self.ready_plan(analyses[0], lots=3),
        }

        def preflight(bot, item, cash_provider):
            return plans[item.symbol]

        def resolver(items, selector_report, candidate_info_provider, **kwargs):
            candidate_info_provider(items[1])
            return self.active_result(selector_report, items[1])

        result = self.invoke(
            analyses,
            report,
            resolver=resolver,
            preflight=preflight,
            audit_ok=False,
        )

        self.assertEqual(result.rc, 0)
        self.assertEqual(
            [call.args[1].symbol for call in result.preflight.call_args_list],
            ["RANK2", "RANK1"],
        )
        result.place.assert_called_once()
        base_state = result.place.call_args.args[1]
        self.assertEqual(base_state["symbol"], "RANK1")
        self.assertTrue(base_state["selector_fallback_applied"])
        self.assertEqual(base_state["selector_fallback_reason"], "audit write/identity failure")


class RunTickConfigTests(unittest.TestCase):
    def test_live_wrapper_enables_only_manifest_gated_rs5_selector(self) -> None:
        text = (Path(trade_bot.PROJECT_DIR) / "scripts/run_tick.sh").read_text(encoding="utf-8")
        self.assertIn("export BOT_RS5_SELECTOR=1", text)
        self.assertIn(
            'export BOT_RS5_ACTIVATION_MANIFEST="$DIR/config/rs5_activation_manifest.json"',
            text,
        )

    @requires_local_files("config/rs5_activation_manifest.json")
    def test_production_manifest_pins_the_current_release_files(self) -> None:
        project = Path(trade_bot.PROJECT_DIR)
        manifest = json.loads(
            (project / "config/rs5_activation_manifest.json").read_text(encoding="utf-8")
        )
        self.assertTrue(manifest["approved"])
        self.assertEqual(manifest["effective_from"], "2026-07-20")
        self.assertEqual(
            manifest["forward_evidence"]["status"], "explicit_user_override"
        )
        pinned = {
            "argonus/watchlists/watchlist_best_target.py": manifest["artifacts"]["analyzer"]["sha256"],
            "argonus/watchlists/shadow_rs5_selector.py": manifest["artifacts"]["selector"]["sha256"],
            **{
                "models/"+name: value["sha256"]
                for name, value in manifest["artifacts"]["models"].items()
            },
            **manifest["runtime_artifacts"],
        }
        for relative_path, expected_hash in pinned.items():
            with self.subTest(relative_path=relative_path):
                self.assertEqual(selector.sha256_file(project / relative_path), expected_hash)


if __name__ == "__main__":
    unittest.main()
