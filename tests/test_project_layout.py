"""Regression checks for relocated resources and historical pickle readers."""
import os
import pickle
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from argonus.backtesting.backtest_generated_watchlists import discover_watchlists
from argonus.market_data import tbank_market_data
from argonus.paths import MODEL_DIR, PROJECT_ROOT, WATCHLIST_DIR
from argonus.serialization import load_pickle_bytes
from argonus.strategies.continuous_intraday import Opportunity
from argonus.strategies.profit_first_profile import PROFILE
from argonus.trading import trade_bot
from argonus.watchlists import watchlist_best_target


class ProjectLayoutTests(unittest.TestCase):
    def test_resources_resolve_when_working_directory_is_outside_project(self):
        previous = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                os.chdir(temporary)
                self.assertIn(str(PROJECT_ROOT / ".tbank_token"),
                              tbank_market_data._iter_token_file_candidates())
                self.assertEqual(Path(trade_bot.STATE_PATH), PROJECT_ROOT / "runtime/bot_state.json")
                self.assertEqual(Path(trade_bot.WATCHLIST_PATH), WATCHLIST_DIR / "watchlist.txt")
                self.assertTrue(PROFILE.is_absolute())
                self.assertTrue(PROFILE.is_relative_to(PROJECT_ROOT / "data/backtests"))
        finally:
            os.chdir(previous)

    @unittest.skipUnless(any(WATCHLIST_DIR.glob("generated_watchlists_*/*.txt")), "Local archived watchlists are not included")
    def test_archived_watchlists_are_discovered_in_new_data_directory(self):
        entries = discover_watchlists(str(WATCHLIST_DIR))
        self.assertGreater(len(entries), 100)
        self.assertEqual(min(entry.trade_date.year for entry in entries), 2025)
        self.assertEqual(max(entry.trade_date.year for entry in entries), 2026)

    def test_old_pickle_module_name_resolves_to_same_opportunity_class(self):
        self.assertIs(load_pickle_bytes(b"ccontinuous_intraday\nOpportunity\n."),
                      Opportunity)

    def test_new_pickle_roundtrip_preserves_opportunity(self):
        value = Opportunity("2026-10-05", "AAA", "long", "opening_follow",
                            "07:20", 100.0, 0.3, 1.0, 2.0, 10000000.0)
        self.assertEqual(load_pickle_bytes(pickle.dumps(value)), value)

    @unittest.skipUnless((MODEL_DIR / watchlist_best_target.SAME_DAY_TOP_RERANKER_MODEL_FILENAME).is_file(), "Local trained model is not included")
    def test_analyzer_loads_models_from_models_directory(self):
        with patch.object(watchlist_best_target, "_same_day_top_reranker_checked", False), \
                patch.object(watchlist_best_target, "_same_day_top_reranker_model", None):
            self.assertIsNotNone(watchlist_best_target.load_same_day_top_reranker_model())
        self.assertTrue((MODEL_DIR / watchlist_best_target.SAME_DAY_TOP_RERANKER_MODEL_FILENAME).is_file())


if __name__ == "__main__":
    unittest.main()
