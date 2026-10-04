#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
exec "${PYTHON:-python3}" -m argonus.runtime.run_candidate_loop "$@"
