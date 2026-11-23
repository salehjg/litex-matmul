# misc/crash_debug: capturing host freezes

The host froze twice on 2026-11-23 while the LiteX SoC was on the card (details in
[docs/notes1/gemm-hls-progress.md](../../docs/notes1/gemm-hls-progress.md), phase 1). Neither left a trace: no panic,
nothing in the journal, and EFI pstore is disabled by default on Arch, so even a panic would not have been saved.

`arm_crash_capture.sh` (root, once per boot) turns on EFI pstore, makes every lockup/stall detector panic (including
RCU stalls, which catch a CPU stuck on a PCIe access) and enables SysRq. After a crash: `ls /var/lib/systemd/pstore/`.

```
misc/crash_debug/arm_crash_capture.sh            # re-runs itself with sudo
misc/crash_debug/arm_crash_capture.sh --status
```
If the machine freezes anyway: try Alt+SysRq+C (forces a panic that pstore saves). No reaction and no reboot after
10 s means a hard hang of the CPUs or the chipset.
