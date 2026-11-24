#!/usr/bin/env bash
# float_exact.sh - bit-exact check of a float32 build with uniform(-1, 1) data (gemm_runner --float-exact: fp32
# reference accumulated in the kernel's order), so a flipped low mantissa bit is not hidden by the tolerance.
#   misc/int32_verify/float_exact.sh <build_dir> [--no-program]
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
B=$1
OUT=$ROOT/misc/int32_verify/logs; mkdir -p "$OUT"
cd "$ROOT"
exec 9>"$ROOT/build/.board.lock"; flock 9; export GEMM_BOARD_LOCK_HELD=1   # the card is ours (gemmflow.board)
if [[ "${2:-}" != "--no-program" ]]; then
  .venv/bin/python scripts/gemm_run.py "$B" --program --sizes 512x512x512 --verify full --results "$OUT/bringup.jsonl" \
    2>&1 | grep -v "^\[bring-up\]" | tail -2
fi
R=$(python3 misc/int32_verify/runner_cmd.py "$B")
TAG=$(basename "$B" | cut -c1-40)
for s in 1 2; do
  echo "== 1024^3 full, seed $s"
  $R --n 1024 --k 1024 --m 1024 --verify full --float-exact --seed $s --dump-bad "$OUT/${TAG}_fx_1024_s$s.txt" 2>&1 | grep -o "verify.*" || true
done
for s in 1 2; do
  echo "== 4096^3 sampled, seed $s"
  $R --n 4096 --k 4096 --m 4096 --verify sampled --float-exact --seed $s --dump-bad "$OUT/${TAG}_fx_4096_s$s.txt" 2>&1 | grep -o "verify.*" || true
done
