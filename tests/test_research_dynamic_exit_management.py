from __future__ import annotations

import unittest

from argonus.backtesting import backtest_live_61_trades as frozen
from argonus.research import research_dynamic_exit_management as research
from argonus.research import research_flat_month_exit_risk as base


def trade(*candles: frozen.Candle) -> base.Trade:
    return base.Trade(
        date="2026-01-01",
        month="2026-01",
        symbol="TEST",
        direction="long",
        target_price=104.0,
        candles=tuple(candles),
        source="unit_test",
        session_sha256="0" * 64,
        expected_gross_return_pct=0.0,
        features=base.FeatureSet(20, 1.0, 1.0, 0.0, 0.0),
    )


class DynamicExitCausalityTests(unittest.TestCase):
    def test_close_ratchet_is_active_only_on_next_candle(self) -> None:
        candidate = trade(
            frozen.Candle("07:00", 100.0, 101.2, 99.5, 101.0),
            frozen.Candle("07:05", 101.0, 101.1, 99.8, 100.0),
        )
        rule = research.ManagementRule("be", ratchets=((0.25, 0.0),))

        result = research.simulate_managed_exit(candidate, rule)

        self.assertAlmostEqual(result.gross_return_pct, 0.0)
        self.assertEqual(result.last_exit_time, "07:05")
        self.assertTrue(result.events[0].startswith("stop@07:05"))

    def test_active_stop_wins_over_partial_target_on_ambiguous_bar(self) -> None:
        candidate = trade(
            frozen.Candle("07:00", 100.0, 103.5, 98.5, 102.0),
        )
        rule = research.ManagementRule(
            "partial", partial_trigger_t=0.75, partial_fraction=0.5
        )

        result = research.simulate_managed_exit(candidate, rule)

        self.assertAlmostEqual(result.gross_return_pct, -1.0)
        self.assertTrue(result.ambiguous_stop_first)
        self.assertEqual(len(result.events), 1)
        self.assertTrue(result.events[0].startswith("both_stop_first@07:00"))

    def test_partial_then_breakeven_applies_to_remaining_position_next_bar(self) -> None:
        candidate = trade(
            frozen.Candle("07:00", 100.0, 103.1, 99.5, 102.0),
            frozen.Candle("07:05", 102.0, 102.1, 99.5, 100.0),
        )
        rule = research.ManagementRule(
            "partial_be",
            partial_trigger_t=0.75,
            partial_fraction=0.5,
            partial_then_breakeven=True,
        )

        result = research.simulate_managed_exit(candidate, rule)

        self.assertAlmostEqual(result.gross_return_pct, 1.5)
        self.assertAlmostEqual(result.net_return_pct, 1.42)
        self.assertEqual(len(result.events), 2)
        self.assertTrue(result.events[0].startswith("partial_target@07:00"))
        self.assertTrue(result.events[1].startswith("stop@07:05"))

    def test_checkpoint_uses_observed_close(self) -> None:
        candidate = trade(
            frozen.Candle("07:00", 100.0, 100.5, 99.5, 100.2),
            frozen.Candle("12:00", 100.2, 100.4, 99.7, 99.8),
            frozen.Candle("12:05", 99.7, 100.0, 99.6, 99.9),
        )
        rule = research.ManagementRule(
            "checkpoint",
            checkpoint_time="12:00",
            checkpoint_max_progress_t=0.0,
            checkpoint_exit_fraction=1.0,
        )

        result = research.simulate_managed_exit(candidate, rule)

        self.assertAlmostEqual(result.gross_return_pct, -0.3)
        self.assertEqual(result.last_exit_time, "12:05")
        self.assertTrue(
            result.events[0].startswith("checkpoint_exit_next_open@12:05")
        )


if __name__ == "__main__":
    unittest.main()
