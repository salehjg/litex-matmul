#!/usr/bin/env bash
# pcie_rescan.sh - make Linux see the card again after a JTAG reload.
# No sudo needed after scripts/setup_permissions.sh (otherwise it asks for the password).
# If the card still does not show up, reboot the PC (warm reboot keeps the FPGA loaded).
set -euo pipefail
source "$(dirname "$0")/env.sh"
bdf="$(pcie_bdf)"
if [[ -n "$bdf" ]]; then
    grep -q '^litepcie ' /proc/modules && "$ROOT/scripts/driver.sh" unload   # The driver must let go first.
    sysfs_write1 "/sys/bus/pci/devices/$bdf/remove"
fi
sysfs_write1 /sys/bus/pci/rescan
sleep 1
bdf="$(pcie_bdf)"
[[ -n "$bdf" ]] || { echo "Card not found on the PCIe bus."; exit 1; }
dev="/sys/bus/pci/devices/$bdf"
lspci -s "$bdf"
echo "BAR0 size: $(( $(stat -c %s "$dev/resource0") / 1024 )) KB (LiteX design: 128 KB)"
echo "Link: $(cat "$dev/current_link_speed") x$(cat "$dev/current_link_width")" \
     "(card max: $(cat "$dev/max_link_speed") x$(cat "$dev/max_link_width"))"
