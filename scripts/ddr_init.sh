#!/usr/bin/env bash
# ddr_init.sh - calibrate both DDR3 channels from the host and test them (~1-2 min).
# Run after every bitstream load. Options: --channel 0|1, --stride N, --bist-size BYTES (see -h).
set -euo pipefail
source "$(dirname "$0")/env.sh"
bar="$(pcie_bar)"
python3 "$ROOT/boards/dupcb/ddr_init.py" --bar "$bar" --build-dir "$BUILD" "$@"
