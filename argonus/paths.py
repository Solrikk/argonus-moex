"""Shared project locations, independent of the current working directory."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
BACKTEST_DIR = DATA_DIR / "backtests"
WATCHLIST_DIR = DATA_DIR / "watchlists"
UNIVERSE_DIR = DATA_DIR / "intraday_universe"
MODEL_DIR = PROJECT_ROOT / "models"
RUNTIME_DIR = PROJECT_ROOT / "runtime"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
