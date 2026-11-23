#!/usr/bin/env bash
# load.sh - load build/dupcb/gateware/dupcb.bit over JTAG (volatile, lost at power-off).
# If the card sits in a running PC, run scripts/pcie_rescan.sh afterwards.
set -euo pipefail
source "$(dirname "$0")/env.sh"
cd "$BUILD"   # Vivado writes its .jou/.log files here, not where you called this from.
python3 "$TARGET" --output-dir "$BUILD" --load "$@"
echo "Loaded. If the card is in this PC: scripts/pcie_rescan.sh"
