#!/usr/bin/env python3
"""Safety contract for the Engine A 150k RUB margin position mode.

These tests are deliberately network-free.  ``GetMaxLots`` is a read-only
broker preflight; no test is allowed to reach ``PostOrder`` except through a
fake that only records the arguments it would receive.
"""
from __future__ import annotations

import re
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from argonus.trading import trade_bot


from argonus.paths import PROJECT_ROOT as PROJECT_DIR


def candidate(direction: str = "long") -> SimpleNamespace:
    return SimpleNamespace(
        symbol="TEST",
        direction=direction,
        skipped_reason=None,
        market_bullish=direction == "long",
        runner_day_prob=None,
        short_rally_10d_pct=None,
        exit_target=SimpleNamespace(
            label="T4",
            price=103.0 if direction == "long" else 97.0,
            move_pct_from_close=3.0,
        ),
    )


class MarginConfigurationContractTests(unittest.TestCase):
    def test_launcher_pins_the_150k_three_x_mode(self) -> None:
        # The module defaults remain a safe opt-in fallback for manual runs;
        # production is pinned by run_tick.sh and the activation manifest.
        self.assertTrue(hasattr(trade_bot, "TARGET_POSITION_RUB"))
        self.assertTrue(hasattr(trade_bot, "MAX_LEVERAGE"))

        launcher = (PROJECT_DIR / "scripts/run_tick.sh").read_text(encoding="utf-8")
        self.assertRegex(
            launcher,
            re.compile(r"^export BOT_TARGET_POSITION_RUB=150000(?:\.0)?\s*(?:#.*)?$", re.M),
        )
        self.assertRegex(
            launcher,
            re.compile(r"^export BOT_MAX_LEVERAGE=3(?:\.0)?\s*(?:#.*)?$", re.M),
        )


class BrokerMaxLotsAdapterTests(unittest.TestCase):
    class FakeBot:
        account_id = "account-test"

        def __init__(self, response: dict) -> None:
            self.response = response
            self.calls: list[tuple[str, dict]] = []

        def _post(self, method: str, payload: dict) -> dict:
            self.calls.append((method, payload))
            return self.response

    def test_get_max_lots_uses_margin_limit_for_each_strategy_direction(self) -> None:
        method = getattr(trade_bot.TradeBot, "max_order_lots", None)
        self.assertIsNotNone(
            method,
            "TradeBot.max_order_lots(uid, direction, price) is required",
        )

        cases = (
            (
                "long",
                {
                    "buyLimits": {"buyMaxLots": "50"},
                    "buyMarginLimits": {"buyMaxLots": "147"},
                    "sellLimits": {"sellMaxLots": "0"},
                    "sellMarginLimits": {"sellMaxLots": "83"},
                },
                147,
            ),
            (
                "short",
                {
                    "buyLimits": {"buyMaxLots": "50"},
                    "buyMarginLimits": {"buyMaxLots": "147"},
                    "sellLimits": {"sellMaxLots": "0"},
                    "sellMarginLimits": {"sellMaxLots": "83"},
                },
                83,
            ),
        )
        for direction, response, expected in cases:
            with self.subTest(direction=direction):
                bot = self.FakeBot(response)
                lots = method(bot, "uid-test", direction, 100.25)

                self.assertEqual(lots, expected)
                self.assertEqual(len(bot.calls), 1)
                api_method, payload = bot.calls[0]
                self.assertEqual(api_method, "OrdersService/GetMaxLots")
                self.assertEqual(payload["accountId"], "account-test")
                self.assertEqual(payload["instrumentId"], "uid-test")
                self.assertEqual(payload["price"], {"units": "100", "nano": 250_000_000})


