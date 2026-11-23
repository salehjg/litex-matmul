#!/usr/bin/env bash
# step1_iommu_on.sh - turn the Intel IOMMU (DMA remapping) on, so a stray FPGA DMA access becomes a logged
# DMAR fault instead of host memory corruption. Part of litex-matmul phase 1 (docs/notes1/gemm-hls-progress.md).
#   scripts/iommu/step1_iommu_on.sh            add intel_iommu=on, rebuild the UKIs, then reboot yourself
#   scripts/iommu/step1_iommu_on.sh --revert   take it out again, rebuild the UKIs, then reboot yourself
# The kernel command line is baked into the UKIs (/boot/EFI/Linux/arch-linux*.efi) from /etc/kernel/cmdline.
# After the reboot run scripts/iommu/step2_after_reboot.sh.
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo "$(readlink -f "$0")" "$@"

CMDLINE=/etc/kernel/cmdline
BACKUP=/etc/kernel/cmdline.bak-before-iommu

echo "Before: $(cat "$CMDLINE")"
if [[ "${1:-}" == "--revert" ]]; then
    sed -i 's/ intel_iommu=on//g' "$CMDLINE"
elif grep -q 'intel_iommu=on' "$CMDLINE"; then
    echo "intel_iommu=on is already there."
else
    [[ -e "$BACKUP" ]] || cp "$CMDLINE" "$BACKUP"
    sed -i '1s/$/ intel_iommu=on/' "$CMDLINE"
fi
echo "After:  $(cat "$CMDLINE")"

mkinitcpio -P

echo
echo "Done. Reboot now (sudo reboot), then run: $(dirname "$(readlink -f "$0")")/step2_after_reboot.sh"
echo "If the machine misbehaves: $(readlink -f "$0") --revert, then reboot (backup: $BACKUP)."
