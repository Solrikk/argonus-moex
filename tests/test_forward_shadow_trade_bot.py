#!/usr/bin/env python3
"""Zero-production-effect contracts for the forward shadow hooks."""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from argonus.trading import trade_bot
from tests.test_shadow_rs5_selector import RS5CmdEnterPreflightTests


class SelectorHookZeroEffectTests(unittest.TestCase):
    @staticmethod
    def _run(*, enabled: bool, hook_effect=None):
        harness = RS5CmdEnterPreflightTests()
        analyses = harness.make_analyses()
        report = harness.base_report("keep", 1, "RANK1")

        def resolver(items, selector_report, **_kwargs):
            return harness.active_result(selector_report, items[0])

        hook = Mock(side_effect=hook_effect) if isinstance(hook_effect, Exception) else Mock(
            return_value=hook_effect
        )
        with (
            patch.object(trade_bot, "FORWARD_SHADOW_ENABLED", enabled),
            patch.object(trade_bot, "_record_forward_selector_shadow", hook),
        ):
            result = harness.invoke(
                analyses,
                report,
                resolver=resolver,
                preflight=lambda _bot, item, _cash: harness.ready_plan(
                    item, price=100.0, lots=7
                ),
                force=False,
                input_path=None,
                live=True,
            )
        return result, hook, [item.symbol for item in analyses]

    def test_success_error_and_disabled_paths_freeze_identical_live_order(self) -> None:
        disabled, disabled_hook, disabled_order = self._run(enabled=False)
        success, success_hook, success_order = self._run(
            enabled=True,
            # Even a malicious-looking return value must be ignored.
            hook_effect={"recommended_symbol": "RANK6", "lots": 999_999},
        )
        failed, failed_hook, failed_order = self._run(
            enabled=True,
            hook_effect=OSError("telemetry disk unavailable"),
        )

        disabled_hook.assert_not_called()
        success_hook.assert_called_once()
        failed_hook.assert_called_once()
        self.assertEqual(disabled.rc, success.rc)
        self.assertEqual(disabled.rc, failed.rc)
        self.assertEqual(disabled_order, success_order)
        self.assertEqual(disabled_order, failed_order)

        # Ignore the distinct FakeBot instance (arg0); every frozen order
        # argument, including symbol/uid/lots/target/margin flag, must match.
        baseline_args = disabled.place.call_args.args[1:]
        self.assertEqual(success.place.call_args.args[1:], baseline_args)
        self.assertEqual(failed.place.call_args.args[1:], baseline_args)
        self.assertEqual(disabled.bot.broker_calls, success.bot.broker_calls)
        self.assertEqual(disabled.bot.broker_calls, failed.bot.broker_calls)

    def test_force_custom_and_dry_run_never_enter_forward_namespace(self) -> None:
        harness = RS5CmdEnterPreflightTests()
        for live, force, input_path in (
            (False, False, None),
            (True, True, None),
            (True, False, "manual-watchlist.txt"),
        ):
            with self.subTest(live=live, force=force, input_path=input_path):
                analyses = harness.make_analyses()
                report = harness.base_report("keep", 1, "RANK1")

                def resolver(items, selector_report, **_kwargs):
                    return harness.active_result(selector_report, items[0])

                hook = Mock(side_effect=AssertionError("forward hook must be suppressed"))
                with (
                    patch.object(trade_bot, "FORWARD_SHADOW_ENABLED", True),
                    patch.object(trade_bot, "_record_forward_selector_shadow", hook),
                ):
                    result = harness.invoke(
                        analyses,
                        report,
                        resolver=resolver,
                        preflight=lambda _bot, item, _cash: harness.ready_plan(item),
                        force=force,
                        input_path=input_path,
                        live=live,
                    )
                self.assertEqual(result.rc, 0)
                hook.assert_not_called()


