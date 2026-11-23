#!/usr/bin/env bash
# step2_after_reboot.sh - run once after rebooting with intel_iommu=on (scripts/iommu/step1_iommu_on.sh).
# Part of litex-matmul phase 1 (docs/notes1/gemm-hls-progress.md).
#   1. checks the IOMMU is really on
#   2. for this boot only: a hard/soft lockup becomes a panic, pstore keeps the log, the PC reboots itself after 10 s
#   3. re-installs the card permissions (udev rule now also enables the card when it appears)
# Run it again after every reboot in which you want to test DMA (the sysctls do not persist).
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo "$(readlink -f "$0")" "$@"

REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"

# 1. IOMMU check.
n_groups=$(find /sys/kernel/iommu_groups -mindepth 1 -maxdepth 1 2> /dev/null | wc -l || true)
if grep -q 'intel_iommu=on' /proc/cmdline && [[ $n_groups -gt 0 ]]; then
    echo "IOMMU on: $n_groups IOMMU groups."
else
    echo "WARNING: IOMMU not active (cmdline: $(cat /proc/cmdline); groups: $n_groups)."
    echo "         Run $REPO/scripts/iommu/step1_iommu_on.sh and reboot first. Do not run DMA tests like this."
    exit 1
fi

# 2. Lockup -> panic -> pstore -> automatic reboot (until the next reboot).
sysctl -w kernel.hardlockup_panic=1 kernel.softlockup_panic=1 kernel.panic=10

# 3. Card permissions for your user (setup_permissions.sh uses $SUDO_USER, set by sudo above).
"$REPO/scripts/setup_permissions.sh"

echo
echo "Ready. Tell Claude to run the DMA retest."