class EngineAPositionPreflightTests(unittest.TestCase):
    class FakeBot:
        def __init__(self, broker_max_lots: int = 999, max_lots_error=None) -> None:
            self.broker_max_lots = broker_max_lots
            self.max_lots_error = max_lots_error
            self.max_lots_calls: list[tuple[str, str, float]] = []
            self.place_limit = Mock(
                side_effect=AssertionError("preflight must never submit an order")
            )

        @staticmethod
        def share_info(symbol: str) -> dict:
            return {
                "uid": "uid-test",
                "lot": 10,
                "min_price_increment": 0.01,
                "api_trade_available": True,
                "short_enabled": True,
            }

        @staticmethod
        def trading_status(uid: str) -> dict:
            return {
                "status": "SECURITY_TRADING_STATUS_NORMAL_TRADING",
                "market_order_available": True,
                "limit_order_available": True,
            }

        @staticmethod
        def last_price(uid: str) -> float:
            return 100.0

        def max_order_lots(self, uid: str, direction: str, price: float) -> int:
            self.max_lots_calls.append((uid, direction, price))
            if self.max_lots_error is not None:
                raise self.max_lots_error
            return self.broker_max_lots

    def preflight(
        self,
        *,
        direction: str = "long",
        cash: float = 50_000.0,
        broker_max_lots: int = 999,
        target: float = 150_000.0,
        max_lots_error=None,
    ) -> tuple[dict, "EngineAPositionPreflightTests.FakeBot"]:
        bot = self.FakeBot(broker_max_lots, max_lots_error)
        with (
            patch.object(trade_bot, "TARGET_POSITION_RUB", target, create=True),
            patch.object(trade_bot, "MAX_LEVERAGE", 3.0),
            patch.object(trade_bot, "CONF_SIZING", False),
        ):
            plan = trade_bot.preflight_engine_a_candidate(
                bot,
                candidate(direction),
                lambda: (cash, cash),
            )
        return plan, bot

    def test_target_is_capped_by_cash_times_leverage_then_by_broker_max_lots(self) -> None:
        plan, bot = self.preflight(cash=50_000.0, broker_max_lots=120)

        self.assertTrue(plan["ready"])
        self.assertEqual(plan["target_position_rub"], 150_000.0)
        self.assertEqual(plan["desired_position_rub"], 150_000.0)
        self.assertEqual(plan["lot_cost"], 1_000.0)
        self.assertEqual(plan["broker_max_lots"], 120)
        self.assertEqual(plan["lots"], 120)
        self.assertEqual(plan["actual_position_rub"], 120_000.0)
        self.assertTrue(plan["confirm_margin_trade"])
        self.assertEqual(bot.max_lots_calls, [("uid-test", "long", 100.0)])
        bot.place_limit.assert_not_called()

    def test_cash_leverage_cap_is_applied_before_lot_floor(self) -> None:
        plan, _ = self.preflight(cash=40_000.0, broker_max_lots=999)

        self.assertTrue(plan["ready"])
        self.assertEqual(plan["desired_position_rub"], 120_000.0)
        self.assertEqual(plan["lots"], 120)
        self.assertEqual(plan["actual_position_rub"], 120_000.0)
        self.assertTrue(plan["confirm_margin_trade"])

    def test_long_without_borrowed_cash_does_not_request_margin_confirmation(self) -> None:
        plan, _ = self.preflight(
            direction="long",
            cash=50_000.0,
            broker_max_lots=999,
            target=30_000.0,
        )

        self.assertTrue(plan["ready"])
        self.assertEqual(plan["actual_position_rub"], 30_000.0)
        self.assertFalse(plan["confirm_margin_trade"])

    def test_short_always_freezes_margin_confirmation(self) -> None:
        plan, bot = self.preflight(direction="short", broker_max_lots=80)

        self.assertTrue(plan["ready"])
        self.assertEqual(plan["broker_max_lots"], 80)
        self.assertEqual(plan["actual_position_rub"], 80_000.0)
        self.assertTrue(plan["confirm_margin_trade"])
        self.assertEqual(bot.max_lots_calls, [("uid-test", "short", 100.0)])

    def test_get_max_lots_failure_is_retryable_and_never_places_an_order(self) -> None:
        plan, bot = self.preflight(
            max_lots_error=TimeoutError("GetMaxLots temporarily unavailable")
        )

        self.assertFalse(plan["ready"])
        self.assertTrue(plan["retry"])
        self.assertIn("GetMaxLots", plan["reason"])
        self.assertEqual(bot.max_lots_calls, [("uid-test", "long", 100.0)])
        bot.place_limit.assert_not_called()


