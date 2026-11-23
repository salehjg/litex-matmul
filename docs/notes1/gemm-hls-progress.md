# GEMM HLS accelerator: progress

Plan: [gemm-hls-plan.md](gemm-hls-plan.md). Update this file whenever a task is done, blocked or changes direction. Newest log
entries go on top. Dates are absolute (YYYY-MM-DD).

Status legend: ✅ done · 🟡 in progress · ⬜ not started · ⛔ blocked

## Phase overview

| Phase | Content | Status |
|---|---|---|
| 0 | Scaffolding: submodule, docs, `.gitignore`, README | ✅ 2026-11-23 |
| 1 | Host DMA bring-up (LitePCIe driver, `litepcie_util dma_test`) | ✅ DMA works (13.43 Gb/s, 0 errors) after the S7 PHY RX byte-enable fix · ⚠️ host freeze cause still open (capture armed) |
| 2 | `GemmSoC` (no CPU) + PCIe-DMA ↔ DDR bridge + `ddr_dma_test` | 🟡 started 2026-11-23 13:25 |
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

### Phase 1: host DMA bring-up ✅ DMA works · ⚠️ freeze cause open
- [x] Base SoC full build (`scripts/build.sh`, 2026-11-23, **~6 min**: synth 1:31, place 0:47, route 0:47, bitstream 0:43): WNS +0.059 ns, WHS +0.012 ns, 0 failing
      endpoints; `build/dupcb/gateware/dupcb.bit`, `csr.csv`, `driver/` generated
- [x] User tools built against the generated headers (`driver/user/litepcie_util`, `litepcie_test`)
- [x] JTAG reachable without root (Vivado hw_server, cable `Xilinx/26SI291A`, `xc7k480t`, DONE=1)
- [x] `scripts/driver.sh` (build / load / unload / info / dma-test) and `scripts/setup_permissions.sh` added; setup
      installed by the user; module load, rescan and `/dev/litepcie0` all work without a password
- [x] Kernel 7.3.5 + matching headers after the Arch upgrade. Module builds with no source changes (warnings only)
- [x] Bitstream loaded over JTAG, rescan: `10ee:7028`, BAR0 128 KB, **Gen2 x4**
- [x] `driver.sh load`: probe OK, identifier `LiteX SoC on DU-PCB-001-003 2026-11-23 03:03:52`, 1 MSI vector
- [x] `driver.sh info`: identifier and scratch register (0x12345678, 0xdeadbeef) read back correctly, so **MMIO works**
- [x] `driver.sh dma-test`: failed at first (0 Gbps, reader stall); **works since the RX byte-enable fix**: 13.43 Gb/s
      loopback, 0 errors over 10 s (2026-11-23 13:02)
- [ ] ⛔ About 3 min after the last test the **host hard-locked** (PSU power cycle needed). No panic/oops in the
      journal (it ends 03:19:39), pstore empty, lockup→panic is off on this host
- [x] **User:** `scripts/iommu/step1_iommu_on.sh`, reboot, `scripts/iommu/step2_after_reboot.sh` (2026-11-23).
      Checked: `intel_iommu=on` in `/proc/cmdline`, 26 IOMMU groups, card alone in group 24, sysctls 1/1/10
- [x] Retest DMA with the IOMMU on: **still stalls, but no DMAR fault, no AER error, host stayed up** (details below)
- [x] Probe SoC (`misc/dma_debug/`) built and loaded (2026-11-23 11:51), its driver built (not loaded)
- [x] **Root cause of the stall found** with the probe counters (step C below): the LitePCIe 2026.08 DMA reader's
      `pending_words` underflows because each 256 B completion arrives as 17 beats, not 16
- [x] Fix the stall: `S7PCIEPHYRXBE` in `target.py`; re-tested on the probe SoC and the base SoC: 13.43 Gb/s, 0 errors
- [x] **User:** re-ran `sudo scripts/setup_permissions.sh` (12:18): `enable` = 1 after udev trigger, rule has the retry
- [x] **User:** `misc/crash_debug/arm_crash_capture.sh` (12:18): `efi_pstore` backend registered, lockup/hung/RCU
      panics on, `kernel.panic=10`, SysRq 1
