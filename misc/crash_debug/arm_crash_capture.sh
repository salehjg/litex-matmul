#!/usr/bin/env bash
# arm_crash_capture.sh - make the next host freeze leave evidence (run once per boot, root; nothing persists).
#   misc/crash_debug/arm_crash_capture.sh            arm
#   misc/crash_debug/arm_crash_capture.sh --status   show the current settings
#
# What it does:
#   - EFI pstore on (Arch builds it disabled by default): a panic's kernel log is written to EFI variables and shows
#     up after the reboot in /var/lib/systemd/pstore/ (systemd-pstore moves it there)
#   - every detector becomes a panic, and a panic reboots after 10 s:
#       hard/soft lockup (the CPU's own watchdog), hung tasks (60 s, e.g. stuck I/O), and RCU stalls. The RCU stall
#       detector matters most here: a CPU stuck on a PCIe access can't take its own watchdog NMI, but the *other* CPUs
#       see it as an RCU stall after ~21 s
#   - backtraces of all CPUs on a lockup, oops -> panic, NMI for unknown reason / unrecovered NMI / IO-check NMI -> panic
#   - SysRq fully enabled: if the screen freezes, try Alt+SysRq+C (forces a panic, so pstore saves the log). If
#     SysRq does nothing at all (and no reboot after 10 s), the CPUs or the chipset are hard-hung (USB keyboard is on
#     the chipset, so a chipset hang also kills SysRq)
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo "$(readlink -f "$0")" "$@"

SYSCTLS=(
    kernel.sysrq=1
    kernel.panic=10
    kernel.panic_on_oops=1
    kernel.hardlockup_panic=1
    kernel.softlockup_panic=1
    kernel.hardlockup_all_cpu_backtrace=1
    kernel.softlockup_all_cpu_backtrace=1
    kernel.hung_task_panic=1
    kernel.hung_task_timeout_secs=60
    kernel.panic_on_rcu_stall=1
    kernel.max_rcu_stall_to_panic=1
    kernel.unknown_nmi_panic=1
    kernel.panic_on_unrecovered_nmi=1
    kernel.panic_on_io_nmi=1
)

status() {
    echo "efi_pstore.pstore_disable = $(cat /sys/module/efi_pstore/parameters/pstore_disable)   (N = enabled)"
    echo "pstore backend            = $(cat /sys/module/pstore/parameters/backend)"
    for kv in "${SYSCTLS[@]}"; do
        k="${kv%%=*}"; printf "%-36s = %s\n" "$k" "$(sysctl -n "$k" 2> /dev/null || echo n/a)"
    done
    echo "saved crash logs: $(ls /var/lib/systemd/pstore 2> /dev/null | wc -l) in /var/lib/systemd/pstore"
}

if [[ "${1:-}" == "--status" ]]; then status; exit 0; fi

echo N > /sys/module/efi_pstore/parameters/pstore_disable
for kv in "${SYSCTLS[@]}"; do sysctl -q -w "$kv" || echo "warning: could not set $kv"; done
status