class FrozenMarginStateTests(unittest.TestCase):
    @staticmethod
    def state(direction: str = "long", confirm: bool = True) -> dict:
        return {
            "date": "2026-07-20",
            "engine": "A",
            "symbol": "TEST",
            "uid": "uid-test",
            "direction": direction,
            "lots": 120,
            "lot_size": 10,
            "increment": 0.01,
            "reference_price": 100.0,
            "target_price": 103.0 if direction == "long" else 97.0,
            "target_position_rub": 150_000.0,
            "desired_position_rub": 150_000.0,
            "actual_position_rub": 120_000.0,
            "broker_max_lots": 120,
            "confirm_margin_trade": confirm,
        }

    class ImmediateBot:
        live = True

        def __init__(self) -> None:
            self.smart_fill_kwargs: dict | None = None

        def smart_fill(self, uid, lots, direction, increment, reference, **kwargs):
            self.smart_fill_kwargs = kwargs
            callback = kwargs["submission_intent_callback"]
            callback("request-margin", 0, reference, lots)
            return lots, reference

    def test_place_entry_uses_frozen_margin_flag_for_long_and_short(self) -> None:
        cases = (("long", True), ("short", True), ("long", False))
        for direction, expected in cases:
            with self.subTest(direction=direction, expected=expected):
                bot = self.ImmediateBot()
                writes: list[dict] = []
                state = self.state(direction, confirm=expected)
                with (
                    patch.object(trade_bot, "PULLBACK_PCT", 0.0),
                    patch.object(
                        trade_bot,
                        "upsert_trade",
                        side_effect=lambda live, day, value: writes.append(dict(value)),
                    ),
                    patch.object(trade_bot, "finalize_entry"),
                ):
                    rc = trade_bot.place_entry(
                        bot,
                        state,
                        100.0,
                        state["lots"],
                        {"uid": "uid-test", "min_price_increment": 0.01},
                    )

                self.assertEqual(rc, 0)
                self.assertEqual(
                    bot.smart_fill_kwargs["confirm_margin_trade"], expected
                )
                self.assertTrue(writes)
                frozen = writes[-1]
                self.assertEqual(frozen["target_position_rub"], 150_000.0)
                self.assertEqual(frozen["actual_position_rub"], 120_000.0)
                self.assertEqual(frozen["confirm_margin_trade"], expected)


class PendingMarginRetryTests(unittest.TestCase):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 20, 10, 0, 0, tzinfo=tz)

    @staticmethod
    def pending_state(deadline: str) -> dict:
        return {
            **FrozenMarginStateTests.state("long", confirm=True),
            "phase": "entry_pending",
            "limit_price": 100.0,
            "entry_deadline": deadline,
            "entry_order_id": "request-old",
        }

    class RetryBot:
        live = True

        def __init__(self) -> None:
            self.place_kwargs: dict | None = None

        @staticmethod
        def order_state(order_id: str) -> dict:
            return {
                "status": "EXECUTION_REPORT_STATUS_CANCELLED",
                "lots": 0,
                "avg_price": 0.0,
            }

        def place_limit(self, uid, lots, direction, price, **kwargs):
            self.place_kwargs = kwargs
            return kwargs["request_id"], {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW",
                "lotsExecuted": 0,
                "orderId": "broker-new",
            }

    def test_pending_limit_retry_uses_frozen_margin_flag(self) -> None:
        bot = self.RetryBot()
        with (
            patch.object(trade_bot, "datetime", self.FixedDatetime),
            patch.object(trade_bot, "upsert_trade"),
        ):
            rc = trade_bot.reconcile_pending(
                bot, self.pending_state("2026-07-20T11:00:00")
            )

        self.assertEqual(rc, 0)
        self.assertIsNotNone(bot.place_kwargs)
        self.assertTrue(bot.place_kwargs["confirm_margin_trade"])

    class FallbackBot:
        live = True

        def __init__(self) -> None:
            self.smart_fill_kwargs: dict | None = None
            self.cancelled: list[str] = []

        @staticmethod
        def order_state(order_id: str) -> dict:
            return {
                "status": "EXECUTION_REPORT_STATUS_CANCELLED",
                "lots": 0,
                "avg_price": 0.0,
            }

        def cancel_order(self, order_id: str, *, suppress_errors: bool = True) -> None:
            self.cancelled.append(order_id)

        @staticmethod
        def last_price(uid: str) -> float:
            return 100.0

        def smart_fill(self, uid, lots, direction, increment, reference, **kwargs):
            self.smart_fill_kwargs = kwargs
            kwargs["submission_intent_callback"](
                "request-fallback", 0, reference, lots
            )
            return lots, reference

    def test_pullback_fallback_uses_frozen_margin_flag(self) -> None:
        bot = self.FallbackBot()
        with (
            patch.object(trade_bot, "datetime", self.FixedDatetime),
            patch.object(trade_bot, "PULLBACK_FALLBACK", "market"),
            patch.object(trade_bot, "EXIT_TIME", datetime.strptime("18:35", "%H:%M").time()),
            patch.object(trade_bot, "upsert_trade"),
            patch.object(trade_bot, "finalize_entry"),
        ):
            rc = trade_bot.reconcile_pending(
                bot, self.pending_state("2026-07-20T09:00:00")
            )

        self.assertEqual(rc, 0)
        self.assertEqual(bot.cancelled, ["request-old"])
        self.assertIsNotNone(bot.smart_fill_kwargs)
        self.assertTrue(bot.smart_fill_kwargs["confirm_margin_trade"])