class ExitHookZeroEffectTests(unittest.TestCase):
    class ProtectionBot:
        live = True

        def __init__(self, *, fail_stop: bool = False, fail_take: bool = False) -> None:
            self.fail_stop = fail_stop
            self.fail_take = fail_take
            self.placed: list[tuple[str, str, int, str, float]] = []

        def stop_order(self, uid, lots, direction, price, kind):
            self.placed.append((kind, uid, lots, direction, price))
            if kind == "STOP_LOSS" and self.fail_stop:
                raise RuntimeError("stop unavailable")
            if kind == "TAKE_PROFIT" and self.fail_take:
                raise RuntimeError("take unavailable")
            return f"{kind.lower()}-id"

    @staticmethod
    def base() -> dict:
        return {
            "date": "2026-07-20",
            "engine": "A",
            "symbol": "TEST",
            "uid": "uid-test",
            "direction": "long",
            "lots": 10,
            "lot_size": 10,
            "increment": 0.01,
            "reference_price": 100.0,
            "target_price": 103.0,
            "risk_pct": 1.0,
            "estimated_fee_side_pct": 0.04,
        }

    def _run(self, *, enabled: bool, hook_effect=None, fail_take: bool = False):
        bot = self.ProtectionBot(fail_take=fail_take)
        writes: list[dict] = []
        hook = Mock(side_effect=hook_effect) if isinstance(hook_effect, Exception) else Mock(
            side_effect=hook_effect if callable(hook_effect) else None
        )
        with (
            patch.object(trade_bot, "FORWARD_SHADOW_ENABLED", enabled),
            patch.object(trade_bot, "_record_forward_exit_shadow", hook),
            patch.object(
                trade_bot,
                "upsert_trade",
                side_effect=lambda live, day, state: writes.append(dict(state)),
            ),
        ):
            trade_bot.finalize_entry(bot, self.base(), 10, 100.0)
        return bot, writes, hook

    def test_writer_success_or_failure_cannot_change_protection_or_journal(self) -> None:
        baseline_bot, baseline_writes, baseline_hook = self._run(enabled=False)

        def mutate_private_copy(_bot, value, **_kwargs):
            value["lots"] = 999_999
            value["stop_price"] = 0.01

        success_bot, success_writes, success_hook = self._run(
            enabled=True, hook_effect=mutate_private_copy
        )
        failed_bot, failed_writes, failed_hook = self._run(
            enabled=True, hook_effect=OSError("sidecar read-only")
        )

        baseline_hook.assert_not_called()
        success_hook.assert_called_once()
        failed_hook.assert_called_once()
        self.assertEqual(success_bot.placed, baseline_bot.placed)
        self.assertEqual(failed_bot.placed, baseline_bot.placed)
        self.assertEqual(success_writes, baseline_writes)
        self.assertEqual(failed_writes, baseline_writes)

    def test_stop_failure_remains_authoritative_and_never_calls_shadow(self) -> None:
        bot = self.ProtectionBot(fail_stop=True)
        hook = Mock(side_effect=AssertionError("shadow must follow confirmed STOP"))
        with (
            patch.object(trade_bot, "FORWARD_SHADOW_ENABLED", True),
            patch.object(trade_bot, "_record_forward_exit_shadow", hook),
            patch.object(trade_bot, "upsert_trade"),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop unavailable"):
                trade_bot.finalize_entry(bot, self.base(), 10, 100.0)
        hook.assert_not_called()
        self.assertEqual([row[0] for row in bot.placed], ["STOP_LOSS"])

    def test_take_failure_still_calls_shadow_only_after_stop(self) -> None:
        bot, writes, hook = self._run(enabled=True, fail_take=True)
        hook.assert_called_once()
        self.assertEqual([row[0] for row in bot.placed], ["STOP_LOSS", "TAKE_PROFIT"])
        self.assertEqual(writes[-1]["protection_status"], "stop_placed")
        self.assertIn("TAKE_PROFIT", writes[-1]["protection_error"])


class LauncherContractTests(unittest.TestCase):
    def test_live_wrapper_enables_only_shadow_data_collection(self) -> None:
        with open(trade_bot.PROJECT_DIR + "/scripts/run_tick.sh", "r", encoding="utf-8") as handle:
            wrapper = handle.read()
        self.assertIn('export BOT_FORWARD_SHADOW=1', wrapper)
        self.assertIn('export BOT_FORWARD_SHADOW_DIR="$DIR/runtime/forward_shadow"', wrapper)
        self.assertIn(
            'export BOT_FORWARD_SHADOW_MANIFEST="$DIR/config/forward_shadow_manifest.json"',
            wrapper,
        )


class BrokerFillTimestampTests(unittest.TestCase):
    class StateBot:
        account_id = "account-test"
        order_state = trade_bot.TradeBot.order_state

        def _post(self, method, payload):
            if method != "OrdersService/GetOrderState":
                raise AssertionError(method)
            return {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                "lotsExecuted": "5",
                "averagePositionPrice": {"units": "100", "nano": 0},
                "stages": [
                    {
                        "quantity": "2",
                        "executionTime": "2026-07-20T04:00:01Z",
                    },
                    {
                        "quantity": "3",
                        "executionTime": "2026-07-20T04:00:04.500Z",
                    },
                ],
            }

    def test_get_order_state_keeps_last_exchange_execution_stage(self) -> None:
        state = self.StateBot().order_state("request-id")
        self.assertEqual(state["lots"], 5)
        self.assertEqual(state["avg_price"], 100.0)
        self.assertEqual(state["execution_time"], "2026-07-20T04:00:04+00:00")


if __name__ == "__main__":
    unittest.main()
