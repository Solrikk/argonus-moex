import math
import unittest
from dataclasses import replace

from argonus.backtesting import backtest_live_61_trades as frozen
from argonus.research import research_0705_execution as engine
from argonus.research import research_flat_month_exit_risk as data


def candle(time, open=100.0, high=100.2, low=99.8, close=100.0):
    return frozen.Candle(time, open, high, low, close)


def trade(*candles, direction="long", target=103.0):
    return data.Trade("2026-01-05", "2026-01", "TEST", direction, target,
                      tuple(candles), "fixture", "fixture", 0.0,
                      data.FeatureSet(20, 1.0, 1.0, 0.0, 0.0))


ZERO = engine.FillModel(0.0, 0.0)


class ExecutionTests(unittest.TestCase):
    def test_entry_requires_exact_timestamp(self):
        with self.assertRaisesRegex(ValueError, "exact 07:05"):
            engine.simulate(trade(candle("07:10")), fills=ZERO)

    def test_pre_entry_range_cannot_trigger_stop(self):
        t = trade(candle("07:00", 100, 105, 90, 100), candle("07:05"), candle("18:35"))
        self.assertEqual(engine.simulate(t, fills=ZERO).reason, "time_exit_open")

    def test_deadline_does_not_read_close_or_high_low(self):
        # Both barriers are touched AFTER the deadline's opening print.
        t = trade(candle("07:05"), candle("18:35", 100, 110, 90, 105))
        r = engine.simulate(t, fills=ZERO)
        self.assertEqual(r.reason, "time_exit_open")
        self.assertEqual(r.exit_price, 100)
        self.assertEqual(r.net_return_pct, 0)

    def test_stop_gap_is_worse_than_stop_long_and_short(self):
        for direction, target, opening, expected in (("long", 103, 97, -3), ("short", 97, 103, -3)):
            with self.subTest(direction=direction):
                t = trade(candle("07:05"), candle("07:10", opening, opening + .1, opening - .1, opening),
                          direction=direction, target=target)
                r = engine.simulate(t, fills=ZERO)
                self.assertEqual(r.reason, "stop_gap")
                self.assertAlmostEqual(r.gross_return_pct, expected)

    def test_open_target_precedes_later_stop_in_same_bar(self):
        t = trade(candle("07:05"), candle("07:10", 104, 105, 98, 99))
        r = engine.simulate(t, fills=ZERO)
        self.assertEqual(r.reason, "target_at_open")
        self.assertAlmostEqual(r.gross_return_pct, 3)

    def test_ambiguous_intrabar_stop_first_both_directions(self):
        for direction, target in (("long", 103), ("short", 97)):
            r = engine.simulate(trade(candle("07:05", 100, 104, 96, 100),
                                     direction=direction, target=target), fills=ZERO)
            self.assertEqual(r.reason, "both_stop_first")
            self.assertTrue(r.ambiguous)
            self.assertAlmostEqual(r.gross_return_pct, -1)

    def test_breakeven_only_active_next_bar(self):
        t = trade(candle("07:05", 100, 101.5, 99.5, 101.2),
                  candle("07:10", 101.2, 101.3, 99.9, 100.5))
        r = engine.simulate(t, engine.Policy("be", breakeven_trigger_pct=1), ZERO)
        self.assertEqual(r.exit_time, "07:10")
        self.assertEqual(r.exit_price, 100)

    def test_breakeven_gap_does_not_guarantee_zero_loss(self):
        t = trade(candle("07:05", 100, 101.5, 99.5, 101.2),
                  candle("07:10", 99, 100, 98.5, 99))
        r = engine.simulate(t, engine.Policy("be", breakeven_trigger_pct=1), ZERO)
        self.assertEqual(r.reason, "stop_gap")
        self.assertAlmostEqual(r.gross_return_pct, -1)

    def test_missing_deadline_does_not_fabricate_exit(self):
        with self.assertRaisesRegex(ValueError, "Missing exit candle"):
            engine.simulate(trade(candle("07:05"), candle("18:30")), fills=ZERO)

    def test_missing_deadline_uses_next_available_open(self):
        t = trade(candle("07:05"), candle("17:05", 100.5, 101, 100, 101))
        r = engine.simulate(t, engine.Policy("early", time_exit="17:00"), ZERO)
        self.assertEqual(r.exit_price, 100.5)
        self.assertEqual(r.exit_time, "17:05")

    def test_adverse_slippage_both_sides_both_directions(self):
        for direction, target in (("long", 103), ("short", 97)):
            t = trade(candle("07:05"), candle("18:35"), direction=direction, target=target)
            r = engine.simulate(t, fills=engine.FillModel(.04, 10))
            sign = 1 if direction == "long" else -1
            self.assertAlmostEqual(r.entry_price, 100 * (1 + sign * .001))
            self.assertAlmostEqual(r.exit_price, 100 * (1 - sign * .001))
            self.assertAlmostEqual(r.net_return_pct,
                                   sign * (r.exit_price / r.entry_price - 1) * 100
                                   - .04 * (1 + r.exit_price / r.entry_price))
            self.assertLess(r.net_return_pct, -.27)

    def test_target_behind_entry_is_skip_with_zero_cost(self):
        r = engine.simulate(trade(candle("07:05"), target=99), fills=ZERO)
        self.assertFalse(r.traded)
        self.assertEqual(r.net_return_pct, 0)

    def test_reward_risk_uses_actual_entry(self):
        t = trade(candle("07:05"), candle("18:35"), target=102)
        r = engine.simulate(t, engine.Policy("rr", min_reward_risk=2), engine.FillModel(.04, 5))
        self.assertEqual(r.reason, "skip_reward_risk")

    def test_invalid_prices_and_duplicate_timestamps_rejected(self):
        cases = [trade(candle("07:05", high=99)),
                 trade(candle("07:05", close=math.nan)),
                 trade(candle("07:05"), candle("07:05"))]
        for t in cases:
            with self.subTest(t=t), self.assertRaises(ValueError):
                engine.simulate(t, fills=ZERO)

    def test_invalid_policy_and_fills_rejected(self):
        t = trade(candle("07:05"), candle("18:35"))
        for policy in (engine.Policy("bad", stop_pct=-1), engine.Policy("bad", time_exit="25:00"),
                       engine.Policy("bad", time_exit="17:02"),
                       engine.Policy("bad", target_multiple=math.nan)):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                engine.simulate(t, policy, ZERO)
        for value in (-1, math.inf, math.nan, 10_000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                engine.FillModel(slippage_side_bps=value)


class AccountingTests(unittest.TestCase):
    def test_rejects_out_of_order_duplicate_and_nonfinite_returns(self):
        r = engine.simulate(trade(candle("07:05"), candle("18:35")), fills=ZERO)
        for rows in ([r, r], [r, replace(r, date="2025-01-05")],
                     [replace(r, net_return_pct=math.nan)], [replace(r, exposure_fraction=1.1)]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                engine.account(rows)

    def test_wider_stop_reduces_notional_without_increasing_planned_loss(self):
        t = trade(candle("07:05", 100, 100.1, 98, 100))
        for stop, position in ((.75, 150_000), (1, 150_000), (1.25, 120_000)):
            r = engine.simulate(t, engine.Policy("risk", stop_pct=stop), ZERO)
            a = engine.account([r])
            self.assertAlmostEqual(a["per_trade"][0]["position_rub"], position)
            self.assertGreaterEqual(a["pnl_rub"], -1500.000001)

    def test_account_uses_pretrade_equity_and_continues_across_months(self):
        t = trade(candle("07:05", 100, 100.1, 98, 100))
        r = engine.simulate(t, fills=ZERO)
        r2 = replace(r, date="2026-02-02", month="2026-02")
        a = engine.account([r, r2])
        self.assertAlmostEqual(a["per_trade"][1]["position_rub"], 145_500)
        self.assertAlmostEqual(a["ending_equity_rub"], 47_045)
        self.assertAlmostEqual(a["monthly"]["2026-02"]["equity_start"], 48_500)

    def test_skips_do_not_count_as_losses(self):
        r = engine.simulate(trade(candle("07:05"), target=99), fills=ZERO)
        a = engine.account([r])
        self.assertEqual((a["trades"], a["skips"], a["losses"]), (0, 1, 0))
        self.assertEqual(a["pnl_rub"], 0)

    def test_walk_forward_test_returns_cannot_change_same_month_choice(self):
        t = trade(candle("07:05"), candle("18:35"))
        r = engine.simulate(t, fills=ZERO)
        paths = {p.name: [replace(r, policy=p.name, date=f"2026-{m:02d}-05", month=f"2026-{m:02d}",
                                 net_return_pct=1 if p.name == "target125" else 0)
                         for m in range(1, 6)] for p in engine.POLICIES}
        first = engine.walk_forward(paths)
        paths["target125"][3] = replace(paths["target125"][3], net_return_pct=-20)
        second = engine.walk_forward(paths)
        self.assertEqual(first["folds"][0]["policy"], "target125")
        self.assertEqual(first["folds"][0]["policy"], second["folds"][0]["policy"])
        self.assertEqual(second["folds"][1]["policy"], "baseline")


from tests.local_artifacts import requires_local_files


@requires_local_files("data/backtests/live_rr2_t4_candidates_2025-11_2026-07.json")
class ArchivedDataTests(unittest.TestCase):
    def test_all_frozen_sessions_replay_at_zero_and_stressed_costs(self):
        _, trades, _ = data.load_books()
        for bps in (0, 20):
            for policy in engine.POLICIES:
                rows = [engine.simulate(t, policy, engine.FillModel(.04, bps)) for t in trades]
                self.assertEqual(len(rows), 65)
                self.assertTrue(all(math.isfinite(r.net_return_pct) for r in rows))
                self.assertTrue(all(r.exposure_fraction * policy.stop_pct <= 1 + 1e-12 for r in rows))
                self.assertEqual(engine.account(rows)["trades"] + engine.account(rows)["skips"], 65)


if __name__ == "__main__":
    unittest.main()
