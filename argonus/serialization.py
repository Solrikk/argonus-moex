"""Read existing research pickles after moving their Python modules.

Only historical first-party module names are remapped. Third-party classes and
new qualified module names retain Python's normal pickle behavior.
"""
import io
import pickle

LEGACY_MODULES = {
    "FLAT_MONTH_SECOND_ENGINE_AUDIT_2026-07-18": "argonus.research.flat_month_second_engine_audit",
    "FLAT_MONTH_SELECTOR_WALKFORWARD_2026-07-18": "argonus.research.flat_month_selector_walkforward",
    "analyze_miss_days": "argonus.research.analyze_miss_days",
    "analyze_t1_days": "argonus.research.analyze_t1_days",
    "analyze_target_hit_levels": "argonus.research.analyze_target_hit_levels",
    "backtest_generated_watchlists": "argonus.backtesting.backtest_generated_watchlists",
    "backtest_live_61_trades": "argonus.backtesting.backtest_live_61_trades",
    "backtest_opening_all_months": "argonus.backtesting.backtest_opening_all_months",
    "capture_delayed_entry_0705": "argonus.shadow.capture_delayed_entry_0705",
    "continuous_intraday": "argonus.strategies.continuous_intraday",
    "continuous_opening": "argonus.strategies.continuous_opening",
    "delayed_entry_shadow": "argonus.shadow.delayed_entry_shadow",
    "evaluate_delayed_entry_shadow": "argonus.shadow.evaluate_delayed_entry_shadow",
    "evaluate_forward_shadows": "argonus.shadow.evaluate_forward_shadows",
    "fetch_moex_research": "argonus.market_data.fetch_moex_research",
    "forward_shadow_report": "argonus.shadow.forward_shadow_report",
    "forward_shadow_research": "argonus.shadow.forward_shadow_research",
    "generate_watchlist": "argonus.watchlists.generate_watchlist",
    "intraday_signals": "argonus.strategies.intraday_signals",
    "intraday_universe.build_features": "argonus.research.universe.build_features",
    "intraday_universe.fetch_five_min": "argonus.research.universe.fetch_five_min",
    "intraday_universe.study1": "argonus.research.universe.study1",
    "intraday_universe.universe_dailies": "argonus.research.universe.universe_dailies",
    "opening_candidate_paper": "argonus.research.opening_candidate_paper",
    "opening_market_data": "argonus.market_data.opening_market_data",
    "opening_profit_model": "argonus.models.opening_profit_model",
    "production_entry_0705": "argonus.trading.production_entry_0705",
    "production_opening": "argonus.trading.production_opening",
    "production_profit_target": "argonus.trading.production_profit_target",
    "profit_first_profile": "argonus.strategies.profit_first_profile",
    "research_0705_execution": "argonus.research.research_0705_execution",
    "research_continuous_opening": "argonus.research.research_continuous_opening",
    "research_dynamic_exit_management": "argonus.research.research_dynamic_exit_management",
    "research_dynamic_risk_frontier": "argonus.research.research_dynamic_risk_frontier",
    "research_entry_timing": "argonus.research.research_entry_timing",
    "research_fixed_150k_sizing": "argonus.research.research_fixed_150k_sizing",
    "research_flat_month_attribution": "argonus.research.research_flat_month_attribution",
    "research_flat_month_exit_risk": "argonus.research.research_flat_month_exit_risk",
    "research_intraday_learning": "argonus.research.research_intraday_learning",
    "research_intraday_portfolio": "argonus.research.research_intraday_portfolio",
    "research_inverted_direction": "argonus.research.research_inverted_direction",
    "research_new_period_engine_a": "argonus.research.research_new_period_engine_a",
    "research_october_engine_a_rs5": "argonus.research.research_october_engine_a_rs5",
    "research_opening_consensus": "argonus.research.research_opening_consensus",
    "research_opening_expectancy": "argonus.research.research_opening_expectancy",
    "research_profit_first": "argonus.research.research_profit_first",
    "research_scanner_expectancy": "argonus.research.research_scanner_expectancy",
    "research_scanner_v2": "argonus.research.research_scanner_v2",
    "research_selector_consensus_expanding": "argonus.research.research_selector_consensus_expanding",
    "research_september_2025": "argonus.research.research_september_2025",
    "research_signal_direction": "argonus.research.research_signal_direction",
    "research_win_rate_60": "argonus.research.research_win_rate_60",
    "run_candidate_loop": "argonus.runtime.run_candidate_loop",
    "scanner_v2_paper": "argonus.research.scanner_v2_paper",
    "selector_stack_ablation": "argonus.research.selector_stack_ablation",
    "shadow_rs5_selector": "argonus.watchlists.shadow_rs5_selector",
    "simulate_intraday_watchlist_strategy": "argonus.backtesting.simulate_intraday_watchlist_strategy",
    "tbank_market_data": "argonus.market_data.tbank_market_data",
    "trade_bot": "argonus.trading.trade_bot",
    "train_runner_day_confidence": "argonus.training.train_runner_day_confidence",
    "train_same_day_top_reranker": "argonus.training.train_same_day_top_reranker",
    "train_universe_runner": "argonus.training.train_universe_runner",
    "universe_backup_pick": "argonus.watchlists.universe_backup_pick",
    "watchlist_best_target": "argonus.watchlists.watchlist_best_target",
    "win_rate_policy": "argonus.strategies.win_rate_policy"
}


class ResearchUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        return super().find_class(LEGACY_MODULES.get(module, module), name)


def load_pickle_bytes(data):
    return ResearchUnpickler(io.BytesIO(data)).load()
