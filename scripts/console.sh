#!/usr/bin/env bash
# console.sh - BIOS console over PCIe (no kernel driver needed). Ctrl-C to quit.
set -euo pipefail
source "$(dirname "$0")/env.sh"
start_server
litex_term crossover --csr-csv "$BUILD/csr.csv"
