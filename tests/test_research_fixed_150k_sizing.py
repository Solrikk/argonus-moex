from __future__ import annotations

import math
import types
import unittest

from argonus.research import research_fixed_150k_sizing as research
from tests.local_artifacts import requires_local_files


@requires_local_files("data/backtests/live_rr2_t4_candidates_2025-11_2026-07.json")
class Fixed150kResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ledger_61, cls.ledger_54, cls.pinned = research.load_ledgers()

    def test_rs5_reconstruction_identity_and_replacements(self) -> None:
        self.assertEqual(len(self.ledger_61), 61)
        self.assertEqual(len(self.ledger_54), 54)
        by_date = {trade.date: trade for trade in self.ledger_54}
        for trade_date in research.RS5_VETOES:
            self.assertNotIn(trade_date, by_date)
        self.assertEqual(by_date["2026-05-04"].symbol, "RUAL")
        self.assertEqual(by_date["2026-07-01"].symbol, "CNRU")
        self.assertEqual(by_date["2026-05-04"].session_sha256, "a650402ff77eee47157e01cad75006975b1f63e42709399538a17c436ecbef0d")
        self.assertEqual(by_date["2026-07-01"].session_sha256, "7cf56af8791262662a9a09ccee2b07fd8e6e01280ce7f0815911568ecc2f514b")

    def test_published_54_trade_control(self) -> None:
        actual = research.published_rs5_control(self.ledger_54, 0.08)
        self.assertTrue(
            math.isclose(
                actual,
                research.EXPECTED_CONTROLS[
                    "published_rs5_long_0_5_short_1_return_pct"
                ],
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        )

    def test_fixed_150k_controls(self) -> None:
        kwargs = {
            "start_equity": 50_000.0,
            "position_cap": 150_000.0,
            "max_leverage": 3.0,
            "cost_pct": 0.08,
        }
        control = research.simulate_fixed_notional(self.ledger_61, **kwargs)["summary"]
        selected = research.simulate_fixed_notional(self.ledger_54, **kwargs)["summary"]
        self.assertAlmostEqual(
            control["total_return_on_start_equity_pct"],
            research.EXPECTED_CONTROLS["fixed_150k_on_50k_61_return_pct"],
            places=9,
        )
        self.assertAlmostEqual(
            selected["total_return_on_start_equity_pct"],
            research.EXPECTED_CONTROLS["fixed_150k_on_50k_54_return_pct"],
            places=9,
        )
        self.assertAlmostEqual(control["max_trade_close_drawdown_pct"], -13.459797822024978)
        self.assertAlmostEqual(selected["max_trade_close_drawdown_pct"], -10.34425508866087)

    def test_full_report_controls(self) -> None:
        args = types.SimpleNamespace(
            start_equity=50_000.0,
            position_cap=150_000.0,
            max_leverage=3.0,
            cost_pct=0.08,
        )
        report = research.build_research_report(args)
        lines = research.verify_controls(report, defaults=True)
        self.assertEqual(len(lines), len(research.EXPECTED_CONTROLS))


if __name__ == "__main__":
    unittest.main()
