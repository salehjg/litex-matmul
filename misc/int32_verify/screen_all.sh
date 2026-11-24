#!/usr/bin/env bash
# screen_all.sh - screen.sh over a list of builds; one summary line per build in logs/screen_all.log.
# Waits while a campaign that predates the board lock (WAIT_LOG) is in its run stage.
#   misc/int32_verify/screen_all.sh build_dir... (env WAIT_LOG=results/<campaign>/dse.log)
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SUM=$ROOT/misc/int32_verify/logs/screen_all.log
for B in "$@"; do
  while [[ -n "${WAIT_LOG:-}" ]] && grep -q "^run " "$WAIT_LOG" 2>/dev/null && ! grep -q "^EXIT=" "$WAIT_LOG"; do sleep 30; done
  out=$("$ROOT/misc/int32_verify/screen.sh" "$B" 2>&1)
  echo "$(date +%H:%M) $(basename "$B"): $(echo "$out" | grep -o 'seed [12]: verify [A-Z]* ([0-9]* checked, [0-9]* bad' | sed 's/verify //;s/ checked//' | tr '\n' ' ') $(echo "$out" | grep -o 'bad by PE.*' | head -1)" | tee -a "$SUM"
done
