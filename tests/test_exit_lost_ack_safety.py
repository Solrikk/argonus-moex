#!/usr/bin/env python3
"""P1 regression contract for recoverable close-order submission."""
from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import Mock, patch

from argonus.trading import trade_bot


def entered_trade() -> dict:
    return {
        "date": date.today().isoformat(),
        "engine": "A",
        "phase": "entered",
        "symbol": "TEST",
        "uid": "uid-test",
        "direction": "long",
        "lots": 5,
        "lot_size": 10,
        "increment": 0.01,
        "entry_price": 100.0,
        "reference_price": 100.0,
        "stop_price": 99.0,
        "target_price": 103.0,
        "stop_order_id": "stop-test",
        "take_order_id": "take-test",
    }


def long_position(lots: int = 5) -> dict:
    return {
        "figi": "figi-test",
        "instrumentUid": "uid-test",
        "instrumentType": "share",
        "quantity": {"units": str(lots * 10), "nano": 0},
        "quantityLots": {"units": str(lots), "nano": 0},
        "currentPrice": {"units": "100", "nano": 0},
    }


class ExitIntentBeforePostTests(unittest.TestCase):
    class LostLimitAckBot:
        live = True
        account_id = "account-test"
        smart_fill = trade_bot.TradeBot.smart_fill
        place_limit = trade_bot.TradeBot.place_limit

        def __init__(self, writes: list[dict]) -> None:
            self.writes = writes
            self.post_payloads: list[dict] = []

        @staticmethod
        def last_price(uid: str) -> float:
            return 100.0

        @staticmethod
        def order_book_touch(uid: str) -> tuple[float, float]:
            return 99.0, 100.0

        def _post(self, method: str, payload: dict) -> dict:
            if method != "OrdersService/PostOrder":
                raise AssertionError(f"unexpected API mutation: {method}")
            request_id = payload["orderId"]
            self.assert_exit_intent_is_durable(request_id, expected_stage=1)
            self.post_payloads.append(dict(payload))
            raise TimeoutError("limit PostOrder ACK lost")

        def assert_exit_intent_is_durable(
            self, request_id: str, *, expected_stage: int
        ) -> None:
            if not self.writes:
                raise AssertionError("exit_submitting must be written before PostOrder")
            frozen = self.writes[-1]
            if frozen.get("phase") != "exit_submitting":
                raise AssertionError("exit phase was not frozen before PostOrder")
            if frozen.get("exit_order_id") != request_id:
                raise AssertionError("journal and PostOrder request ids differ")
            if frozen.get("exit_submit_stage") != expected_stage:
                raise AssertionError("wrong frozen smart-fill stage")

    def test_limit_exit_intent_is_durable_with_exact_id_before_post(self) -> None:
        writes: list[dict] = []
        bot = self.LostLimitAckBot(writes)
        trade = entered_trade()
        with (
            patch.object(trade_bot, "cancel_trade_protection"),
            patch.object(
                trade_bot,
                "upsert_trade",
                side_effect=lambda live, day, value: writes.append(dict(value)),
            ),
        ):
            with self.assertRaises(trade_bot.OrderSubmissionUncertain) as caught:
                trade_bot.close_trade_position(
                    bot, trade, long_position(), "forced close"
                )

        self.assertEqual(len(bot.post_payloads), 1)
        first_request_id = bot.post_payloads[0]["orderId"]
        self.assertEqual(caught.exception.request_id, first_request_id)
        self.assertEqual(writes[-1]["exit_order_id"], first_request_id)
        self.assertEqual(writes[-1]["exit_direction"], "sell")
        self.assertEqual(writes[-1]["exit_submit_lots"], 5)

    class LostMarketAckBot:
        live = True
        account_id = "account-test"
        smart_fill = trade_bot.TradeBot.smart_fill
        place_limit = trade_bot.TradeBot.place_limit
        market_order = trade_bot.TradeBot.market_order

        def __init__(self, writes: list[dict]) -> None:
            self.writes = writes
            self.post_payloads: list[dict] = []
            self.terminal_states: dict[str, dict] = {}
            self.cancel_calls: list[tuple[str, bool]] = []

        @staticmethod
        def last_price(uid: str) -> float:
            return 100.0

        @staticmethod
        def order_book_touch(uid: str) -> tuple[float, float]:
            return 99.0, 100.0

        def _post(self, method: str, payload: dict) -> dict:
            if method != "OrdersService/PostOrder":
                raise AssertionError(f"unexpected API mutation: {method}")
            request_id = payload["orderId"]
            expected_stage = 4 if payload["orderType"] == "ORDER_TYPE_MARKET" else len(self.post_payloads) + 1
            frozen = self.writes[-1] if self.writes else {}
            if (
                frozen.get("phase") != "exit_submitting"
                or frozen.get("exit_order_id") != request_id
                or frozen.get("exit_submit_stage") != expected_stage
            ):
                raise AssertionError(
                    "the exact exit request must be durable before every PostOrder"
                )
            self.post_payloads.append(dict(payload))
            if payload["orderType"] == "ORDER_TYPE_MARKET":
                raise TimeoutError("market fallback ACK lost")
            self.terminal_states[request_id] = {
                "status": "EXECUTION_REPORT_STATUS_CANCELLED",
                "lots": 0,
                "avg_price": 0.0,
            }
            return {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW",
                "lotsExecuted": 0,
            }

        def _wait_fill(self, order_id: str, seconds: int) -> dict:
            return dict(self.terminal_states[order_id])

        def cancel_order(
            self, order_id: str, *, suppress_errors: bool = True
        ) -> None:
            self.cancel_calls.append((order_id, suppress_errors))

        def order_state(self, order_id: str) -> dict:
            return dict(self.terminal_states[order_id])

    def test_market_fallback_intent_is_durable_before_market_post(self) -> None:
        writes: list[dict] = []
        bot = self.LostMarketAckBot(writes)
        with (
            patch.object(trade_bot, "cancel_trade_protection"),
            patch.object(
                trade_bot,
                "upsert_trade",
                side_effect=lambda live, day, value: writes.append(dict(value)),
            ),
        ):
            with self.assertRaises(trade_bot.OrderSubmissionUncertain) as caught:
                trade_bot.close_trade_position(
                    bot, entered_trade(), long_position(), "forced close"
                )

        self.assertEqual(
            [payload["orderType"] for payload in bot.post_payloads],
            ["ORDER_TYPE_LIMIT"] * 3 + ["ORDER_TYPE_MARKET"],
        )
        market_request_id = bot.post_payloads[-1]["orderId"]
        self.assertEqual(caught.exception.request_id, market_request_id)
        self.assertEqual(writes[-1]["phase"], "exit_submitting")
        self.assertEqual(writes[-1]["exit_submit_stage"], 4)
        self.assertEqual(writes[-1]["exit_order_id"], market_request_id)
        self.assertEqual(
            bot.cancel_calls,
            [(payload["orderId"], False) for payload in bot.post_payloads[:3]],
        )


