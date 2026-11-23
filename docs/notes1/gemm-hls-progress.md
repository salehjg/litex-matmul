# GEMM HLS accelerator: progress

Plan: [gemm-hls-plan.md](gemm-hls-plan.md). Update this file whenever a task is done, blocked or changes direction. Newest log
entries go on top. Dates are absolute (YYYY-MM-DD).

Status legend: ✅ done · 🟡 in progress · ⬜ not started · ⛔ blocked

## Phase overview

| Phase | Content | Status |
|---|---|---|
| 0 | Scaffolding: submodule, docs, `.gitignore`, README | ✅ 2026-11-23 |
| 1 | Host DMA bring-up (LitePCIe driver, `litepcie_util dma_test`) | ⛔ DMA reader stalls; **host froze twice** (no trace); bisecting the freeze |
| 2 | `GemmSoC` (no CPU) + PCIe-DMA ↔ DDR bridge + `ddr_dma_test` | ⬜ |
| 3 | Kernel port to `kernels/gemm/`, config rendering/validation, HLS step | ⬜ |
| 4 | SoC integration: `HlsKernel`, perf counters, Vivado Tcl hooks, first full build | ⬜ |
| 5 | `gemm_runner` end to end, verified results and counters | ⬜ |
| 6 | `gemm_build.py` complete (stages, caching, manifest) | ⬜ |
| 7 | `gemm_dse.py` + example campaigns | ⬜ |
| 8 | README usage + final notes | ⬜ |

## Phase checklists

### Phase 0: scaffolding ✅
- [x] `submodules/gemm_hls` added as a git submodule (staged, **not committed**), recursive init:
  - gemm_hls `7c790eb660aca2754ea8026fb2e911c47565ef8c` (2022-03-15)
  - hlslib `a67e02f263d666e1c7ba649c6755025b5fdd38bf` (v1.4.3-4)
  - powermeter `ffc935b` (not used)
- [x] `docs/notes1/gemm-hls-plan.md`
- [x] `docs/notes1/gemm-hls-progress.md` (this file)
- [x] `.gitignore` extended (results, HLS/Vivado scratch, host binaries)
- [x] README: section pointing to the plan

### Phase 1: host DMA bring-up ⛔ (DMA stalls, host locked up once)
- [x] Base SoC full build (`scripts/build.sh`, 2026-11-23, ~44 min): WNS +0.059 ns, WHS +0.012 ns, 0 failing
      endpoints; `build/dupcb/gateware/dupcb.bit`, `csr.csv`, `driver/` generated
- [x] User tools built against the generated headers (`driver/user/litepcie_util`, `litepcie_test`)
- [x] JTAG reachable without root (Vivado hw_server, cable `Xilinx/26SI291A`, `xc7k480t`, DONE=1)
- [x] `scripts/driver.sh` (build / load / unload / info / dma-test) and `scripts/setup_permissions.sh` added; setup
      installed by the user; module load, rescan and `/dev/litepcie0` all work without a password
- [x] Kernel 7.3.5 + matching headers after the Arch upgrade. Module builds with no source changes (warnings only)
- [x] Bitstream loaded over JTAG, rescan: `10ee:7028`, BAR0 128 KB, **Gen2 x4**
- [x] `driver.sh load`: probe OK, identifier `LiteX SoC on DU-PCB-001-003 2026-11-23 03:03:52`, 1 MSI vector
- [x] `driver.sh info`: identifier and scratch register (0x12345678, 0xdeadbeef) read back correctly, so **MMIO works**
- [ ] ⛔ `driver.sh dma-test` **fails**: 0 Gbps, TX_BUFFERS stuck at 129, RX 0, no data errors (nothing comes back)
- [ ] ⛔ About 3 min after the last test the **host hard-locked** (PSU power cycle needed). No panic/oops in the
      journal (it ends 03:19:39), pstore empty, lockup→panic is off on this host
