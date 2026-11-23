#!/usr/bin/env bash
# set_mrrs.sh - set the card's PCIe Max Read Request Size (debug experiment, needs root).
# The value lasts until the next rescan / reboot (Linux sets it again at enumeration; the litepcie driver does not
# touch it). The FPGA side follows it live (LitePCIe reads cfg_dcommand, caps it at 512).
#   sudo misc/dma_debug/set_mrrs.sh 128|256|512 [BDF]      (default BDF: first 10ee:* device)
#   sudo misc/dma_debug/set_mrrs.sh show [BDF]
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo "$(readlink -f "$0")" "$@"

size="${1:?usage: $0 128|256|512|show [BDF]}"
bdf="${2:-$(lspci -D -d 10ee: | head -n1 | cut -d' ' -f1)}"
[[ -n "$bdf" ]] || { echo "No 10ee: device on the bus." >&2; exit 1; }

show() {  # Device Control (PCIe capability + 8): MRRS bits 14:12, MPS bits 7:5, size = 128 << code.
    local v; v=$((16#$(setpci -s "$bdf" CAP_EXP+8.w)))
    echo "$bdf DevCtl=0x$(printf %04x $v): MRRS $((128 << ((v >> 12) & 7))) B, MPS $((128 << ((v >> 5) & 7))) B"
}

case "$size" in
    show) show; exit 0 ;;
    128) code=0 ;; 256) code=1 ;; 512) code=2 ;;
    *) echo "size must be 128, 256 or 512 (the FPGA caps at 512)" >&2; exit 1 ;;
esac
echo -n "before: "; show
setpci -s "$bdf" "CAP_EXP+8.w=$(printf %04x $((code << 12))):7000"
echo -n "after:  "; show
