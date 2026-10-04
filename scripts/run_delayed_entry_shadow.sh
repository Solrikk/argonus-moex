#!/usr/bin/env bash
# Standalone read-only 07:05 shadow recorder. Expected weekday schedule:
# 5 7 * * 1-5 /path/to/argonus-moex/run_delayed_entry_shadow.sh
set -euo pipefail

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
[ -f PAUSE ] && exit 0
mkdir -p "$DIR/runtime"

# This wrapper deliberately does not source the live launcher, take its mutex,
# or set any production selector/position/exit variable.
exec "${PYTHON:-python3}" -m argonus.shadow.capture_delayed_entry_0705 \
  --manifest "$DIR/config/delayed_entry_shadow_manifest.json" \
  --forward-shadow-dir "$DIR/runtime/forward_shadow" \
  --state "$DIR/runtime/bot_state.json" \
  --output-dir "$DIR/runtime/delayed_entry_shadow" \
  >> "$DIR/runtime/delayed_entry_shadow.log" 2>&1