class ExitSubmissionReconcileTests(unittest.TestCase):
    @staticmethod
    def exit_state() -> dict:
        return {
            **entered_trade(),
            "phase": "exit_submitting",
            "exit_order_id": "exit-request-frozen",
            "exit_submit_stage": 1,
            "exit_submit_price": 100.0,
            "exit_submit_lots": 5,
            "exit_position_lots_before": 5,
            "exit_direction": "sell",
            "exit_note": "forced close",
        }

    class NonterminalBot:
        live = True

        def __init__(self) -> None:
            self.order_calls: list[str] = []
            self.cancel_calls: list[tuple[str, bool]] = []

        def order_state(self, request_id: str) -> dict:
            self.order_calls.append(request_id)
            return {
                "status": "EXECUTION_REPORT_STATUS_NEW",
                "lots": 0,
                "avg_price": 0.0,
            }

        def cancel_order(
            self, request_id: str, *, suppress_errors: bool = True
        ) -> None:
            self.cancel_calls.append((request_id, suppress_errors))

        @staticmethod
        def share_positions():
            raise AssertionError("nonterminal exit must block before portfolio handling")

    def test_nonterminal_exit_remains_frozen_and_blocks_duplicate_close(self) -> None:
        bot = self.NonterminalBot()
        writes: list[dict] = []
        with (
            patch.object(
                trade_bot,
                "upsert_trade",
                side_effect=lambda live, day, value: writes.append(dict(value)),
            ),
            patch.object(trade_bot, "mark_trade_closed") as mark_closed,
        ):
            rc = trade_bot.reconcile_exit_submitting(bot, self.exit_state())

        self.assertEqual(rc, 1)
        self.assertEqual(bot.order_calls, ["exit-request-frozen"] * 2)
        self.assertEqual(bot.cancel_calls, [("exit-request-frozen", False)])
        self.assertEqual(writes, [])
        mark_closed.assert_not_called()

    class TerminalBot:
        live = True

        def __init__(self, *, lots: int, positions: list[dict]) -> None:
            self.lots = lots
            self.positions = positions
            self.order_calls: list[str] = []

        def order_state(self, request_id: str) -> dict:
            self.order_calls.append(request_id)
            return {
                "status": "EXECUTION_REPORT_STATUS_FILL"
                if self.lots
                else "EXECUTION_REPORT_STATUS_CANCELLED",
                "lots": self.lots,
                "avg_price": 100.0 if self.lots else 0.0,
            }

        def share_positions(self) -> list[dict]:
            return list(self.positions)

    def test_terminal_zero_fill_restores_entered_and_protection(self) -> None:
        bot = self.TerminalBot(lots=0, positions=[long_position()])
        writes: list[dict] = []
        repair = Mock()
        with (
            patch.object(
                trade_bot,
                "upsert_trade",
                side_effect=lambda live, day, value: writes.append(dict(value)),
            ),
            patch.object(trade_bot, "repair_protection", repair),
            patch.object(trade_bot, "mark_trade_closed") as mark_closed,
        ):
            rc = trade_bot.reconcile_exit_submitting(bot, self.exit_state())

        self.assertEqual(rc, 0)
        self.assertEqual(len(writes), 1)
        restored = writes[0]
        self.assertEqual(restored["phase"], "entered")
        self.assertEqual(restored["lots"], 5)
        self.assertFalse(any(key.startswith("exit_") for key in restored))
        repair.assert_called_once_with(bot, restored)
        mark_closed.assert_not_called()

    def test_terminal_fill_with_no_position_marks_trade_closed(self) -> None:
        bot = self.TerminalBot(lots=5, positions=[])
        mark_closed = Mock()
        with (
            patch.object(trade_bot, "mark_trade_closed", mark_closed),
            patch.object(
                trade_bot,
                "upsert_trade",
                side_effect=AssertionError("a fully closed exit must not restore entered"),
            ),
            patch.object(
                trade_bot,
                "repair_protection",
                side_effect=AssertionError("a closed position needs no protection"),
            ),
        ):
            state = self.exit_state()
            rc = trade_bot.reconcile_exit_submitting(bot, state)

        self.assertEqual(rc, 0)
        mark_closed.assert_called_once_with(bot, state, "forced close")


