#!/usr/bin/env bash
# tune_and_screen.sh - program a build, screen it, tune the DDR read taps under load (load_tune.py), screen again.
#   misc/int32_verify/tune_and_screen.sh <build_dir> [load_tune options]
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
B=$(cd "$1" && pwd); shift
cd "$ROOT"
[[ -n "${GEMM_BOARD_LOCK_HELD:-}" ]] || { exec 9>"$ROOT/build/.board.lock"; flock 9; export GEMM_BOARD_LOCK_HELD=1; }
echo "== before tuning"; misc/int32_verify/screen.sh "$B" 2>&1 | grep -E "^==|bad by"
echo "== tuning"; .venv/bin/python misc/int32_verify/load_tune.py "$B" "$@" 2>&1 | grep -E "load tune"
echo "== after tuning"; misc/int32_verify/screen.sh "$B" --no-program 2>&1 | grep -E "^==|bad by"