- [x] **User:** `scripts/iommu/step1_iommu_on.sh`, reboot, `scripts/iommu/step2_after_reboot.sh` (2026-11-23).
      Checked: `intel_iommu=on` in `/proc/cmdline`, 26 IOMMU groups, card alone in group 24, sysctls 1/1/10
- [x] Retest DMA with the IOMMU on: **still stalls, but no DMAR fault, no AER error, host stayed up** (details below)
- [ ] Root cause of the stall (next: debug build with reader probes, see below), fix
- [ ] **User:** re-run `sudo scripts/setup_permissions.sh` once more (enable-after-rescan race fix, see below)
- [ ] `scripts/status.sh` (needs the updated udev rule)

**DMA stall: what was measured.** CSRs were read through the driver, because mmap of `resource0` returns EINVAL
while the driver owns the BAR. The helper script lived in the session scratchpad and was lost with the reboot.
To rebuild it (it becomes part of `gemmflow` later): open `/dev/litepcie0`, `ioctl(fd, 0xC00C5300, buf)` =
`_IOWR('S', 0, struct litepcie_ioctl_reg)` with `buf = struct.pack("<IIB3x", addr, value, is_write)`. `addr` is the
absolute CSR address from `build/dupcb/csr.csv` (CSR base `0xf0000000`; the driver subtracts it). Multi-word CSRs are
32-bit words, MSB first. Registers sampled: `pcie_dma0_{reader,writer}_{enable,table_level,table_loop_status}`,
`pcie_dma0_loopback_enable`, `pcie_dma0_buffering_{reader,writer}_fifo_{control,status}`,
`pcie_msi_{enable,vector}`, `pcie_endpoint_phy_*` (link status, MRRS, MPS, bus master, MSI enable).
Interrupt count: `grep litepcie /proc/interrupts` (it went up by exactly 1 per test run).
- The tables are loaded as expected: reader/writer `table_level` = 256, `enable` = 3 (enable + loop mode), loopback on,
  `msi_enable` = 3.
- **The reader stops at descriptor 7, the writer at 8, in every run, within the first ~50 ms.** That is ~56 KB of
  8 KB buffers, before the first IRQ point (every 32 buffers). The 129 TX buffers are just the software queuing half
  the ring + 1, then waiting.
- Both buffering FIFOs (reader and writer side) are **empty** while stuck, so nothing is backed up in the loopback or
  the writer. The reader is waiting for read completions from the host that never arrive (or are not retired).
- Host side: no AER errors (endpoint and root port 80:1c.4 counters all 0), no DMAR/IOMMU faults logged.
- PCIe core status: link up, Gen2 x4, L0, bus master on, MSI on, MRRS 512 B, MPS 256 B.
- LitePCIe 2026.08 reorders completions with one FIFO per tag (8 tags, `max_pending_requests=8`), each sized for
  `max_request_size = 512` B (`litepcie/tlp/common.py`), the same as the negotiated MRRS. So it is not a plain
  buffer-size mismatch.