class PartialMarketExitTests(unittest.TestCase):
    class Bot:
        live = True
        smart_fill = trade_bot.TradeBot.smart_fill

        def __init__(self) -> None:
            self.market_request_id: str | None = None
            self.cancelled_limits: set[str] = set()

        @staticmethod
        def last_price(uid: str) -> float:
            return 100.0

        @staticmethod
        def order_book_touch(uid: str) -> tuple[float, float]:
            return 99.0, 100.0

        def place_limit(
            self,
            uid: str,
            lots: int,
            direction: str,
            price: float,
            *,
            confirm_margin_trade: bool = False,
            request_id: str | None = None,
        ) -> tuple[str, dict]:
            return request_id, {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW",
                "lotsExecuted": 0,
            }

        @staticmethod
        def _wait_fill(order_id: str, seconds: int) -> dict:
            return {
                "status": "EXECUTION_REPORT_STATUS_CANCELLED",
                "lots": 0,
                "avg_price": 0.0,
            }

        def cancel_order(
            self, order_id: str, *, suppress_errors: bool = True
        ) -> None:
            self.cancelled_limits.add(order_id)

        def market_order(
            self,
            uid: str,
            lots: int,
            direction: str,
            *,
            request_id: str | None = None,
        ) -> dict:
            self.market_request_id = request_id
            return {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
                "lotsExecuted": 2,
                "executedOrderPrice": trade_bot.float_to_quotation(100.5),
            }

        def order_state(self, order_id: str) -> dict:
            if order_id == self.market_request_id:
                return {
                    "status": "EXECUTION_REPORT_STATUS_CANCELLED",
                    "lots": 2,
                    "avg_price": 100.5,
                }
            return {
                "status": "EXECUTION_REPORT_STATUS_CANCELLED",
                "lots": 0,
                "avg_price": 0.0,
            }

    def test_partial_market_fill_restores_only_residual_position(self) -> None:
        bot = self.Bot()
        writes: list[dict] = []
        repair = Mock()
        with (
            patch.object(trade_bot, "cancel_trade_protection"),
            patch.object(
                trade_bot,
                "upsert_trade",
                side_effect=lambda live, day, value: writes.append(dict(value)),
            ),
            patch.object(trade_bot, "repair_protection", repair),
            patch.object(trade_bot, "mark_trade_closed") as mark_closed,
        ):
            trade_bot.close_trade_position(
                bot, entered_trade(), long_position(), "forced close"
            )

        residual = writes[-1]
        self.assertEqual(residual["phase"], "entered")
        self.assertEqual(residual["lots"], 3)
        repair.assert_called_once_with(bot, residual)
        mark_closed.assert_not_called()


