"""Broker-free tests of target125 activation, fills and protection recovery."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager, ExitStack
from datetime import date, time as dtime
from functools import partial
from pathlib import Path
from unittest.mock import patch

from argonus.trading import production_profit_target as policy
from argonus.trading import trade_bot
from tests import test_production_entry_0705 as entry_tests
from tests.test_trade_bot_safety import FakeProtectionBot
from tests.local_artifacts import requires_local_files, synthetic_artifact_pins


@contextmanager
def active_config():
    values = {
        "ENTRY_START": dtime(7, 5), "ENTRY_DEADLINE": dtime(7, 7),
        "ENTRY_EXECUTION_POLICY": "delayed_0705_depth50_fok",
        "ENTRY_BOOK_DEPTH": 50, "ENTRY_MAX_QUOTE_AGE_MS": 3000,
        "ENTRY_MAX_IMPACT_BPS": 10.0, "TARGET_POSITION_RUB": 150000.0,
        "MAX_LEVERAGE": 3.0, "EXIT_TIME": dtime(18, 35),
        "TARGET_DISTANCE_MULTIPLIER": 1.25, "DAY_FILTER": False,
        "REGIME_GATES": True, "SHORT_RALLY_GUARD_PCT": 8.0,
        "MIN_TARGET_PCT": 2.0, "LONG_RISK_MULTIPLIER": 0.5,
        "RISK_PCT": 1.0, "SECOND_ENGINE": False, "CONF_SIZING": False,
        "PULLBACK_PCT": 0.0, "RS5_ACTIVATION_REQUESTED": True,
        "RS5_SELECTOR_THRESHOLD": -2.39,
    }
    with ExitStack() as stack:
        for name, value in values.items():
            stack.enter_context(patch.object(trade_bot, name, value))
        yield


def candidate_state(direction="long", lots=4):
    t4 = 102.0 if direction == "long" else 98.0
    return {
        **entry_tests.base_state(direction, lots), "date": "2026-10-05",
        "original_t4_price": t4, "target_distance_multiplier": 1.25,
        "target_policy": "target125",
    }


class TargetGeometryTests(unittest.TestCase):
    def test_scales_distance_from_actual_fill_and_rounds_toward_entry(self):
        self.assertEqual(policy.scaled_target_price("long", 100.16, 102.007, 0.01), 102.46)
        self.assertEqual(policy.scaled_target_price("short", 99.97, 98, 0.01), 97.51)
        self.assertEqual(policy.scaled_target_price("long", 100, 103, 0.05), 103.75)

    def test_rejects_invalid_geometry_and_nonfinite_prices(self):
        for args in [
            ("long", 100, 99, 0.01), ("short", 100, 101, 0.01),
            ("long", 100, 103, 0), ("long", float("nan"), 103, 0.01),
            ("short", 100, 1, 0.01), ("long", 100, 100.001, 1),
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                policy.scaled_target_price(*args)


class ProductionFillTests(unittest.TestCase):
    def invoke_route(self, bot, state):
        with tempfile.TemporaryDirectory() as directory, active_config(), \
                patch.object(trade_bot, "PROJECT_DIR", directory), \
                patch.object(trade_bot, "FORWARD_SHADOW_ENABLED", False):
            manifest = Path(directory) / "target_manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": 1, "policy": "target125", "approved": True,
                "production_activation_allowed": True, "effective_from": "2026-10-03",
                "activation_evidence": {
                    "status": "explicit_user_override", "forward_gate_passed": False,
                    "approved_by": "workspace_user_explicit_instruction",
                    "approval_date": "2026-10-03",
                },
                "source_research": {"verdict": "HISTORICAL_CANDIDATE_NOT_CONFIRMED"},
                "required_config": trade_bot.profit_target_runtime_config(),
                "runtime_artifacts": synthetic_artifact_pins(directory, policy.REQUIRED_ARTIFACTS),
            }))
            validator = partial(trade_bot.validate_profit_target_activation_manifest, str(manifest))
            with patch.object(trade_bot, "validate_profit_target_activation_manifest", validator):
                return entry_tests.ProductionRouteTests().invoke(bot, state)

    def test_long_and_short_fok_freeze_original_target_then_protect_actual_fill(self):
        for direction, lots, fill, target, stop in [
            ("long", 4, 100.16, 102.46, 99.16),
            ("short", 3, 99.97, 97.51, 100.97),
        ]:
            with self.subTest(direction=direction):
                writes = []
                bot = entry_tests.RouteBot({
                    "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                    "lotsExecuted": str(lots),
                    "executedOrderPrice": entry_tests.quotation(fill),
                }, writes)
                state = candidate_state(direction, lots)
                rc, writes = self.invoke_route(bot, state)
                self.assertEqual(rc, 0)
                self.assertEqual(len(bot.fok_calls), 1)
                intent = next(row for row in writes if row.get("phase") == "entry_submitting")
                self.assertEqual(intent["target_price"], state["original_t4_price"])
                self.assertEqual(intent["target_distance_multiplier"], 1.25)
                self.assertEqual(len(intent["target_activation_manifest_sha256"]), 64)
                self.assertEqual([order[0] for order in bot.protection], ["STOP_LOSS", "TAKE_PROFIT"])
                final = writes[-1]
                self.assertAlmostEqual(final["target_price"], target)
                self.assertAlmostEqual(final["stop_price"], stop)
                self.assertEqual(final["original_t4_price"], state["original_t4_price"])
                self.assertEqual(final["lots"], lots)

    def test_terminal_partial_fill_protects_only_observed_exposure(self):
        writes = []
        bot = entry_tests.RouteBot({
            "executionReportStatus": "EXECUTION_REPORT_STATUS_CANCELLED",
            "lotsExecuted": "2", "executedOrderPrice": entry_tests.quotation(100.16),
        }, writes)
        rc, writes = self.invoke_route(bot, candidate_state())
        self.assertEqual(rc, 0)
        self.assertEqual(writes[-1]["lots"], 2)
        self.assertEqual(writes[-1]["target_price"], 102.46)
        self.assertTrue(all(order[2] == 2 for order in bot.protection))

    def test_lost_ack_reconciles_frozen_policy_even_after_runtime_reverts(self):
        writes = []
        bot = entry_tests.RouteBot(None, writes)
        bot.raise_post = TimeoutError("ACK lost")
        rc, writes = self.invoke_route(bot, candidate_state())
        self.assertEqual(rc, 1)
        frozen = dict(writes[-1])
        self.assertEqual(frozen["phase"], "entry_submitting")
        bot.reconcile_result = {"status": "EXECUTION_REPORT_STATUS_FILL", "lots": 4, "avg_price": 100.16}
        with (
            patch.object(trade_bot, "TARGET_DISTANCE_MULTIPLIER", 1.0),
            patch.object(trade_bot, "FORWARD_SHADOW_ENABLED", False),
            patch.object(trade_bot, "upsert_trade", side_effect=lambda live, day, state: writes.append(dict(state))),
        ):
            rc = trade_bot.reconcile_submitting(bot, frozen)
        self.assertEqual(rc, 0)
        self.assertEqual(len(bot.fok_calls), 1)
        self.assertEqual(writes[-1]["target_price"], 102.46)
        self.assertEqual(writes[-1]["target_distance_multiplier"], 1.25)

    def test_rejected_target_manifest_prevents_entry_orders(self):
        writes = []
        bot = entry_tests.RouteBot(None, writes)
        with patch.object(trade_bot, "validate_profit_target_activation_manifest", return_value=(False, "hash mismatch", None)):
            rc, writes = self.invoke_route(bot, candidate_state())
        self.assertEqual(rc, 1)
        self.assertEqual(bot.fok_calls, [])
        self.assertEqual(bot.book_calls, [])
        self.assertEqual(writes[-1]["phase"], "skipped")


class ProtectionRecoveryTests(unittest.TestCase):
    def finalize(self, state, *, fill=100.0, fail_take=False):
        bot = FakeProtectionBot(fail_take=fail_take)
        writes = []
        with (
            patch.object(trade_bot, "FORWARD_SHADOW_ENABLED", False),
            patch.object(trade_bot, "upsert_trade", side_effect=lambda live, day, value: writes.append(dict(value))),
        ):
            trade_bot.finalize_entry(bot, state, state["lots"], fill)
        return bot, writes

    def test_target_retry_uses_saved_price_without_second_multiplication(self):
        bot, writes = self.finalize(candidate_state(), fail_take=True)
        state = writes[-1]
        self.assertEqual(state["target_price"], 102.5)
        self.assertIsNone(state["take_order_id"])
        bot.fail_take = False
        bot.placed.clear()
        with (
            patch.object(bot, "stop_orders", return_value=[{
                "instrumentUid": "uid-test", "stopOrderType": "STOP_ORDER_TYPE_STOP_LOSS",
            }]),
            patch.object(trade_bot, "upsert_trade"),
            patch.object(trade_bot, "TARGET_DISTANCE_MULTIPLIER", 1.0),
        ):
            trade_bot.repair_protection(bot, state)
        self.assertEqual(bot.placed, [("TAKE_PROFIT", 102.5)])

    def test_missing_or_invalid_original_t4_still_places_stop_and_requests_exit(self):
        for original in [None, 99, float("nan")]:
            state = candidate_state()
            if original is None:
                state.pop("original_t4_price")
            else:
                state["original_t4_price"] = original
            with self.subTest(original=original):
                bot, writes = self.finalize(state)
                self.assertEqual(bot.placed, [("STOP_LOSS", 99.0)])
                self.assertIsNone(writes[-1]["target_price"])
                self.assertTrue(writes[-1]["exit_required"])
                self.assertTrue(writes[-1]["target_policy_error"])

    def test_old_pending_entry_retains_absolute_t4_under_new_runtime_profile(self):
        with active_config():
            bot, writes = self.finalize(entry_tests.base_state())
        self.assertEqual(writes[-1]["target_price"], 102.0)
        self.assertEqual(bot.placed[-1], ("TAKE_PROFIT", 102.0))

    def test_new_profile_does_not_enter_old_shadow_comparisons(self):
        state = {**candidate_state(), "stop_order_id": "stop-id"}
        with self.assertRaisesRegex(RuntimeError, "new registration"):
            trade_bot._record_forward_exit_shadow(FakeProtectionBot(), state)
        with active_config(), self.assertRaisesRegex(RuntimeError, "new registration"):
            trade_bot._record_forward_selector_shadow(
                analyses=[], trade_date=date(2026, 10, 5), control=None, legacy_top=None,
                watchlist_text="", control_plan={}, rs5_report={},
            )


@requires_local_files("config/profit_target_activation_manifest.json")
class ActivationTests(unittest.TestCase):
    def test_actual_activation_manifest_matches_production_config(self):
        with active_config():
            valid, reason, digest = trade_bot.validate_profit_target_activation_manifest(trade_date=date(2026, 10, 5))
        self.assertTrue(valid, reason)
        self.assertEqual(len(digest or ""), 64)

    def test_activation_rejects_tampered_artifacts_approval_and_config(self):
        original = json.loads(Path(trade_bot.TARGET_ACTIVATION_MANIFEST_PATH).read_text())
        for change in ["hash", "approval", "verdict", "config", "missing_pin"]:
            manifest = json.loads(json.dumps(original))
            if change == "hash":
                manifest["runtime_artifacts"]["argonus/trading/production_profit_target.py"] = "0" * 64
            elif change == "approval":
                manifest["activation_evidence"]["forward_gate_passed"] = True
            elif change == "verdict":
                manifest["source_research"]["verdict"] = "CONFIRMED"
            elif change == "config":
                manifest["required_config"]["target_distance_multiplier"] = 1.0
            else:
                manifest["runtime_artifacts"].pop("argonus/trading/trade_bot.py")
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, active_config():
                path = Path(directory) / "activation.json"
                path.write_text(json.dumps(manifest))
                valid, _, digest = trade_bot.validate_profit_target_activation_manifest(str(path), trade_date=date(2026, 10, 5))
                self.assertFalse(valid)
                self.assertIsNone(digest)

    def test_activation_is_not_effective_before_user_instruction(self):
        with active_config():
            valid, reason, _ = trade_bot.validate_profit_target_activation_manifest(trade_date=date(2026, 10, 2))
        self.assertFalse(valid)
        self.assertIn("2026-10-03", reason)


if __name__ == "__main__":
    unittest.main()