- [x] `scripts/status.sh` works without a password (udev rule + exact sudoers lines): 200.000 MHz accel, 50.000 MHz

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

**Bisection of the freeze (in progress).** Steps B and C should run with crash capture armed:
- [x] **A: our design loaded, no driver, Vivado load → no freeze.** Base SoC JTAG-loaded + rescan at 11:34:36,
      XADC watch for 9 min (39 °C, flat), then the probe SoC Vivado build 11:45:08–11:49:58 on top. Still fine at
      11:51 (**17 min** after the load, past both freeze windows of 9 and 14 min). The only kernel messages are the
      expected AER lines from the JTAG reload (11:34:41: root-port RxErr + CmpltTO when the link drops). Together with
      the clean 12 h MMIO-only session on 2026-11-20, this points at the **LitePCIe driver/DMA path** as the trigger.
      Caveat: the Vivado load lasted ~5 min, not the ~45 min assumed.
- [x] **B: driver loaded, MMIO only → no freeze.** Probe build, crash capture armed. `BUILD_DIR=build/dma_probe
      scripts/driver.sh load` at 12:19:09, `info` OK (identifier `... 11:45:08`, scratch OK). Probe at rest: all
      counters 0, `levels_live` = all 8 tags free, `flags_live` 0x2d82 (reader idle, completion path ready), MRRS 512,
      MPS 256. Watched 15.5 min (`misc/crash_debug/watch_host.sh`, log
      `misc/crash_debug/logs/20261123-122047_stepB_driver_mmio_only.log`): no freeze, 0 IRQs, no kernel messages.
- [x] **C (test part): one DMA test with probe counters**, 12:34:57, `misc/dma_debug/run_probe_test.py --build-dir
      build/dma_probe --seconds 2 --tag stepC_mrrs512`, then `driver.sh unload` at 12:34:59. Same stall as before
      (`0.00 Gbps, TX 135, RX 8, 0 errors`, 1 IRQ), no DMAR/AER. Counters explained below. Log:
      `misc/dma_debug/logs/20261123-123459_stepC_mrrs512.json`.
- [x] **C (wait part): 20 min watch after the test, driver unloaded → no freeze** (freeze #2's conditions did not
      reproduce: watch completed 12:55, 20 min after the test, through a Vivado run).
- **Bisection result so far:** A, B and C did not reproduce a freeze. The freezes remain **unexplained**. The stall's
  garbage DW only went into the driver's own DMA buffers (valid addresses), so it is not an obvious way to corrupt
  kernel memory. Not yet repeated: freeze #1's conditions (several stalled DMA tests in a row, CSR polling via ioctl
  *during* a stalled test, driver left loaded). Crash capture stays armed for all further work, so a new freeze
  leaves a pstore log. Running from 12:35,
      log `misc/crash_debug/logs/*_stepC_after_dma_unloaded.log`. Includes a Vivado run 12:39:51–12:45:18 (the fixed
      probe build), like freeze #2. **No freeze at 12:45 (10 min after the DMA test)**; freeze #2 came at 13 min.
- [ ] If C freezes: read `/var/lib/systemd/pstore/` after the reboot. If not: repeat C a few times (freeze #1 came
      right after DMA tests with the driver still loaded), then MRRS 256/128 (`misc/dma_debug/set_mrrs.sh`).

**Stall root cause (probe counters, step C, 2026-11-23 12:34).**

| Counter | Value | Meaning |
|---|---|---|
| `ctl_rd_reqs` = `rd_split_reqs` = `rd_port_reqs` | 137 | read requests issued (512 B each, `cmp_bytes` 70144 = 137 × 512) |
| `cmp_ends` = `rd_port_cmp_done` | 137 | every request completed and retired: **no lost completions**, `cmp_errs` 0, `ctl_cmp_badtag` 0 |
| `cmp_len64` (all others 0) | 274 | the host answers each 512 B read with **two 256 B completions** (its MPS 256) |
| `cmp_beats` / `cmp_tlps` | 4658 / 274 = **17** | each 256 B completion arrives as **17** 128-bit beats (16 would be the payload size) |
| `rd_split_words` (queued) vs `rd_fifo_out` (dequeued) | 4384 vs 4658 | the reader adds 32 beats per request but removes **34** |
| `pending_min` = `snap_pending` | **−274** = −2 × 137 | shadow of `pending_words`: 2 beats lost per request |
| `pending_max` | 256 | = 8 requests × 32 beats in flight |
| snapshot levels at disable | data FIFO 0, metadata 0, tag queue 8, req queue 0 | controller clean, nothing outstanding; `splitter_valid` 1 (reader wanted to issue more) |

