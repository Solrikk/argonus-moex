#!/usr/bin/env python3
"""P1 regressions for cancellation certainty between smart-fill stages."""
from __future__ import annotations

import unittest

from argonus.trading import trade_bot


class SmartFillCancellationSafetyTests(unittest.TestCase):
    class FakeLadder:
        live = True
        account_id = "account-test"
        smart_fill = trade_bot.TradeBot.smart_fill
        place_limit = trade_bot.TradeBot.place_limit

        def __init__(
            self,
            *,
            post_cancel_status: str = "EXECUTION_REPORT_STATUS_NEW",
            cancel_error: Exception | None = None,
        ) -> None:
            self.post_cancel_status = post_cancel_status
            self.cancel_error = cancel_error
            self.post_payloads: list[dict] = []
            self.cancel_calls: list[tuple[str, bool]] = []
            self.order_state_calls: list[str] = []
            self.intent_request_ids: list[str] = []
            self.order_book_calls = 0

        def order_book_touch(self, uid: str) -> tuple[float, float]:
            self.order_book_calls += 1
            return 99.0, 100.0

        def _post(self, method: str, payload: dict) -> dict:
            if method != "OrdersService/PostOrder":
                raise AssertionError(f"unexpected mutating API method: {method}")
            self.post_payloads.append(dict(payload))
            return {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW",
                "lotsExecuted": 0,
                "orderId": "broker-order-first",
            }

        @staticmethod
        def _wait_fill(order_id: str, seconds: int) -> dict:
            return {
                "status": "EXECUTION_REPORT_STATUS_NEW",
                "lots": 0,
                "avg_price": 0.0,
            }

        def cancel_order(
            self, order_id: str, *, suppress_errors: bool = True
        ) -> None:
            self.cancel_calls.append((order_id, suppress_errors))
            if self.cancel_error is not None:
                raise self.cancel_error

        def order_state(self, order_id: str) -> dict:
            self.order_state_calls.append(order_id)
            return {
                "status": self.post_cancel_status,
                "lots": 1 if self.post_cancel_status.endswith("PARTIALLYFILL") else 0,
                "avg_price": 100.0,
            }

        def record_intent(
            self, request_id: str, stage: int, price: float, lots: int
        ) -> None:
            self.intent_request_ids.append(request_id)

    def assert_frozen_after_uncertain_cancel(self, bot: "FakeLadder") -> None:
        with self.assertRaises(trade_bot.OrderSubmissionUncertain) as caught:
            bot.smart_fill(
                "uid-test",
                5,
                "buy",
                0.01,
                100.0,
                must_fill=False,
                submission_intent_callback=bot.record_intent,
            )

        self.assertEqual(len(bot.intent_request_ids), 1)
        first_request_id = bot.intent_request_ids[0]
        self.assertEqual(caught.exception.request_id, first_request_id)
        self.assertEqual(
            len(bot.post_payloads),
            1,
            "an uncertain cancellation must forbid a second PostOrder",
        )
        self.assertEqual(bot.post_payloads[0]["orderId"], first_request_id)
        self.assertEqual(bot.order_book_calls, 1)
        self.assertEqual(
            bot.cancel_calls,
            [(first_request_id, False)],
            "CancelOrder must be called with suppress_errors=False",
        )

    def test_cancel_error_freezes_first_request_and_stops_ladder(self) -> None:
        bot = self.FakeLadder(cancel_error=TimeoutError("CancelOrder ACK lost"))

        self.assert_frozen_after_uncertain_cancel(bot)

        self.assertEqual(
            bot.order_state_calls,
            [],
            "a failed CancelOrder has no confirmed terminal state to inspect",
        )

    def test_nonterminal_state_after_cancel_never_advances_to_second_post(self) -> None:
        for status in (
            "EXECUTION_REPORT_STATUS_NEW",
            "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
        ):
            with self.subTest(status=status):
                bot = self.FakeLadder(post_cancel_status=status)

                self.assert_frozen_after_uncertain_cancel(bot)

                self.assertEqual(bot.order_state_calls, [bot.intent_request_ids[0]])


if __name__ == "__main__":
    unittest.main()
