#!/usr/bin/env bash
# sim.sh - simulate a LiteX SoC (VexRiscv + BIOS + DDR3 model) in Verilator.
# Use it to test firmware and your own SoC logic. The board PHYs (DDR, PCIe) are Xilinx
# primitives and cannot run in Verilator; for those, test on the card.
# Needs: verilator, json-c and libevent from your distribution (Arch: sudo pacman -S verilator json-c libevent).
set -euo pipefail
source "$(dirname "$0")/env.sh"
command -v verilator > /dev/null || { echo "verilator not found: install verilator, json-c and libevent"; exit 1; }
litex_sim --output-dir "$ROOT/build/sim" --cpu-type vexriscv \
    --with-sdram --sdram-module MT41K256M8 --sdram-data-width 64 "$@"
