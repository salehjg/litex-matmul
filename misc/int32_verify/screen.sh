#!/usr/bin/env bash
# screen.sh - full exact verification of a build at 2048^3 (2 seeds, full-range data; float32: --float-exact) and a
# per-PE count of the bad rows (row i is computed by PE i mod PN).   misc/int32_verify/screen.sh <build_dir> [--no-program]
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
B=$1
OUT=$ROOT/misc/int32_verify/logs; mkdir -p "$OUT"
cd "$ROOT"
[[ -n "${GEMM_BOARD_LOCK_HELD:-}" ]] || { exec 9>"$ROOT/build/.board.lock"; flock 9; export GEMM_BOARD_LOCK_HELD=1; }  # the card is ours
if [[ "${2:-}" != "--no-program" ]]; then
  .venv/bin/python scripts/gemm_run.py "$B" --program --sizes 512x512x512 --verify full --results "$OUT/bringup.jsonl" \
    2>&1 | grep -v "^\[bring-up\]" | grep -o "verify.*" || true
fi
R=$(python3 misc/int32_verify/runner_cmd.py "$B")

PN=$(python3 -c "import json;print(json.load(open('$B/config.resolved.json'))['config']['kernel']['parallelism_n'])")
TAG=$(basename "$B" | cut -c1-40)
for s in 1 2; do
  f="$OUT/${TAG}_screen_s$s.txt"
  echo "== $TAG 2048^3 full, seed $s: $($R --n 2048 --k 2048 --m 2048 --verify full --seed $s --dump-bad "$f" 2>&1 | grep -o 'verify.*')"
  python3 -c "
import collections
rows=[tuple(map(int,l.split()[:2])) for l in open('$f')]
if rows: print('   bad by PE (i mod $PN):', sorted(collections.Counter(i % $PN for i,j in rows).items()), ' by j mod 64 (top):', collections.Counter(j % 64 for i,j in rows).most_common(4))"
done