**Hypotheses (not yet tested):**
1. **No IOMMU protection: a stray DMA write corrupted host memory.** DMA remapping is *off* on this host: no
   `/sys/kernel/iommu_groups`, no `/sys/class/iommu`; only interrupt remapping is enabled (the "Default domain type:
   Translated" boot line is only the policy). So the card writes to physical addresses and nothing stops a wrong one.
   That fits a lockup minutes later with nothing logged. The writer reaching descriptor 8 while the reader is at 7 is
   suspicious in a loopback. **Turning the IOMMU on makes a stray access a logged DMAR fault (with the address)
   instead of memory corruption.** That is the first step.
2. **Completion retirement / reorder deadlock in LitePCIe** with how this root complex (Arrow Lake PCH root port,
   bus 0x80) splits or orders read completions. It explains the stall; whether it alone can hang the host is open.
3. Driver teardown on 7.3 after a stuck DMA (less likely; nothing logged).

**Decision (2026-11-23): turn the IOMMU on, then retest DMA**, before anything else on the board. The user chose
this over moving to phase 3 first.

**Next attempt, step by step.**

*User (needs root, once).* The same steps are in two scripts in `scripts/iommu/`:
`scripts/iommu/step1_iommu_on.sh` (adds `intel_iommu=on`, backs up the old command line to
`/etc/kernel/cmdline.bak-before-iommu`, runs `mkinitcpio -P`; `--revert` removes it again), then reboot, then
`scripts/iommu/step2_after_reboot.sh` (checks the IOMMU is active and stops if not, sets the sysctls, re-runs
`scripts/setup_permissions.sh`; run it again after each reboot before DMA tests). Both re-run themselves with sudo.
```
# 1. IOMMU on. The kernel command line is baked into the UKI from /etc/kernel/cmdline (GRUB 2.14 + systemd-stub;
#    /etc/default/grub is not used for it). Before: "root=PARTUUID=83a1ea82-... rw rootfstype=ext4"
sudo sed -i 's/$/ intel_iommu=on/' /etc/kernel/cmdline
sudo mkinitcpio -P            # rebuilds both /boot/EFI/Linux/arch-linux.efi and arch-linux-fallback.efi
reboot
# 2. After the reboot, for this session only (lost at the next reboot): a lockup becomes a panic, pstore keeps the
#    log, the machine reboots by itself after 10 s
sudo sysctl kernel.hardlockup_panic=1 kernel.softlockup_panic=1 kernel.panic=10
# 3. Install the updated udev rule (it writes enable=1 when the card appears; needed by scripts/status.sh)
sudo scripts/setup_permissions.sh
```
*Revert the IOMMU change* if it causes trouble: remove ` intel_iommu=on` from `/etc/kernel/cmdline`,
`sudo mkinitcpio -P`, reboot. Both UKIs carry the same command line, so if the machine won't boot at all, fix it from
a live USB.

*Claude (no root needed):*
1. Check the IOMMU is active: `ls /sys/kernel/iommu_groups | wc -l` > 0, and the card has an `iommu_group` link.
   Check the sysctls stuck: `cat /proc/sys/kernel/{hardlockup_panic,softlockup_panic,panic}` = 1 1 10.
2. `scripts/load.sh` + `scripts/pcie_rescan.sh`. The power cycle put the card back on its **flash image**
   (BAR0 2 MB), so expect BAR0 128 KB and Gen2 x4 afterwards. Check `enable` = 1.
3. `scripts/driver.sh load`, then `scripts/driver.sh info` (identifier must be `... 2026-11-23 03:03:52`).
4. **One** short test: `scripts/driver.sh dma-test -w 32 -t 2`. No parallel CSR polling.
5. `scripts/driver.sh unload` right after.
6. `journalctl -k -b | grep -iE "DMAR|iommu|AER|litepcie"`, plus the interrupt count. If the machine crashed:
   `/var/lib/systemd/pstore/` after it comes back.

*Reading the outcome:*
- **DMA works with the IOMMU on** → the earlier failure involved untranslated addresses (for example a 32-bit DMA
  mask or address problem on this platform). Measure GB/s, run a longer test, then decide between translated and
  `iommu=pt` for performance (openTPU's note).
- **DMAR faults logged** → the FPGA accessed addresses it shouldn't. The fault lines give the address and read/write
  direction: compare them with the driver's DMA buffer addresses to see whether the reader's or the writer's
  addresses are wrong.
- **Same stall, no faults** → the addresses are fine; the read completions get lost or are not retired in the FPGA
  (hypothesis 2). Next: look at completion handling in `litepcie/tlp/controller.py` (per-tag reorder FIFOs,
  `tag_retire`), possibly add LiteScope on the completion path, or test with `max_pending_requests` lowered and
  a 256 B max request size.
- **Host crashes again** → read pstore. Stop board DMA tests until the cause is understood.

**Retest with the IOMMU on (2026-11-23 03:45).** `scripts/load.sh`, `scripts/pcie_rescan.sh` (Gen2 x4, BAR0
128 KB), `driver.sh load`, `info` OK, then one `dma-test -w 32 -t 2` and `unload`:
- Result: `0.00 Gbps, TX_BUFFERS 135, RX_BUFFERS 8, ERRORS 0`, one interrupt. **No DMAR fault, no AER error, no
  crash** (the machine stayed up afterwards).
- So the card made **no stray memory access** in this run, and the first 8 buffers (64 KB) went host → FPGA →
  loopback → host **with correct data**. Then the reader stops, at the same point as before.
- The JTAG reload itself logs a root-port completion timeout + receiver errors when the link drops. Expected, harmless.

**Where the reader is stuck (from the earlier register dump + LitePCIe 2026.08 source, `litepcie/frontend/dma.py`):**
- `reader_enable` read `0x3`. Bit 1 is written by hardware with `fsm.ongoing("IDLE")`, so the reader FSM is
  **idle**. It is not waiting for a free tag or for missing completions (that would be `MEM-RD-REQ`). It has stopped
  *issuing* read requests.
- The descriptor table always presents a descriptor in loop mode (level 256). So the only thing keeping it in IDLE
  is the gate `pending_words < data_fifo_depth - max_words_per_request` (1024 − 32 beats).
- `pending_words` += `request_words` (= ceil(len/16) = 32 per 512 B request) when a request is issued, and −= 1 per
  beat leaving the reader's data FIFO. With both FIFOs empty and nothing in flight it should be 0, so **the counter
  has drifted**. Two ways that can happen:
  - requests retired **without their data**: error completions (UR/CA) are zero-length but still retire the
    request (`request_done = last & (end | err)`), leaving 32 words in the counter each; about 31 of them close the
    gate. Or completion beats get dropped before the data FIFO;
  - **more beats out than queued**: the 11-bit counter wraps around and the gate closes for good.
- Not the IOMMU (no faults) and not buffer sizing (per-tag reorder FIFOs sized for 512 B = negotiated MRRS).
  The host crash before the IOMMU was on is still unexplained. With the IOMMU on, a stray write would now be blocked
  and logged.

**Next debug step (proposal).** A debug SoC that subclasses `BaseSoC` (no change to `target.py`) and exposes the
reader and controller internals as CSRs: issued requests, completion TLPs/beats in, completions with `err`,
data FIFO level/beats out, `request_metadata` level, splitter valid, and the TLP controller's tag/request queue
levels. Then one DMA test shows which counter disagrees. Cheap extra experiment without a rebuild: set the card's
max read request size to 256 or 128 B (`setpci`, root) and see whether the stall point moves with the number of
requests, completions or bytes.

**udev race found during the retest.** After a rescan, `enable` stayed 0: udev logged `ATTR{enable}="1": Failed to
write "1" ... Invalid argument`, and the kernel logged `BAR 0 ... not claimed; can't enable device` before `BAR 0
... assigned`. On a rescan the "add" event comes *before* BAR0 is assigned (at boot the BARs are already assigned,
so it worked then). Fix in `scripts/setup_permissions.sh`: the rule's `RUN` now retries `echo 1 > enable` for up
to 3 s instead of `ATTR{enable}="1"` (rule checked with `udevadm verify`). **Needs one more
`sudo scripts/setup_permissions.sh`.** It only matters for the tools that map BAR0 without the driver
(`litex_server`, `status.sh`, `ddr_init.py`); the driver enables the card itself. udev also warns that `OWNER=` for
a non-system user is deprecated (still works on systemd 262).

**Host freeze #2 (2026-11-23 ~03:59) and what it rules out.**
- Timeline: IOMMU retest at 03:45 (driver unloaded 03:45:32), then commits, the probe SoC fast check, and the probe
  SoC Vivado build started ~03:56. The kernel logged **nothing** after the unload. The machine stopped at ~03:59:08,
  during synthesis: `dupcb_synth.dcp` was created (0 bytes), and the build log's tail is zero-filled (page cache
  lost). Even NVMe writes stopped, and the NVMe hangs off the CPU, not the chipset, so the **whole machine** froze, not just the PCH.
- The lockup→panic sysctls were set but nothing happened (no automatic reboot after 10 s). **Correction:** EFI
  pstore is disabled by default on Arch (`CONFIG_EFI_VARS_PSTORE_DEFAULT_DISABLE=y`), so a panic would not have been
  saved anyway. `misc/crash_debug/arm_crash_capture.sh` now turns it on at runtime and adds the RCU-stall panic.
  That is the detector that catches a CPU stuck on a PCIe access, because the stuck CPU can't take its own watchdog
  NMI but the other CPUs notice it.
- **Pattern:** both freezes came ~10–15 min after our bitstream was loaded (03:10→03:19, 03:44→03:59). Freeze #1
  happened with the driver loaded, right after DMA tests, with nothing heavy running. Freeze #2 happened 13 min after
  a DMA test, with the driver unloaded and Vivado running.
- **History:** no unexplained freezes before the card was installed. The 2026-11-11 unclean boot predates the card (then
  `82:00.0` was the Realtek NIC). On 2026-11-20 the LiteX design ran here (MMIO only, no driver/DMA) in a 12 h
  boot that shut down cleanly.
- The card is slot-powered only (no aux connector). Topology: card on Z890 PCH root port `80:1c.4`; NVMe on the CPU
  (`00:01.0`); USB/keyboard, NIC, SATA on the PCH.
- **Heat/supply ruled out** (`misc/fpga_health/`, XADC over JTAG): flash image 47 °C; our base SoC **39 °C, flat for
  9 min** after a JTAG load + rescan with no driver; VCCINT 0.957 V, VCCAUX 1.80 V, VCCBRAM 0.957 V, all steady.
  No freeze in those 9 min.

**Bisection of the freeze (in progress).** Each step runs with crash capture armed:
- [ ] **A: our design loaded, no driver, heavy load.** The probe SoC Vivado build (~45 min) runs from 11:47 with the
      base SoC on the card since 11:34. Survives → the driver/DMA path is implicated. Freezes → it is the design or
      link itself.
- [ ] **B: driver loaded, MMIO only** (`driver.sh load`, `info`), then wait 20 min.
- [ ] **C: one DMA test, driver unloaded, wait 20 min** (repeats freeze #2's conditions).
- [ ] Then the probe SoC: same DMA test with counters, to explain the stall.

### Phase 2: DMA ↔ DDR ⬜
- [ ] `hw/gemm/gemm_soc.py` without CPU; `csr.csv` DDR names unchanged versus the base SoC
- [ ] `hw/gemm/ddr_dma.py` + Migen sim test
- [ ] `sw/gemm_host/ddr_dma_test`: CH0 and CH1 write/read-back clean, GB/s recorded
- [ ] `ddr_init.py` passes without a CPU

### Phase 3: kernel port + HLS step ⬜
- [ ] Sources copied to `kernels/gemm/`, `ORIGIN.md` written
- [ ] Interface pragmas changed (offset=direct, ap_none, ap_ctrl_hs, 32-bit addresses, m_axi knobs)
- [ ] `gemmflow/config.py`: dataclasses, validation, `Config.h` rendering, hashes
- [ ] g++ simulation test passes (including non-tile-multiple sizes)
- [ ] `tcl/hls/run_hls.tcl` + `HlsStep` + `HlsReport`; csynth + IP export of `default.json`
- [ ] hlslib compatibility with Vitis HLS 2026.1 checked (fallback used? yes/no)

### Phase 4: SoC integration ⬜
- [ ] `hw/gemm/hls_kernel.py` (Instance from `csynth.xml`)
- [ ] `hw/gemm/axi_perf.py` + Migen sim test
- [ ] `tcl/vivado/{project_settings,import_hls_ip,floorplan,reports_json,program}.tcl`
- [ ] `--no-compile-gateware` check passes
- [ ] Full build of `default.json`: timing met, `reports/vivado.json` written

### Phase 5: end to end ⬜
- [ ] `gemm_runner`: build-ID check, DMA in/out, start/poll, counters, JSON output
- [ ] Correct results for several sizes, both memory maps
- [ ] Counter bytes match analytic traffic

### Phase 6: build driver ⬜
- [ ] `scripts/gemm_build.py`: stages, `--force`, `--dry-run`
- [ ] Stamps: a rerun with no change skips everything; a Vivado-only change reuses the HLS IP
- [ ] `manifest.json`, `reports/summary.json`

### Phase 7: DSE ⬜
- [ ] `scripts/gemm_dse.py`, `ResultStore` (CSV + JSONL, schema_version)
- [ ] Campaigns: PE sweep, clock sweep, Vivado preset sweep, size sweep
- [ ] `--resume` after an interruption

### Phase 8: docs ⬜
- [ ] README usage section
- [ ] Final numbers and lessons below

## Measurements

| Date | Build / config | What | Result |
|---|---|---|---|
| 2026-11-23 | base SoC (`build/dupcb`) | Vivado timing | WNS +0.059 ns, WHS +0.012 ns (met) |
| 2026-11-23 | (design loaded before) | PCIe link before reload | Gen2 x1 (max x8), BAR0 2 MB: not this design |
| 2026-11-23 | base SoC (`build/dupcb`) | PCIe link after JTAG load + rescan | Gen2 x4, BAR0 128 KB |
| 2026-11-23 | base SoC | MMIO (identifier, scratch) | OK |
| 2026-11-23 | base SoC | DMA loopback (`dma_test -w 32`) | **0 Gbps**, reader stuck at descriptor 7, writer at 8 |
| 2026-11-23 | base SoC, IOMMU on | DMA loopback (`dma_test -w 32 -t 2`) | **0 Gbps**, TX 135 / RX 8 buffers, 0 errors, no DMAR/AER, no crash |

## Open issues / decisions pending

- `README.md` and `.gitignore` are in `FILES` of `setup.litex.du-pcb.py`. Phase 0 changed both but did **not** run
  `--pack`, so `setup.litex.du-pcb.py --force` would revert them. Decide whether to pack them, or keep the GEMM flow
  out of the setup bundle (the plan's default for all new GEMM files).
- `scripts/env.sh` and `scripts/pcie_rescan.sh` (also packed files) were changed in phase 1 for the password-less
  flow. The same `--pack` decision applies; the new `scripts/driver.sh` and `scripts/setup_permissions.sh` would have
  to be added to `FILES` if the bundle should carry them.
- `/usr/local/sbin/litepcie-modctl` and the sudoers lines name the slot `0000:82:00.0`. Moving the card means
  re-running `sudo scripts/setup_permissions.sh`.
- Risks are listed in plan §11.

## Log

### 2026-11-23 (phase 1, freeze #2, bisection)
- Host froze again (~03:59), this time with no DMA running (Vivado synthesis, driver unloaded for 13 min). No trace.
- Committed the work so far (user allowed commits in this repo): baseline LiteX setup (original files from the setup
  archive), plan + submodule, phase 1 scripts, misc/ debug cases.
- Found EFI pstore disabled on Arch (so no panic could ever have been saved); added `misc/crash_debug/`.
- Ruled out heat and supply problems (XADC over JTAG, `misc/fpga_health/`). Started the bisection (step A).
- LitePCIe 2026.08 history: the reader / TLP controller / depacketizer were reworked in Aug–Sep 2026 (`2ca518e`
  tag recycling after ordered retirement, `ea77120`/`04a3262` reader completion handling, `5b0133a`/`aa88789`
  completion buffer accounting, `f4dbb63` depacketizer tails, `75500a0` descriptor retirement on completion). This is a
  candidate regression for the *stall*; 2026.04 predates all of it.

### 2026-11-23 (phase 1, IOMMU retest)
- IOMMU enabled by the user (`scripts/iommu/`, moved there from the home directory at the user's request; paths in
  the scripts are now relative to the repo). Retest: same stall, no faults, host fine, first 64 KB correct.
- Narrowed the stall to the DMA reader's pending-words gate (reader idle, table full, FIFOs empty). Proposed a debug
  build with probe CSRs.
- Found and fixed the udev enable race after a rescan (needs `sudo scripts/setup_permissions.sh` again).

### 2026-11-23 (phase 1, DMA failure + host lockup)
- After the upgrade: module built and loaded with no password, `/dev/litepcie0` owned by the user (udev rule works).
  MMIO OK. DMA loopback stalls (details in the phase 1 section).
- Host hard-locked a few minutes after the last DMA test; user power-cycled. The journal has no trace. After the power
  cycle the card runs its **flash image** again (BAR0 2 MB), so `scripts/load.sh` + rescan are needed before any test.
- Stopped hardware work to agree on a safe debug plan.
- Found that DMA remapping is off on this host (no IOMMU groups; only interrupt remapping). So the card writes to
  physical memory unprotected, and a stray write is the leading explanation for the delayed hard lock. The earlier
  "IOMMU translated" reading was wrong; corrected in the hypotheses.
- Decision with the user: enable the IOMMU (`intel_iommu=on`), set lockup→panic sysctls for the session, then do one
  short DMA test. Step-by-step procedure and how to read each outcome are in the phase 1 section.
- The CSR helper in the scratchpad was lost with the reboot (tmpfs); how to rebuild it is documented above.

### 2026-11-23 (phase 1, paused)
- User installed linux-headers and ran the setup script. Permissions verified. Bitstream loaded, rescan without a
  password, link Gen2 x4, BAR0 128 KB.
- Headers are 7.3.5 but the kernel is 7.3.4 (the headers package was newer than the installed kernel). The module
  compiles cleanly against 7.3.5, so no driver patch is needed; loading it waits for the kernel upgrade.
- `scripts/status.sh` failed: writing the card's sysfs `enable` returns EPERM even with mode 666. The kernel requires
  CAP_SYS_ADMIN for that file (unlike `remove`/`rescan`). Fix: the udev rule sets `ATTR{enable}="1"` when the card
  appears, and `sysfs_write1` in `env.sh` falls back to `sudo tee` when the kernel refuses a direct write. **The setup
  script must be re-run once** to install the new rule.
- Paused here: the user is upgrading Arch.

### 2026-11-23 (phase 1)
- Built the base SoC with driver; timing met. Built the LitePCIe user tools. Confirmed JTAG works without root.
- Kernel headers are missing and `sudo` needs a password, so the module build and load are blocked. Added
  `scripts/setup_permissions.sh` (one-time root setup) and `scripts/driver.sh`; made `env.sh` and `pcie_rescan.sh`
  password-less once the setup is installed. `pipefail` + `lsmod | grep -q` is unreliable (SIGPIPE), so the scripts
  read `/proc/modules` instead.

### 2026-11-23
- Explored the repo, the board docs and upstream gemm_hls. Decisions D1–D7 written down in the plan (Vivado through
  LiteX script plus our Tcl hooks; no CPU; C++ runner plus Python; CSR control; IP-catalog import; dynamic sizes;
  `params.tcl` rule).
- Added the `submodules/gemm_hls` submodule (staged only, nothing committed). Wrote the plan and progress docs.
  Extended `.gitignore`. Added a README pointer.
