#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from argonus.backtesting import backtest_live_61_trades as exact
from argonus.research import research_october_engine_a_rs5 as subject
from tests.local_artifacts import requires_local_files


def idea(*, direction="long", bullish=True, rally=None, move=2.5, skipped=None):
    return SimpleNamespace(
        skipped_reason=skipped,
        direction=direction,
        market_bullish=bullish,
        short_rally_10d_pct=rally,
        exit_target=SimpleNamespace(label="T4", price=110.0, move_pct_from_close=move),
    )


class OctoberResearchTests(unittest.TestCase):
    @requires_local_files("data/watchlists/generated_watchlists_2025_10/watchlist_1001.txt")
    def test_pinned_october_manifest_is_complete(self):
        entries, manifest = subject.discover_entries(subject.MONTH_DIR)
        self.assertEqual(len(entries), 23)
        self.assertEqual(len(manifest), 23)

    def test_manifest_gates_are_forced_not_runtime_defaults(self):
        self.assertIsNone(subject.gate_reason(idea()))
        self.assertIn("regime", subject.gate_reason(idea(direction="short", bullish=True)))
        self.assertIn("rally_guard", subject.gate_reason(idea(direction="short", bullish=False, rally=8.01)))
        self.assertIn("rr_floor", subject.gate_reason(idea(move=1.999)))
        self.assertEqual(subject.MIN_TARGET_PCT, 2.0)
        self.assertEqual(subject.RS5_THRESHOLD_PP, -2.39)

    def test_exact_execution_is_stop_first_and_costed(self):
        candidate = {"date": "2025-10-01", "month": "2025-10", "symbol": "TEST", "direction": "long", "target_label": "T4", "target_price": 102.0}
        candles = [exact.Candle("07:00", 100.0, 103.0, 98.0, 101.0)]
        config = exact.BacktestConfig(entry_time="07:00", exit_time="18:35", stop_loss_pct=1.0, fee_pct=0.08)
        result = exact.simulate_trade(candidate, candles, "test", "hash", config)
        self.assertEqual(result.exit_reason, "both_stop_first")
        self.assertAlmostEqual(result.gross_return_pct, -1.0)
        self.assertAlmostEqual(result.net_return_pct_at_full_risk, -1.08)

    def test_immutable_cache_hash_and_offline_load(self):
        body = subject._cache_body([], [], {})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            written = subject.write_immutable_cache(path, body)
            loaded = subject.load_cache(path, [])
            self.assertEqual(loaded["cache_sha256"], written["cache_sha256"])
            with self.assertRaises(RuntimeError):
                subject.write_immutable_cache(path, body)

    def test_cache_tamper_is_rejected(self):
        body = subject._cache_body([], [], {})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            subject.write_immutable_cache(path, body)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["config"]["minimum_target_pct"] = 0.0
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                subject.load_cache(path, [])

    def test_report_skips_unavailable_market_data_instead_of_booking_loss(self):
        body = subject._cache_body(
            [],
            [{"date": "2025-10-01", "decision": "keep", "decision_reason": "passes", "selected": {"rank": 1, "symbol": "TEST", "direction": "long", "target_label": "T4", "target_price": 102.0}}],
            {"2025-10-01|TEST": {"status": "unavailable_no_5m_candles", "rows": [], "session_sha256": subject.canonical_sha256([])}},
        )
        body["cache_sha256"] = subject.canonical_sha256(body)
        frozen_trade = subject.fixed_research.ResearchTrade(
            "2025-11-03", "2025-11", "TEST", "long", 0.08,
            "synthetic_exit", "unit_fixture", "synthetic_hash",
        )
        with patch.object(subject.fixed_research, "load_ledgers",
                          return_value=([frozen_trade], [frozen_trade], {})):
            report = subject.build_report(body)
        self.assertEqual(report["summary"]["executed_trades"], 0)
        self.assertEqual(report["summary"]["skips"], 1)
        self.assertEqual(report["summary"]["final_equity_rub"], 50_000.0)

    @requires_local_files("data/backtests/october_engine_a_rs5_selected_5m_cache.json",
                          "data/watchlists/generated_watchlists_2025_10/watchlist_1001.txt",
                          "data/backtests/live_rr2_t4_candidates_2025-11_2026-07.json")
    def test_real_offline_cache_reproduces_october_and_hybrid_controls(self):
        _, manifest = subject.discover_entries(subject.MONTH_DIR)
        cache = subject.load_cache(subject.CACHE_PATH, manifest)
        report = subject.build_report(cache)
        self.assertEqual(report["summary"]["executed_trades"], 11)
        self.assertEqual((report["summary"]["wins"], report["summary"]["losses"]), (5, 6))
        self.assertAlmostEqual(report["summary"]["total_return_on_equity_pct"], 6.763775711426856)
        hybrid = report["table_convention"]["hybrid_october_current_plus_frozen_rs5_nov_jul"]
        self.assertEqual(hybrid["trades"], 65)
        self.assertAlmostEqual(hybrid["return_pct"], 54.27745462958335)
        fixed = report["fixed_150k_on_50k_hybrid_oct_to_jul"]["summary"]
        self.assertAlmostEqual(fixed["final_equity"], 115_649.2055602503)


if __name__ == "__main__":
    unittest.main()
