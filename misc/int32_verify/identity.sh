#!/usr/bin/env bash
# identity.sh - with B = I the kernel returns C = A as it read A from DDR channel 0: every corrupted A element and
# bit is visible directly. Runs a few repetitions on the same data (does the same element fail again?).
#   misc/int32_verify/identity.sh [build_dir] [--no-program]
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
B=${1:-$ROOT/build/gemm/soc/i32_pe32x8_t1024x512_200mhz_tA-c0ddddde}
OUT=$ROOT/misc/int32_verify/logs; mkdir -p "$OUT"
cd "$ROOT"
[[ -n "${GEMM_BOARD_LOCK_HELD:-}" ]] || { exec 9>"$ROOT/build/.board.lock"; flock 9; export GEMM_BOARD_LOCK_HELD=1; }  # the card is ours
if [[ "${2:-}" != "--no-program" ]]; then
  .venv/bin/python scripts/gemm_run.py "$B" --program --sizes 512x512x512 --verify full --results "$OUT/bringup.jsonl" \
    2>&1 | grep -v "^\[bring-up\]" | tail -2
fi
DT=$(python3 -c "import json,sys;sys.path.insert(0,'flow');from gemmflow.config import BuildCfg;print(BuildCfg.from_dict(json.load(open('$B/config.resolved.json'))['config']).numeric().spec)")
R=$(python3 misc/int32_verify/runner_cmd.py "$B")
TAG=$(basename "$B" | cut -c1-24)
for rep in 1 2 3; do
  echo "== identity rep $rep (seed 1)"
  $R --n 1024 --k 1024 --m 1024 --verify full --b-identity --seed 1 --dump-bad "$OUT/${TAG}_id_s1_r$rep.txt" 2>&1 | grep -o "verify.*" || true
done
echo "== identity seed 2"
$R --n 1024 --k 1024 --m 1024 --verify full --b-identity --seed 2 --dump-bad "$OUT/${TAG}_id_s2.txt" 2>&1 | grep -o "verify.*" || true
