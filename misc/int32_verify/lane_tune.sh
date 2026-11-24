#!/usr/bin/env bash
# lane_tune.sh - with the current bring-up kept (no reprogramming), set some CH lanes to given taps and sweep one more
# lane, counting int32 errors under GEMM load. Lane settings come from the build's last ddr_init.log.
#   misc/int32_verify/lane_tune.sh <build_dir> <channel> "<lane>=<tap> ..." <sweep_lane> <taps...>
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
B=$(cd "$1" && pwd); CH=$2; FIXED=$3; SL=$4; shift 4; TAPS="$*"
OUT=$ROOT/misc/int32_verify/logs; cd "$ROOT"
[[ -n "${GEMM_BOARD_LOCK_HELD:-}" ]] || { exec 9>"$ROOT/build/.board.lock"; flock 9; export GEMM_BOARD_LOCK_HELD=1; }  # the card is ours
cal() { python3 - "$B/logs/ddr_init.log" "$CH" "$1" <<'PY'
import re, sys
sec = open(sys.argv[1]).read().split(f"DDR3 CH{sys.argv[2]}: calibration at")[-1]
m = re.search(rf"lane {sys.argv[3]}: write bitslip (\d+), read bitslip (\d+), delay\s+(\d+) \(window \d+/32 taps\)(?:, bit offsets \[([-\d, ]+)\])?", sec)
print(m.group(1), m.group(2), m.group(3), m.group(4).replace(" ", "") if m.group(4) else "0,0,0,0,0,0,0,0")
PY
}
setl() { read -r w r d o < <(cal "$1"); .venv/bin/python misc/int32_verify/lane_delay.py "$B" "$CH" "$1" "$w" "$r" "$2" "$o" >/dev/null; }
for f in $FIXED; do setl "${f%=*}" "${f#*=}"; done
R=$(python3 misc/int32_verify/runner_cmd.py "$B")
for t in $TAPS; do
  setl "$SL" "$t"
  echo "CH$CH ${FIXED} lane $SL=$t: $($R --n 1024 --k 1024 --m 1024 --verify full --seed 1 2>&1 | grep -o 'verify [A-Z]* ([0-9]* checked, [0-9]* bad')"
done
