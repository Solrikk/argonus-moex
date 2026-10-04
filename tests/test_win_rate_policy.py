from dataclasses import asdict
import unittest

from argonus.research import research_0705_execution as base
from tests.test_research_0705_execution import candle, trade, ZERO
from argonus.strategies.win_rate_policy import RULES, Rule, passes_filter, simulate


class WinRatePolicyTests(unittest.TestCase):
    def test_baseline_matches_existing_gap_stop_target_and_deadline(self):
        scenarios = [
            trade(candle("07:00", 100, 110, 90, 100), candle("07:05"), candle("18:35")),
            trade(candle("07:05"), candle("07:10", 97, 98, 96, 97)),
            trade(candle("07:05", 100, 104, 98, 100)),
            trade(candle("07:05"), candle("07:10", 104, 105, 98, 100)),
        ]
        for direction, target in (("long", 103), ("short", 97)):
            for t in scenarios:
                from dataclasses import replace
                t = replace(t, direction=direction, target_price=target)
                for fills in (ZERO, base.FillModel()):
                    expected = asdict(base.simulate(t, fills=fills))
                    actual = asdict(simulate(t, fills=fills))
                    for key, value in expected.items():
                        if isinstance(value, float):
                            self.assertAlmostEqual(actual[key], value, places=11)
                        else:
                            self.assertEqual(actual[key], value)

    def test_confirmed_entry_cannot_see_entry_candle_close(self):
        rule = Rule("confirm", confirmation=True)
        t = trade(candle("07:00", 100, 101, 99, 99.5),
                  candle("07:05", 100, 104, 99.1, 103))
        outcome = simulate(t, rule, ZERO)
        self.assertFalse(outcome.traded)
        self.assertEqual(outcome.reason, "skip_morning_confirmation")

    def test_delayed_entry_ignores_all_prior_barrier_touches(self):
        t = trade(candle("07:05", 100, 105, 90, 100),
                  candle("07:15", 100, 100.1, 99.9, 100), candle("18:35"))
        outcome = simulate(t, Rule("late", entry_time="07:15"), ZERO)
        self.assertEqual(outcome.entry_time, "07:15")
        self.assertEqual(outcome.reason, "time_exit_open")
        self.assertEqual(outcome.net_return_pct, 0)

    def test_partial_is_one_trade_with_exact_weighted_cost(self):
        rule = Rule("partial", partial_at_multiple=.5)
        t = trade(candle("07:05", 100, 101.6, 99.5, 101),
                  candle("07:10", 101, 103.5, 100, 103))
        fills = base.FillModel(.04, 0)
        outcome = simulate(t, rule, fills)
        self.assertEqual(len(outcome.legs), 2)
        self.assertAlmostEqual(outcome.exit_price, .5 * 101.5 + .5 * 103)
        self.assertAlmostEqual(outcome.net_return_pct, 2.25 - .04 * (1 + 1.0225))
        account = base.account([outcome])
        self.assertEqual(account["trades"], 1)
        self.assertEqual(account["wins"], 1)

    def test_partial_intrabar_ambiguity_uses_original_stop_first(self):
        t = trade(candle("07:05", 100, 101.8, 98, 101), candle("18:35"))
        outcome = simulate(t, Rule("partial", partial_at_multiple=.5), ZERO)
        self.assertEqual(len(outcome.legs), 1)
        self.assertEqual(outcome.reason, "both_stop_first")
        self.assertAlmostEqual(outcome.net_return_pct, -1)

    def test_partial_at_open_precedes_later_low(self):
        t = trade(candle("07:05", 100, 100.1, 99.9, 100),
                  candle("07:10", 102, 102.5, 98, 99))
        outcome = simulate(t, Rule("partial", partial_at_multiple=.5), ZERO)
        self.assertEqual(len(outcome.legs), 2)
        self.assertAlmostEqual(outcome.net_return_pct, .25)

    def test_partial_breakeven_activates_next_candle(self):
        t = trade(candle("07:05", 100, 101.8, 99.5, 101),
                  candle("07:10", 101, 101.2, 99.5, 100))
        outcome = simulate(t, Rule("partial_be", partial_at_multiple=.5,
                                   breakeven_after_partial=True), ZERO)
        self.assertEqual(outcome.exit_time, "07:10")
        self.assertAlmostEqual(outcome.net_return_pct, .75)

    def test_breakeven_gap_preserves_adverse_fill(self):
        t = trade(candle("07:05", 100, 101.8, 99.5, 101),
                  candle("07:10", 98, 98.5, 97.5, 98))
        outcome = simulate(t, Rule("partial_be", partial_at_multiple=.5,
                                   breakeven_after_partial=True), ZERO)
        self.assertEqual(outcome.reason, "stop_gap")
        self.assertAlmostEqual(outcome.net_return_pct, -.25)

    def test_partial_short_sign_and_costs(self):
        t = trade(candle("07:05", 100, 100.5, 98.4, 99),
                  candle("07:10", 99, 99.5, 96.5, 97), direction="short", target=97)
        outcome = simulate(t, Rule("partial", partial_at_multiple=.5), base.FillModel(.04, 0))
        self.assertAlmostEqual(outcome.exit_price, 97.75)
        self.assertAlmostEqual(outcome.net_return_pct, 2.25 - .04 * (1 + .9775))

    def test_wider_stop_keeps_original_planned_stop_loss(self):
        t = trade(candle("07:05", 100, 100.1, 98, 99))
        outcome = simulate(t, Rule("wide", stop_pct=1.5), ZERO)
        self.assertAlmostEqual(outcome.exposure_fraction, 2 / 3)
        self.assertAlmostEqual(outcome.net_return_pct * outcome.exposure_fraction, -1)

    def test_missing_features_do_not_imply_passing_filter(self):
        self.assertFalse(passes_filter(Rule("rs5", feature_filter="rs5_nonnegative"), {}))
        self.assertFalse(passes_filter(Rule("rs5", feature_filter="rs5_nonnegative"), {"aligned_rs5_pp": float("nan")}))

    def test_missing_entry_data_is_explicit_failure(self):
        with self.assertRaisesRegex(ValueError, "Missing exact 09:05"):
            simulate(trade(candle("07:05"), candle("18:35")), Rule("late", entry_time="09:05"))

    def test_registry_has_only_declared_unique_rules(self):
        self.assertEqual(len(RULES), 29)
        self.assertEqual(len({rule.name for rule in RULES}), 29)


if __name__ == "__main__":
    unittest.main()
