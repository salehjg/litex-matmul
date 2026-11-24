#!/usr/bin/env bash
# lane_sweep.sh - int32 error count per A lane while one DDR byte lane's read delay is moved around its calibrated
# value. Same bitstream and data each time; only the IDELAY tap changes.
#   misc/int32_verify/lane_sweep.sh <build_dir> [channel] [lane] [deltas...]
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
B=$(cd "$1" && pwd); CH=${2:-0}; LANE=${3:-0}; shift 3 2>/dev/null; DELTAS=${*:-"-3 -2 -1 0 1 2 3"}
OUT=$ROOT/misc/int32_verify/logs; mkdir -p "$OUT"
cd "$ROOT"
[[ -n "${GEMM_BOARD_LOCK_HELD:-}" ]] || { exec 9>"$ROOT/build/.board.lock"; flock 9; export GEMM_BOARD_LOCK_HELD=1; }  # the card is ours
.venv/bin/python scripts/gemm_run.py "$B" --program --sizes 512x512x512 --verify full --results "$OUT/bringup.jsonl" 2>&1 | grep -o "verify.*" || true
# calibrated values of this lane, from the bring-up's ddr_init.log
read -r WBS RBS D0 OFFS < <(python3 - "$B/logs/ddr_init.log" "$CH" "$LANE" <<'PY'
import re, sys
log, ch, lane = open(sys.argv[1]).read(), sys.argv[2], sys.argv[3]
sec = log.split(f"DDR3 CH{ch}: calibration at")[-1]
m = re.search(rf"lane {lane}: write bitslip (\d+), read bitslip (\d+), delay\s+(\d+) \(window \d+/32 taps\)(?:, bit offsets \[([-\d, ]+)\])?", sec)
offs = m.group(4).replace(" ", "") if m.group(4) else "0,0,0,0,0,0,0,0"
print(m.group(1), m.group(2), m.group(3), offs)
PY
)
echo "CH$CH lane $LANE calibrated: wbitslip $WBS rbitslip $RBS delay $D0 offsets $OFFS"
R=$(python3 misc/int32_verify/runner_cmd.py "$B")
TAG=$(basename "$B" | cut -c1-24)
for d in $DELTAS; do
  .venv/bin/python misc/int32_verify/lane_delay.py "$B" "$CH" "$LANE" "$WBS" "$RBS" $((D0 + d)) "$OFFS" > /dev/null
  f="$OUT/${TAG}_ch${CH}l${LANE}_d$d.txt"
  res=$($R --n 1024 --k 1024 --m 1024 --verify full --seed 1 --dump-bad "$f" 2>&1 | grep -o 'verify [A-Z]* ([0-9]* checked, [0-9]* bad')
  echo "delta $d (tap $((D0 + d))): $res  rows by lane: $(python3 -c "
import collections
r=[int(l.split()[0]) for l in open('$f')]
print(sorted(collections.Counter(i % 16 for i in set(r)).items()))")"
done
.venv/bin/python misc/int32_verify/lane_delay.py "$B" "$CH" "$LANE" "$WBS" "$RBS" "$D0" "$OFFS"   # restore
