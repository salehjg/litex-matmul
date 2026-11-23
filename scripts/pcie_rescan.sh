#!/usr/bin/env bash
# pcie_rescan.sh - make Linux see the card again after a JTAG reload (needs sudo).
# If the card still does not show up, reboot the PC (warm reboot keeps the FPGA loaded).
set -euo pipefail
source "$(dirname "$0")/env.sh"
bdf="$(pcie_bdf)"
[[ -n "$bdf" ]] && echo 1 | sudo tee "/sys/bus/pci/devices/$bdf/remove" > /dev/null
echo 1 | sudo tee /sys/bus/pci/rescan > /dev/null
sleep 1
sudo lspci -vv -d 10ee: | grep -E "^[0-9a-f]|Region 0|LnkCap:|LnkSta:" || echo "Card not found on the PCIe bus."
# LiteX design: Region 0 size=128K. LnkSta shows the link we got (Gen2 = 5GT/s, width x8 or less).
