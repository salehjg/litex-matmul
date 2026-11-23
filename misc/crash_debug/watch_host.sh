#!/usr/bin/env bash
# watch_host.sh - log the host and the card once per interval for a while (freeze bisection), no root needed.
#   misc/crash_debug/watch_host.sh <minutes> [interval_s] [label]
# Each line: time, uptime/load, card link + enable, litepcie IRQ count, new kernel messages about PCIe/DMA/locks.
# The log (misc/crash_debug/logs/, git-ignored) is fsync'ed every line, so after a freeze it shows the last minute
# the machine was alive.
set -euo pipefail
minutes="${1:?usage: $0 <minutes> [interval_s] [label]}"
interval="${2:-60}"
label="${3:-watch}"
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
mkdir -p "$here/logs"
log="$here/logs/$(date +%Y%m%d-%H%M%S)_${label}.log"
bdf="$(lspci -D -d 10ee: | head -n1 | cut -d' ' -f1)"
dev="/sys/bus/pci/devices/$bdf"

since="$(date +%s)"
end=$(( since + minutes * 60 ))
echo "watch $label: $minutes min every $interval s, card $bdf, log $log" | tee -a "$log"
while (( $(date +%s) < end )); do
    sleep "$interval"
    irqs="$(awk '/litepcie/ {s=0; for (i=2; i<=NF; i++) if ($i ~ /^[0-9]+$/) s+=$i; print s}' /proc/interrupts)"
    link="x$(cat "$dev/current_link_width" 2> /dev/null || echo ?) en=$(cat "$dev/enable" 2> /dev/null || echo ?)"
    msgs="$(journalctl -k --no-pager -o cat --since "@$since" 2> /dev/null \
            | grep -iE "AER|DMAR|Bus Error|litepcie|rcu|lockup|hung|NMI|MCE|Machine check" | tail -3 | tr '\n' '|' || true)"
    since="$(date +%s)"
    line="$(date +%H:%M:%S) $(cut -d' ' -f1-3 /proc/loadavg) up=$(cut -d. -f1 /proc/uptime)s card=$link irqs=${irqs:-none} msgs=${msgs:-none}"
    echo "$line" >> "$log"; sync "$log"
    echo "$line"
done
echo "watch $label: done, no freeze" | tee -a "$log"