- In `litepcie/frontend/dma.py` (`LitePCIeDMAReader`), `pending_words` += `request_words` = `ceil(length / 16)` =
  32 per issued 512 B request, and −= 1 per beat leaving the data FIFO. The payload of each completion starts behind
  the 3-DW header, so on the 128-bit path it comes as a partial first beat + 15 full + a partial last beat = 17 beats
  (= `ceil((3 + 64) / 4)`). 2 completions per request → 34 beats out per 32 queued.
- The real counter is `Signal(max=data_fifo_depth + 1)`, 11-bit unsigned. While ≤ 8 requests are in flight it stays
  positive, but the deficit grows by 2 per request; after ~128 requests (deficit > 256 in-flight beats) it goes
  "below zero", wraps to ~2046, and the gate `pending_words < data_fifo_depth - max_words_per_request` (= 992)
  closes **for good**. The reader sits in IDLE with a descriptor waiting and everything empty, exactly as observed
  (137 requests ≈ 8.5 descriptors of 8 KB; the earlier "reader at descriptor 7" fits).
- It is a **LitePCIe 2026.08 regression**. The August rework taught the TLP controller that completions can take an
  extra beat (`5b0133a` "account for completion packet rounding": per-tag buffer depth `request_beats + 1`, comment
  "partial first/last Completion TLPs cannot share one"), and `ea77120` started carrying byte enables through the
  completion path, but the reader's `request_words` was not updated. 2026.04 predates all of this.
- **Possible second problem (to check):** when the PHY and DMA widths are equal (128 = 128 here), the reader drops
  the byte enables (`data_fifo.source.connect(data_conv.sink, omit={"be"})`), so the partial beats may pass as full
  beats and corrupt the DMA stream (`wr_beats` = 4658 = every completion beat reached the writer). `dma_test`
  reported 0 errors on the 8 RX buffers it saw; still to be verified with a working reader.
- **Possible tie to the freezes (open):** the counters show the PCIe side clean after the stall (all tags back, no
  outstanding requests, no AER/DMAR), so the stall itself leaves nothing wedged. Whether the host freeze needs DMA
  at all is what the step C watch and repeats decide.

