#!/usr/bin/env bash
# int32_verify - why does the int32 build (PE 32x8, 200 MHz) fail exact verification while int8/int16/fixed16.8
# and float32 at the same array pass? Loads the build and runs gemm_runner with value ranges that separate
# "products overflow int32" from "sums overflow int32" from "no overflow at all". Output in logs/ (git-ignored).
#   misc/int32_verify/run.sh [build_dir] [--no-program]
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
B=${1:-$ROOT/build/gemm/soc/i32_pe32x8_t1024x512_200mhz_tA-c0ddddde}
OUT=$ROOT/misc/int32_verify/logs; mkdir -p "$OUT"
cd "$ROOT"
exec 9>"$ROOT/build/.board.lock"; flock 9; export GEMM_BOARD_LOCK_HELD=1   # the card is ours (gemmflow.board)
if [[ "${2:-}" != "--no-program" ]]; then
  .venv/bin/python scripts/gemm_run.py "$B" --program --sizes 512x512x512 --verify full --results "$OUT/bringup.jsonl" \
    2>&1 | tee "$OUT/bringup.log" | grep -v "^\[bring-up\]" || true
fi
DT=$(python3 -c "import json,sys;sys.path.insert(0,'flow');from gemmflow.config import BuildCfg;print(BuildCfg.from_dict(json.load(open('$B/config.resolved.json'))['config']).numeric().spec)")
R=$(python3 misc/int32_verify/runner_cmd.py "$B")
TAG=$(basename "$B" | cut -c1-24)
n=0
run() { n=$((n+1)); echo "== [$n] $*"; $R "$@" --dump-bad "$OUT/${TAG}_bad_$n.txt" 2>&1 | grep -v '^{' || true; }
BITS=$(python3 -c "import sys;sys.path.insert(0,'flow');from gemmflow.numeric import parse_spec;print(parse_spec('$DT').bits)")
SMALL=$([ "$BITS" -ge 32 ] && echo 100 || echo 3)
{
  run --n 1024 --k 1024 --m 1024 --verify full                                   # full code range
  run --n 1024 --k 1024 --m 1024 --verify full --value-range -$SMALL,$SMALL      # small, mixed signs
  run --n 1024 --k 1024 --m 1024 --verify full --value-range 0,$SMALL            # small, non-negative
  run --n 1024 --k 1024 --m 1024 --verify full --value-range -$SMALL,0           # small, non-positive
  run --n 1024 --k 1024 --m 1024 --verify full --value-range 0,1                 # tiny
} 2>&1 | tee "$OUT/${TAG}_ranges.log"