class CmdExitRoutingTests(unittest.TestCase):
    class KnownPositionBot:
        live = True

        def __init__(self, position: dict) -> None:
            self.position = position
            self.share_position_calls = 0
            self.smart_fill = Mock(
                side_effect=AssertionError(
                    "cmd_exit must route a known position through close_trade_position"
                )
            )

        @staticmethod
        def cancel_stop_orders() -> int:
            return 0

        def share_positions(self) -> list[dict]:
            self.share_position_calls += 1
            return [self.position] if self.share_position_calls == 1 else []

        @staticmethod
        def _post(method: str, payload: dict) -> dict:
            if method != "InstrumentsService/GetInstrumentBy":
                raise AssertionError(f"unexpected API method: {method}")
            return {
                "instrument": {
                    "uid": "uid-test",
                    "ticker": "TEST",
                    "lot": 10,
                    "minPriceIncrement": {"units": "0", "nano": 10_000_000},
                }
            }

        @staticmethod
        def last_price(uid: str) -> float:
            return 100.0

        @staticmethod
        def cash_rub() -> float:
            return 50_000.0

    def test_known_position_routes_through_recoverable_close_helper(self) -> None:
        trade = entered_trade()
        journal = {"date": trade["date"], "trades": [trade]}
        bot = self.KnownPositionBot(long_position())
        close = Mock()
        with (
            patch.object(trade_bot, "load_journal", return_value=journal),
            patch.object(trade_bot, "close_trade_position", close),
        ):
            rc = trade_bot.cmd_exit(bot)

        self.assertEqual(rc, 0)
        close.assert_called_once_with(
            bot, trade, bot.position, "принудительное закрытие"
        )
        bot.smart_fill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
