#!/usr/bin/env bash
# status.sh - quick health check over PCIe: SoC ident, measured clocks (Hz), LED register.
set -euo pipefail
source "$(dirname "$0")/env.sh"
start_server
cd "$BUILD"
litex_cli --ident
litex_cli --regs --filter freq    # Expect ~50000000 (clk50) and ~200000000 (accel, from clk200 #1).
litex_cli --regs --filter leds
