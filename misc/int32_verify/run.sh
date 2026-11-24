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
if [[ "${2:-}" != "--no-program" ]]; then
  .venv/bin/python scripts/gemm_run.py "$B" --program --sizes 512x512x512 --verify full --results "$OUT/bringup.jsonl" \
    2>&1 | tee "$OUT/bringup.log" | grep -v "^\[bring-up\]" || true
fi
ID=$(python3 -c "import json;print(json.load(open('$B/config.resolved.json'))['config_hash'][:8])")
DT=$(python3 -c "import json,sys;sys.path.insert(0,'flow');from gemmflow.config import BuildCfg;print(BuildCfg.from_dict(json.load(open('$B/config.resolved.json'))['config']).numeric().spec)")
R="$B/sw/gemm_runner --map a=ch0,b=ch1,c=ch1 --transposed-a --expect-build-id $ID --dtype $DT --multiples 1024,16,512"
n=0
run() { n=$((n+1)); echo "== [$n] $*"; $R "$@" --dump-bad "$OUT/bad_$n.txt" 2>&1 | grep -v '^{' || true; }
{
  run --n 1024 --k 1024 --m 1024 --verify full                                   # full code range
  run --n 1024 --k 1024 --m 1024 --verify full --value-range -100,100            # nothing overflows
  run --n 1024 --k 1024 --m 1024 --verify full --value-range -30000,30000        # products fit, sums overflow
  run --n 1024 --k 1024 --m 1024 --verify full --value-range 0,1                 # tiny
  run --n 1024 --k 16 --m 512 --verify full                                      # shortest K, full range
  run --n 1024 --k 64 --m 512 --verify full
  run --n 1024 --k 1024 --m 512 --verify full
} 2>&1 | tee "$OUT/ranges.log"
