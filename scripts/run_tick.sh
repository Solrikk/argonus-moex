#!/usr/bin/env bash
# One Argonus tick. Add --live explicitly to enable order submission.
set -euo pipefail
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
export TZ="Europe/Moscow"
[ -f PAUSE ] && exit 0
mkdir -p "$DIR/runtime"
export BOT_ACCOUNT_NAME="${BOT_ACCOUNT_NAME:-YOUR_ACCOUNT_NAME}"
export BOT_SECOND_ENGINE=0
export BOT_CONF_SIZING=0
export BOT_PULLBACK_PCT=0
export BOT_DAY_FILTER=0
export BOT_REGIME_GATES=1
export BOT_SHORT_RALLY_GUARD_PCT=8.0
export BOT_RISK_PCT=1.0
export BOT_TARGET_POSITION_RUB=150000
export BOT_MAX_LEVERAGE=3.0
export BOT_LONG_RISK_MULTIPLIER=0.5
export BOT_RS5_SELECTOR=1
export BOT_RS5_THRESHOLD=-2.39
export BOT_RS5_ACTIVATION_MANIFEST="$DIR/config/rs5_activation_manifest.json"
export BOT_MIN_TARGET_PCT=2.0
export BOT_ENTRY_START=07:05
export BOT_ENTRY_DEADLINE=07:07
export BOT_EXIT_TIME=18:35
export BOT_ENTRY_EXECUTION_POLICY=delayed_0705_depth50_fok
export BOT_ENTRY_BOOK_DEPTH=50
export BOT_ENTRY_MAX_QUOTE_AGE_MS=3000
export BOT_ENTRY_MAX_IMPACT_BPS=10.0
export BOT_ENTRY_ACTIVATION_MANIFEST="$DIR/config/entry_0705_activation_manifest.json"
export BOT_TARGET_DISTANCE_MULTIPLIER=1.25
export BOT_TARGET_ACTIVATION_MANIFEST="$DIR/config/profit_target_activation_manifest.json"
export BOT_OPENING_SCANNER=1
export BOT_FORWARD_SHADOW=1
export BOT_FORWARD_SHADOW_DIR="$DIR/runtime/forward_shadow"
export BOT_FORWARD_SHADOW_MANIFEST="$DIR/config/forward_shadow_manifest.json"

exec "${PYTHON:-python3}" -m argonus.trading.trade_bot tick "$@" >> "$DIR/runtime/bot_log.txt" 2>&1