**Deeper cause: the 17th beat comes from wrong RX byte enables in the S7 PHY (code reading, 2026-11-23 12:45).**
- The 128-bit header extracter (`litepcie/tlp/depacketizer.py`, `_LitePCIeTLPHeaderExtracter`) *does* realign the
  payload (output beat = previous beat's DW3 + current DW0..2). A 64-DW completion behind a 3-DW header = 67 DW =
  17 input beats comes out as **16** beats, unless the last input beat claims a valid DW3: `tail_valid =
  sink.be[12:16] != 0` then emits a "tail" beat holding that DW3. For 67 DW, lane 3 of the last beat is empty.
- `litepcie/phy/s7pciephy.py` (128-bit) feeds `m_axis_rx.be` from the core's `m_axis_rx_tkeep` (line 359) and uses
  `rx_is_eof` (`m_axis_rx_tuser[21:17]`) only for `last` (line 520). On the 7-series 128-bit interface the end
  position is in `rx_is_eof[3:0]`, not in `tkeep`, so the last beat claims all 4 DWs → a spurious tail beat on every
  TLP whose last beat isn't full → **17 beats per 256 B completion, the 17th carrying one garbage DW marked valid**.
- Before `f4dbb63` ("tlp/depacketizer: preserve shifted packet tails", 2026-08-25) the extracter did not look at
  `be` for tails, which is why the same PHY worked with 2026.04.
- Consequences: (1) the reader's `pending_words` underflow → stall (above); (2) on equal PHY/DMA widths the reader
  drops `be`, so each spurious beat would put 16 junk bytes after every 256 B into the DMA stream; (3) per-tag
  completion buffers sized for 32 beats per 512 B request receive 34 (in-order arrival hides it; out-of-order would
  deadlock).
- **"0 errors" in `dma_test` means nothing here**: it skips checking for the first 128 × 256 buffers ("Skip the first
  128 DMA loops"), and only 8 buffers ever arrived.

**Fix candidate built (2026-11-23 12:45).** `boards/dupcb/s7pciephy_rx_be.py` (`S7PCIEPHYRXBE`, commit `804148c`):
the RX byte enables of an EOF beat come from `rx_is_eof` (DWs up to the end position), plus the DWs of a straddled
next TLP (`rx_is_sof` at DW2); other beats keep `tkeep`. Simulation `misc/dma_debug/test_rx_be.py`: 8/8 cases pass
(e.g. 256 B completion last beat, EOF at DW2 → `be = 0x0fff`). Not used by `target.py` yet.
Probe SoC with it: `python3 misc/dma_debug/dma_probe_soc.py --build --rx-be-fix` → `build/dma_probe_fix/`
(identifier `... 2026-11-23 12:39:51`), 12:39:51–12:45:18, timing met (WNS +0.029 ns, WHS +0.021 ns). In the
Verilog `m_axis_rx_tkeep` now goes to an unused `tkeep` net instead of the RX byte enables. Same CSR map as
`build/dma_probe`. Driver built (`BUILD_DIR=build/dma_probe_fix scripts/driver.sh build`), not loaded.
Loaded 12:54:36 (`--rx-be-fix --load` + rescan): Gen2 x4, BAR0 128 KB, `enable` = 1 right after the rescan (the udev
retry fix works).

**✅ Fix verified on hardware (2026-11-23 12:54:54, `misc/dma_debug/logs/20261123-125456_fix_mrrs512.json`).**
`run_probe_test.py --build-dir build/dma_probe_fix --seconds 2`, IOMMU on:
- `dma_test`: **13.43 Gb/s** loopback (≈1.68 GB/s per direction at the same time; Gen2 x4 is 16 Gb/s raw per
  direction), TX 370497 / RX 370337 buffers, **0 errors**. That is far past the 128 × 256-buffer skip, so the data
  *was* checked this time. 12852 IRQs.
- Probe: 6552656 read requests of 512 B, all ended, 0 error completions, 0 bad tags. `rd_split_words` =
  `rd_fifo_out` = `cmp_beats` = 209684992, so queued == dequeued; `pending_min` 0, `pending_max` 256. Completion mix:
  len16 37726, len32 55525, len64 13040218, len128 0, len_other 37200. Average 15.92 beats per completion; the RX byte enables now end the TLPs right.
- Controller clean afterwards (8 tags free, queues empty). No DMAR/AER.
- Kernel log: "Reading too late / Writing too late, N buffers lost" from the driver: user space did not keep up
  with the free-running loopback ring. A software pacing message, not a hardware fault.
- **Applied to production (2026-11-23 13:00):** `boards/dupcb/target.py` now builds the PHY with `S7PCIEPHYRXBE`
  (import from `boards/dupcb/s7pciephy_rx_be.py`, comment at the PCIe section). The probe SoC follows `target.py` by
  default; `--stock-phy` swaps LitePCIe's `S7PCIEPHY` back in (`build/dma_probe_stock/`, reproduces the stall).
  Existing build dirs: `build/dma_probe/` was built with the stock PHY (11:45), `build/dma_probe_fix/` with the fix
  (12:39); a new default probe build will overwrite `build/dma_probe/` with the fixed PHY.
  Not packed yet: `setup.litex.du-pcb.py --pack` would need `boards/dupcb/s7pciephy_rx_be.py` added to `FILES`
  (open decision, see "Open issues").
- Base SoC rebuilt with the fix: 12:56:22–13:01:44 (`scripts/build.sh`, log `build/base_rxbe_build.log`), identifier
  `LiteX SoC on DU-PCB-001-003 2026-11-23 12:56:22`, timing met (WNS +0.049 ns, WHS +0.015 ns),
  `m_axis_rx_tkeep` → unused `main_tkeep` net (fix present).
- Loaded 13:01:57 (`scripts/load.sh` + `scripts/pcie_rescan.sh`): Gen2 x4, BAR0 128 KB, `enable` = 1.
- Driver rebuilt for this build (13:02), loaded, `info`: identifier `... 12:56:22`, scratch register OK.
- **✅ DMA on the production base SoC (13:02:34, IOMMU on):** `scripts/driver.sh dma-test -w 32 -t 10`: steady
  **13.42–13.44 Gb/s** loopback, ~2.0 M buffers of 8 KB each way (~16.5 GB per direction), **0 errors**, 72928
  IRQs. Kernel: only the AER lines of the JTAG reload (13:02:01) and 42 driver "too late" pacing notices.
- `scripts/status.sh` (13:03, driver unloaded, BAR0 via `litex_server`, no password): identifier `... 12:56:22`,
  `accel_freq` 200000288 Hz, `clk50_freq` 50000158 Hz, `leds_out` 0.
- Freeze watch after the working DMA run (`misc/crash_debug/watch_host.sh 20 60 after_fixed_dma`, 13:03–13:23, crash
  capture armed): **no freeze**. Uptime 1 h 55 min at 13:23 with 3 bitstream loads, 3 DMA test runs (1 stalled,
  2 working) and 4 Vivado builds this boot.
- **Decision (user, 2026-11-23 13:25): move on, watch passively.** The freezes are treated as a likely side effect of
  the stalled DMA (now fixed), not provoked on purpose. **Rule for all further board work: run
  `misc/crash_debug/arm_crash_capture.sh` once per boot and keep the IOMMU on**, so a new freeze leaves a pstore log
  (`/var/lib/systemd/pstore/`). If one happens: stop, read the log, record it here.
  Not done (kept for later if freezes return): freeze #1's conditions with the stock-PHY probe build
  (`build/dma_probe/` from 11:45 or `--stock-phy`): several stalled DMA tests in a row, CSR polling during a stalled
  test, driver left loaded. Expected: 16 beats per completion,
`pending_min` ≥ 0, DMA running at speed, then a test longer than the 128-loop skip to check data.

**Fix options for the stall** (option 1 is built and waiting for its hardware test):
1. **Fix the RX byte enables in a PHY subclass in this repo (recommended).** `S7PCIEPHY` instantiates the core in
   `do_finalize()` from `self.pcie_phy_params`, so a subclass can, before finalize, replace
   `o_m_axis_rx_tkeep` with a dummy and drive `rx_datapath.sink.be` itself from `rx_is_eof` (last beat: DWs up to the
   end position) and `rx_is_sof` (straddled next TLP in the upper half). No change to `deps/`; keeps 2026.08; fixes
   all three consequences. Check: probe shows 16 beats per completion, `pending_min` ≥ 0, DMA runs at speed, and a
   longer `dma_test` (past the 128-loop skip, a few seconds) checks data. Report upstream (LitePCIe).
2. Pin LitePCIe to **2026.04** in `setup.litex.du-pcb.py`. Simplest, but drops the 2026.08 fixes, and the PHY bug
   stays latent.
3. Patch `deps/litepcie` (reader accounting or PHY) as a patch file applied by the setup script: only if 1 is not
   enough.

**Probe SoC build (2026-11-23 11:45–11:50, `build/dma_probe/`).** Identifier `LiteX SoC on DU-PCB-001-003
2026-11-23 11:45:08`. Timing met: WNS +0.033 ns, TNS 0, WHS +0.016 ns. 37 `dma_probe_*` CSRs. `pcie_dma0` moved
from 0xf0004800 to 0xf0005000 (CSR regions are allocated by name), so the probe build has its own driver; never use
`build/dupcb/driver` with it. The TLP controller only exists after the PCIe crossbar's finalize, so
`DMAProbeSoC.finalize()` finalizes the endpoint first and then adds the probe. Fast check and full build print
the same LiteX warning as the base build (`Combinatorial cycle detected: ...accel_freq_gray_decoder...`, a false
positive in FreqMeter).
Loaded at 11:51:37 (`python3 misc/dma_debug/dma_probe_soc.py --load` + `scripts/pcie_rescan.sh`): Gen2 x4, BAR0
128 KB. Driver built against this build (`BUILD_DIR=build/dma_probe scripts/driver.sh build`), not loaded.

**Build time correction.** A full build of this SoC takes **~5–6 min** of Vivado here (base and probe alike), not
the 30–60 min in the README or the ~45 min I assumed (the plan's "about 50 min" came from the board notes; corrected
in the plan). So the DSE can afford many builds.

### Phase 2: DMA ↔ DDR 🟡

**Design notes (2026-11-23 13:35, from reading LiteDRAM/LitePCIe/driver code):**
- DDR side: reuse LiteDRAM's `LiteDRAMDMAWriter` / `LiteDRAMDMAReader` with `with_csr=True` (base, length,
  enable, done, loop, offset CSRs; address generated inside). One write port + one read port per channel.
- Bridge (`hw/gemm/ddr_dma.py`): `pcie_dma0.source` (128 b) → `stream.Converter(128, 512)` → writer of the selected
  channel; reader of the selected channel → `Converter(512, 128)` → `pcie_dma0.sink`. The host disables the DMA
  loopback (it stays in the SoC for `dma_test`). Data that arrives while the selected writer is not in RUN is
  **dropped** (the writer stops accepting in DONE, which would otherwise back up into PCIe). `drained` per channel =
  writer done and its data FIFO empty (command and data leave together, so the writes are in the controller).
- **Host side (driver loop mode):** the driver runs both DMA directions in loop mode over a 256 × 8 KB ring; the
  hardware reader streams the ring from the moment it is enabled, filled or not. So host → DDR uses **zero-copy**
  (mmap of the ring), fills the whole ring first, then enables the reader; the bridge takes `length` bytes and drops
  the repeats; > 2 MB goes in 2 MB chunks. DDR → host: enable the DMA writer, start the bridge reader (lengths in
  multiples of 8 KB so every descriptor completes), poll `pcie_dma0_writer_table_loop_status` (exact; the IRQ-based
  counts only move every 32 buffers); the CSR read also flushes the posted writes ahead of it.
- No CPU (`cpu_type=None`): CH0 stays `add_sdram` (its Wishbone port is idle without a CPU), so the CSR names stay
  and `ddr_init.py` is unchanged. DDR init comes from the host only (`scripts/ddr_init.sh`).

- [x] `hw/gemm/gemm_soc.py` (`GemmSoC(BaseSoC)`, `cpu_type=None`): fast check OK (2026-11-23 13:45). DDR CSR names
      unchanged (`sdram*`, `ddrphy*`, `ddrclk*`). **Without a CPU LiteX puts the CSR region at 0x0** (not
      0xf0000000): the generated driver (`CSR_BASE 0x0L`), `ddr_init.py` (reads `mems.csr.base`) and `litex_server`
      handle it; `misc/dma_debug/csr_peek.py` now reads the base from `csr.csv` too.
- [x] `hw/gemm/ddr_dma.py` (`PCIeDDRBridge`): 30 CSRs `ddr_dma_{sel,drained,clear,h2c_beats,h2c_dropped,c2h_beats}` +
      `ddr_dma_ch{0,1}_{writer,reader}_{base,length,enable,done,loop,offset}`. No separate Migen sim test (the
      hardware test with the C++ tool covers it; a sim would need a DRAM model); noted as a deviation from the plan.
- [ ] Full build of `GemmSoC`
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
| 2026-11-23 | flash image | XADC (JTAG) | 47 °C, VCCINT 0.957 V, VCCAUX 1.814 V, VCCBRAM 0.958 V |
| 2026-11-23 | base SoC, no driver | XADC over 9 min | 38.6–40.5 °C (flat), VCCINT 0.956–0.957 V, VCCAUX 1.800–1.802 V |
| 2026-11-23 | base SoC, no driver | freeze bisection step A | no freeze in 17 min incl. a Vivado run |
| 2026-11-23 | base / probe SoC | full Vivado build time | ~6 min / ~5 min |
| 2026-11-23 | probe SoC | Vivado timing | WNS +0.033 ns, WHS +0.016 ns (met) |
| 2026-11-23 | probe SoC, driver, MMIO only | freeze bisection step B | no freeze in 15.5 min, 0 IRQs |
| 2026-11-23 | probe SoC, IOMMU on | DMA loopback + probes | 137 reqs × 512 B, 274 cpl × 256 B, **17 beats/cpl**, pending −2/req → stall |
| 2026-11-23 | probe SoC + RX be fix | Vivado timing | WNS +0.029 ns, WHS +0.021 ns (met) |
| 2026-11-23 | probe SoC + RX be fix, IOMMU on | **DMA loopback 2 s** | **13.43 Gb/s, 0 errors** (370k buffers), queued == dequeued |
| 2026-11-23 | base SoC + RX be fix | Vivado timing | WNS +0.049 ns, WHS +0.015 ns (met) |
| 2026-11-23 | base SoC + RX be fix, IOMMU on | **DMA loopback 10 s** | **13.42–13.44 Gb/s, 0 errors**, ~2.0 M buffers each way |

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

### 2026-11-23 13:25 (phase 1 closed for DMA, phase 2 started)
- User decision: don't provoke the freeze; arm crash capture every boot, keep the IOMMU on, continue the plan.
- Phase 2 started.

### 2026-11-23 13:23 (phase 1, freeze watch after working DMA)
- 20 min watch after the working 10 s DMA run: no freeze. Asked the user whether to provoke freeze #1's conditions
  with the stock-PHY build.

### 2026-11-23 13:03 (phase 1, DMA works on the production SoC)
- Base SoC rebuilt with `S7PCIEPHYRXBE` (timing met), loaded, driver rebuilt: DMA loopback 13.43 Gb/s, 0 errors over
  10 s. The generated LitePCIe driver + DMA path is now verified on this card (it never was before).

### 2026-11-23 12:55 (phase 1, DMA fixed)
- Step C passed (no freeze 19 min after a DMA test, through a Vivado run).
- Fixed probe SoC loaded, DMA test: **13.43 Gb/s loopback, 0 errors**. Root cause of the stall confirmed and fixed:
  S7 PHY 128-bit RX byte enables from `tkeep` instead of `rx_is_eof`.

### 2026-11-23 12:46 (phase 1, fix candidate built)
- `S7PCIEPHYRXBE` written, simulated (8/8), committed; probe SoC built with it (`build/dma_probe_fix/`, timing met),
  driver built. Step C watch: no freeze 10 min after the DMA test, through a Vivado run.
- From now on the progress file is committed after every step (the host may freeze).

### 2026-11-23 (phase 1, stall root cause found)
- User armed crash capture and installed the udev fix (12:18).
- Step B (probe build, driver loaded, MMIO only, 15.5 min): no freeze.
- Step C test: one DMA run with probe counters. **Stall explained:** each 256 B completion arrives as 17 beats; the
  LitePCIe 2026.08 reader queues 32 beats per 512 B request but dequeues 34, so its 11-bit `pending_words` wraps
  after ~128 requests and the reader stops for good. A regression from the Aug 2026 completion-path rework. Fix
  options written down (pin 2026.04, or patch the reader).
- Fixed `watch_host.sh` (grep with no match under `pipefail` ended the script after one minute).
- Step C 20-min watch running (driver unloaded since 12:34:59).
- Code reading: the 17th beat is a spurious "tail" made by the reworked header extracter because the S7 PHY takes
  the 128-bit RX byte enables from `tkeep` instead of `rx_is_eof`. Recommended fix: PHY subclass in this repo.
  `dma_test`'s "0 errors" is meaningless for short runs (it skips the first 128 × 256 buffers).

### 2026-11-23 (phase 1, bisection step A passed, probe SoC ready)
- Step A: our design on the card for 17 min without the driver, through a Vivado run: no freeze. Trigger is most
  likely the driver/DMA path.
- Probe SoC built (timing met) and loaded; its driver built. Waiting for the user to arm crash capture
  (`misc/crash_debug/arm_crash_capture.sh`) and re-run `sudo scripts/setup_permissions.sh` before steps B and C.
- Corrected the build-time figure: ~5–6 min per full build, not ~45 min.

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
