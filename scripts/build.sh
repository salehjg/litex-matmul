#!/usr/bin/env bash
# build.sh - generate the SoC, BIOS, kernel driver and bitstream into build/dupcb/.
#   scripts/build.sh                         full build (Vivado, ~30-60 min)
#   scripts/build.sh --no-compile-gateware   fast check: Verilog + XDC + BIOS only
set -euo pipefail
source "$(dirname "$0")/env.sh"
python3 "$TARGET" --output-dir "$BUILD" --build --driver "$@"
