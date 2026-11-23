#!/usr/bin/env bash
# ddr_init.sh - calibrate both DDR3 channels from the host and test them (~15 s).
# Run after every bitstream load, and *after* loading the LitePCIe driver if you use it: its probe resets the SoC
# (CSR_CTRL_RESET), which clears the calibration. With the driver loaded the CSRs go through /dev/litepcie0 (BAR0
# mmap is refused then), otherwise through BAR0.
# Options: --channel 0|1, --stride N, --bist-size BYTES (see -h). BUILD_DIR=... selects another build.
set -euo pipefail
source "$(dirname "$0")/env.sh"
BUILD="${BUILD_DIR:-$BUILD}"
if [[ -e /dev/litepcie0 ]]; then
    where=(--dev /dev/litepcie0)
else
    where=(--bar "$(pcie_bar)")
fi
python3 "$ROOT/boards/dupcb/ddr_init.py" "${where[@]}" --build-dir "$BUILD" "$@"
