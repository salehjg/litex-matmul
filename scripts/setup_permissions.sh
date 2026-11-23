#!/usr/bin/env bash
# setup_permissions.sh - let your user drive the card without typing a sudo password. Run once:
#   sudo scripts/setup_permissions.sh              install (re-run if the card moves to another slot)
#   sudo scripts/setup_permissions.sh --uninstall  remove everything it installed
#
# What it installs (all for the user who ran sudo, $SUDO_USER):
#   /etc/udev/rules.d/60-litepcie.rules   /dev/litepcie* owned by you; each time the card appears: enable it
#                                         (the kernel allows writing sysfs "enable" only with CAP_SYS_ADMIN,
#                                         whatever the file mode) and give you its remove/resource0 files.
#                                         (udev warns that OWNER= for a non-system user is deprecated; it still
#                                         works on systemd 262)
#   /etc/tmpfiles.d/litepcie.conf         /sys/bus/pci/rescan writable by you (re-applied at boot)
#   /usr/local/sbin/litepcie-modctl       root-owned wrapper: load/unload litepcie.ko (and nothing else)
#   /etc/sudoers.d/litepcie               NOPASSWD for that wrapper, and for the exact "chmod 666" on the card's
#                                         enable/resource0 files that LiteX's CommPCIe always runs with sudo
#
# Loading a kernel module needs root, so module load/unload still goes through sudo, only without a
# password. Note: whoever may load a litepcie.ko of their choosing can run anything as root, so this
# is for a single-user workstation where that user already has sudo.
set -euo pipefail

UDEV_RULE=/etc/udev/rules.d/60-litepcie.rules
TMPFILES=/etc/tmpfiles.d/litepcie.conf
WRAPPER=/usr/local/sbin/litepcie-modctl
SUDOERS=/etc/sudoers.d/litepcie

[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo $0 $*" >&2; exit 1; }
USER_NAME="${SUDO_USER:-}"
[[ -n "$USER_NAME" && "$USER_NAME" != root ]] || { echo "Run via sudo from your own user account." >&2; exit 1; }

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$UDEV_RULE" "$TMPFILES" "$WRAPPER" "$SUDOERS"
    udevadm control --reload
    echo "Removed. Permissions already changed on /sys and /dev stay until the next reboot / card reload."
    exit 0
fi

# 1. udev: device nodes + the card's sysfs files. LitePCIe uses Xilinx vendor 10ee, device IDs 0x70xx.
cat > "$UDEV_RULE" << EOF
# Installed by litex-matmul scripts/setup_permissions.sh - LitePCIe card usable by $USER_NAME without sudo.
KERNEL=="litepcie[0-9]*", OWNER="$USER_NAME", MODE="0600"
ACTION=="add", SUBSYSTEM=="pci", ATTR{vendor}=="0x10ee", ATTR{device}=="0x70??", \\
  RUN+="/bin/sh -c 'cd /sys%p && chown $USER_NAME enable remove resource0; for i in 1 2 3 4 5 6 7 8 9 10; do echo 1 > enable 2> /dev/null && break; sleep 0.3; done'"
EOF
# The enable retry: on a rescan the "add" event comes before the kernel assigns BAR0, and enabling fails
# ("BAR 0 not claimed; can't enable device") until it is assigned, a few hundred ms later.
udevadm control --reload
udevadm trigger --action=add --subsystem-match=pci --attr-match=vendor=0x10ee
udevadm trigger --action=add --subsystem-match=litepcie 2> /dev/null || true

# 2. tmpfiles: the global PCI rescan file (sysfs permissions do not survive a reboot).
cat > "$TMPFILES" << EOF
# Installed by litex-matmul scripts/setup_permissions.sh
z /sys/bus/pci/rescan 0200 $USER_NAME root -
EOF
systemd-tmpfiles --create "$TMPFILES"

# 3. Module wrapper: accepts only a file named litepcie.ko.
cat > "$WRAPPER" << 'EOF'
#!/bin/sh
# litepcie-modctl - load/unload the LitePCIe kernel module. Installed by litex-matmul scripts/setup_permissions.sh.
#   litepcie-modctl load /path/to/litepcie.ko | unload
set -eu
loaded() { grep -q "^$1 " /proc/modules; }
case "${1:-}" in
    load)
        ko="${2:-}"
        [ "$(basename "$ko")" = litepcie.ko ] && [ -f "$ko" ] || { echo "usage: $0 load /path/to/litepcie.ko" >&2; exit 1; }
        if loaded litepcie; then rmmod litepcie; fi
        if loaded liteuart; then rmmod liteuart; fi
        insmod "$ko"
        ;;
    unload)
        if loaded litepcie; then rmmod litepcie; fi
        ;;
    *) echo "usage: $0 load /path/to/litepcie.ko | unload" >&2; exit 1 ;;
esac
EOF
chown root:root "$WRAPPER"
chmod 0755 "$WRAPPER"

# 4. sudoers: the wrapper, plus exact chmod commands for each LitePCIe card present now (no wildcards).
CHMOD="$(command -v chmod)"
tmp="$(mktemp)"
{
    echo "# Installed by litex-matmul scripts/setup_permissions.sh"
    echo "$USER_NAME ALL=(root) NOPASSWD: $WRAPPER"
    found=0
    for dev in /sys/bus/pci/devices/*; do
        [[ "$(cat "$dev/vendor")" == 0x10ee && "$(cat "$dev/device")" == 0x70* ]] || continue
        found=1
        bdf="${dev##*/}"; esc="${bdf//:/\\:}"
        for f in enable resource0; do
            echo "$USER_NAME ALL=(root) NOPASSWD: $CHMOD 666 /sys/bus/pci/devices/$esc/$f"
        done
    done
    [[ $found -eq 1 ]] || echo "# (no LitePCIe card found when this was written: re-run once it enumerates)"
} > "$tmp"
visudo -cf "$tmp" > /dev/null || { echo "Generated sudoers file is invalid, not installed:" >&2; cat "$tmp" >&2; rm -f "$tmp"; exit 1; }
install -m 0440 -o root -g root "$tmp" "$SUDOERS"
rm -f "$tmp"

echo "Installed for user $USER_NAME:"
echo "  $UDEV_RULE  $TMPFILES  $WRAPPER  $SUDOERS"
grep -q "no LitePCIe card" "$SUDOERS" && echo "WARNING: no card on the bus; re-run after scripts/pcie_rescan.sh shows it."
ls -l /sys/bus/pci/rescan
for dev in /sys/bus/pci/devices/*; do
    [[ "$(cat "$dev/vendor")" == 0x10ee ]] || continue
    ls -l "$dev/enable" "$dev/remove" "$dev/resource0"
    echo "$dev/enable = $(cat "$dev/enable") (expected 1)"
done
exit 0
