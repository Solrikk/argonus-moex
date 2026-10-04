import unittest

from argonus.backtesting import backtest_live_61_trades as frozen
from argonus.research import research_entry_timing as timing
from argonus.research import research_flat_month_exit_risk as base
from tests.local_artifacts import requires_local_files


def make_trade(
    candles: tuple[frozen.Candle, ...],
    *,
    direction: str = "long",
    target: float = 103.0,
) -> base.Trade:
    return base.Trade(
        date="2026-01-01",
        month="2026-01",
        symbol="TEST",
        direction=direction,
        target_price=target,
        candles=candles,
        source="unit_fixture",
        session_sha256="fixture",
        expected_gross_return_pct=0.0,
        features=base.FeatureSet(
            prior_daily_rows=20,
            range20_pct=1.0,
            vol_ratio_5_20=1.0,
            regime_strength_pct=0.0,
            regime_strength_z=0.0,
        ),
    )


class EntryTimingSimulationTests(unittest.TestCase):
    def test_delayed_policy_ignores_pre_entry_path_and_resets_stop(self) -> None:
        trade = make_trade(
            (
                frozen.Candle("07:00", 100.0, 100.5, 98.0, 99.0),
                frozen.Candle("07:05", 100.0, 101.0, 99.5, 100.5),
                frozen.Candle("07:10", 100.5, 103.2, 100.0, 103.0),
            )
        )

        control = timing.simulate_entry(trade, "07:00")
        candidate = timing.simulate_entry(trade, "07:05")

        self.assertEqual(control.exit_reason, "stop")
        self.assertEqual(candidate.entry_time, "07:05")
        self.assertEqual(candidate.stop_price, 99.0)
        self.assertEqual(candidate.exit_reason, "target")
        self.assertAlmostEqual(candidate.net_return_pct, 2.92)

    def test_entry_candle_is_stop_first_when_stop_and_target_both_hit(self) -> None:
        trade = make_trade(
            (
                frozen.Candle("07:00", 100.0, 100.2, 99.8, 100.0),
                frozen.Candle("07:05", 100.0, 103.2, 98.8, 101.0),
            )
        )

        result = timing.simulate_entry(trade, "07:05")

        self.assertTrue(result.ambiguous)
        self.assertEqual(result.exit_reason, "both_stop_first")
        self.assertAlmostEqual(result.gross_return_pct, -1.0)

    def test_target_behind_delayed_entry_is_rejected(self) -> None:
        trade = make_trade(
            (
                frozen.Candle("07:00", 100.0, 100.2, 99.8, 100.0),
                frozen.Candle("07:05", 104.0, 104.2, 103.8, 104.0),
            )
        )

        with self.assertRaisesRegex(RuntimeError, "target is behind"):
            timing.simulate_entry(trade, "07:05")


@requires_local_files("data/backtests/live_rr2_t4_candidates_2025-11_2026-07.json")
class FrozenLedgerControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.trades54, cls.trades65, _ = base.load_books()

    def test_control_replays_baseline_trade_by_trade(self) -> None:
        for trade in self.trades65:
            with self.subTest(date=trade.date, symbol=trade.symbol):
                result = timing.simulate_entry(trade, timing.CONTROL_ENTRY)
                control = base.simulate_exit(trade, base.baseline_rule())
                self.assertAlmostEqual(
                    result.gross_return_pct, control.gross_return_pct
                )
                self.assertAlmostEqual(result.net_return_pct, control.net_return_pct)
                self.assertEqual(result.exit_reason, control.exit_reason)
                self.assertEqual(result.exit_time, control.exit_time)
                self.assertEqual(result.ambiguous, control.ambiguous)

    def test_candidate_timestamp_exists_exactly_in_every_session(self) -> None:
        for trade in self.trades65:
            with self.subTest(date=trade.date, symbol=trade.symbol):
                result = timing.simulate_entry(trade, timing.CANDIDATE_ENTRY)
                self.assertEqual(result.entry_time, timing.CANDIDATE_ENTRY)


if __name__ == "__main__":
    unittest.main()