class MarginJournalPropagationTests(unittest.TestCase):
    class WeekdayDate(datetime):
        @classmethod
        def today(cls):
            return cls(2026, 7, 20).date()

    class FakeBot:
        live = True

        @staticmethod
        def cash_rub() -> float:
            return 50_000.0

        @staticmethod
        def estimate_order_price(uid, lots, direction, price) -> dict:
            return {
                "notional_rub": lots * 1_000.0,
                "commission_rub": 48.0,
                "fee_side_pct": 0.04,
            }

    def test_cmd_enter_copies_margin_plan_into_frozen_journal_state(self) -> None:
        item = candidate("long")
        info = {
            "uid": "uid-test",
            "lot": 10,
            "min_price_increment": 0.01,
            "api_trade_available": True,
            "short_enabled": True,
        }
        plan = {
            "symbol": item.symbol,
            "direction": item.direction,
            "ready": True,
            "retry": False,
            "reason": None,
            "info": info,
            "price": 100.0,
            "target_price": 103.0,
            "target_move_at_quote": 3.0,
            "cash_before": 50_000.0,
            "budget": 50_000.0,
            "direction_risk_multiplier": 0.5,
            "confidence_risk_multiplier": 1.0,
            "risk": 0.5,
            "position_rub": 150_000.0,
            "lot_cost": 1_000.0,
            "target_position_rub": 150_000.0,
            "desired_position_rub": 150_000.0,
            "actual_position_rub": 120_000.0,
            "desired_lots": 150,
            "broker_max_lots": 120,
            "confirm_margin_trade": True,
            "account_risk_pct": 2.4,
            "lots": 120,
        }
        place = Mock(return_value=0)
        selector_report = {
            "mode": "shadow",
            "decision": "keep",
            "decision_id": "decision-test",
            "activation_applied": False,
            "activation_fallback_applied": False,
            "live_selected_symbol": "TEST",
        }
        with (
            patch.object(trade_bot, "date", self.WeekdayDate),
            patch.object(trade_bot, "RS5_ACTIVATION_REQUESTED", False),
            patch.object(trade_bot, "load_journal", return_value=None),
            patch.object(trade_bot, "read_watchlist", return_value="watchlist"),
            patch.object(trade_bot.wbt, "analyze_watchlist", return_value=[item]),
            patch.object(trade_bot.wbt, "detect_reference_close_mismatch", return_value=None),
            patch.object(trade_bot.wbt, "detect_watchlist_mismatch", return_value=None),
            patch.object(trade_bot, "run_rs5_shadow_selector", return_value=selector_report),
            patch.object(
                trade_bot,
                "resolve_rs5_activation",
                return_value=(item, False, selector_report),
            ),
            patch.object(trade_bot, "persist_rs5_report", return_value=True),
            patch.object(trade_bot, "preflight_engine_a_candidate", return_value=plan),
            patch.object(trade_bot, "place_entry", place),
        ):
            rc = trade_bot.cmd_enter(
                self.FakeBot(),
                SimpleNamespace(generate=False, force=True, input=None),
            )

        self.assertEqual(rc, 0)
        place.assert_called_once()
        frozen = place.call_args.args[1]
        self.assertEqual(frozen["target_position_rub"], 150_000.0)
        self.assertEqual(frozen["desired_position_rub"], 150_000.0)
        self.assertEqual(frozen["actual_position_rub"], 120_000.0)
        self.assertEqual(frozen["broker_max_lots"], 120)
        self.assertTrue(frozen["confirm_margin_trade"])


if __name__ == "__main__":
    unittest.main()
