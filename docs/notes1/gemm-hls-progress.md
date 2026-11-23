# GEMM HLS accelerator: progress

Plan: [gemm-hls-plan.md](gemm-hls-plan.md). Update this file whenever a task is done, blocked or changes direction. Newest log
entries go on top. Dates are absolute (YYYY-MM-DD).

Status legend: ✅ done · 🟡 in progress · ⬜ not started · ⛔ blocked

## Phase overview

| Phase | Content | Status |
|---|---|---|
| 0 | Scaffolding: submodule, docs, `.gitignore`, README | ✅ 2026-11-23 |
| 1 | Host DMA bring-up (LitePCIe driver, `litepcie_util dma_test`) | ✅ DMA works (13.43 Gb/s, 0 errors) after the S7 PHY RX byte-enable fix · ⚠️ host freeze cause still open (capture armed) |
| 2 | `GemmSoC` (no CPU) + PCIe-DMA ↔ DDR bridge + `ddr_dma_test` | ✅ 2026-11-23 14:09: round trips clean on both channels, 0.82/0.84 GB/s (optimisation pending) |
| 3 | Kernel port to `kernels/gemm/`, config rendering/validation, HLS step | ✅ csynth + IP export through `HlsStep` in every build; g++ simulation passes (Vitis csim wired via `hls.run_csim`, not used) |
| 4 | SoC integration: `HlsKernel`, perf counters, Vivado Tcl hooks, first full build | ✅ 2026-11-23 17:04: builds, meets timing at 200 MHz |
| 5 | `gemm_runner` end to end, verified results and counters | ✅ 2026-11-23 17:06: sizes 256³–2048³ + non-multiples verified, 23.4 GFLOP/s (91.5 % of peak), counters = analytic traffic |
| 6 | `gemm_build.py` complete (stages, caching, manifest) | ✅ stages + stamps (unchanged steps skipped, HLS IP shared across Vivado knobs); DSE builds now also write `reports/summary.json` |
| 7 | `gemm_dse.py` + example campaigns | ✅ 12 campaigns run (PE, transposed A, tiles, memory maps, sizes, clocks, Vivado presets); best 110.9 GFLOP/s (PE 32×8, 225 MHz) |
| 8 | README usage + final notes | 🟡 README GEMM section + measured table, summary and lessons above; final clock numbers pending |

## Summary and lessons (2026-11-23, 22:45)

**Where it stands** (best result updated 2026-11-24 00:41). Every piece of the plan works end to end on the card, unattended: JSON config → HLS →
LiteX/Vivado → JTAG load → PCIe rescan → driver → DDR calibration → host tools → GEMM runs with verification →
JSONL/CSV. The DSE driver ran 12 campaigns (20 Vivado builds, 21 automatic bring-ups, 206 run rows, all verified
OK) with no manual step and no host problem.

| What | Result |
|---|---|
| Best kernel | PE 32×8, transposed A, tile 1024×512, **225 MHz** (`aggressive` preset): **110.86 GFLOP/s** at 8192³ (96.2 % of 115.2); 152k LUT (51 %), 1320 DSP (69 %), 654.5 BRAM tiles (69 %). At 200 MHz (default preset): 98.59 |
| Same, end to end | 8192³ in 10.77 s (kernel 9.92 s; the rest is DMA of 768 MB in, 256 MB out) |
| Highest clocks | PE 8×8 at 250 MHz: 29.02 GFLOP/s (90.7 %). PE 32×8 at 225 MHz with `explore`/`aggressive` (default preset misses by 41 ps); 250 MHz misses by 0.78 ns |
| Host ↔ DDR | H2C 0.82 GB/s, C2H 1.49 GB/s (PCIe Gen2 x4 slot; raw DMA loopback 1.68 GB/s) |
| DDR3 | both channels calibrated by the host, 1 GB BIST clean, ~7.1–7.2 GB/s each |
| Build times | HLS 0.5–4 min (shared across Vivado knobs); Vivado PE 8×8 12–14 min, PE 32×8 ~25 min |

**Lessons (the non-obvious ones; details in the phase sections):**
1. **Old hlslib vs Vitis HLS 2026.1:** arrays of `hlslib::Stream` fail (HLS 214-177); the kernel uses `hls::stream`
   in synthesis and keeps hlslib for simulation and DataPack. `vitis-run` exits 0 even when csynth fails, so the log
   is scanned for `ERROR:`.
2. **LitePCIe 2026.08 + stock `S7PCIEPHY`:** RX byte enables taken from `tkeep` add a garbage beat to every read
   completion, and DMA stalls after ~64 KB. Fixed PHY: `boards/dupcb/s7pciephy_rx_be.py`.
3. **Order on the card:** the driver's probe resets the SoC, so DDR calibration must come after the driver load.
4. **Vivado `Route 35-13`** on driverless LUTRAM nets in constant/unused AXI converter FIFOs: use single-direction
   converters and our own `AxiWriteToNative` for C.
5. **The A read path decides PE 32×8:** A is read with single-beat strided accesses unless the host sends Aᵀ
   (`transposed_a`); then larger tiles, the N tile first (fewer passes over B). 82 → 98.6 GFLOP/s.
6. **The kernel computes whole memory tiles and silently needs whole bus words:** small matrices run at a
   fraction of the peak (pick the tile for the workload), and sizes that are not bus-word multiples gave wrong
   results without an error until `gemm_runner --multiples` began rejecting them.
7. **The HLS clock estimate is not a limit:** with a tighter target HLS pipelines deeper, and Vivado closed PE 8×8 at
   250 MHz although HLS estimated 4.24 ns. Only a routed build decides. For PE 32×8 at 225 MHz the Vivado preset
   decided (default −0.041, aggressive +0.049 ns): try the presets before changing the kernel.
8. **The marginal path in every build is in the PCIe hard block** (Gen2 x8 core clock at 500 MHz, 0 logic levels,
   −0.009 … +0.076 ns), not in the kernel. Open decision below.
9. **DMA speed was the driver, not the link:** `udelay(1000)` in each DMA stop cost ~40 % of the time. C2H was fixed by
   enabling the writer once per transfer (0.84 → 1.49 GB/s); H2C needs a driver/gateware decision.
10. **Host freezes:** two hard freezes came early, when the DMA stalled (before the PHY fix). Since the fix and with the
    IOMMU on: none in 11 h of builds, reloads and DMA (up since 11:27). A JTAG reload in the running PC always gives an
    AER burst on root port `80:1c.4` (correctable errors plus one non-fatal completion timeout) while the link is down.
    No AER at all during DMA or GEMM runs.

## DDR memory map: where the matrices are

The card has two independent DDR3 channels, **CH0 and CH1, 2 GB each** (64-bit DDR3-1000, 512-bit native port).
Addresses below are **byte addresses inside a channel** (0x0000_0000 … 0x7FFF_FFFF); there is no common address space
across channels (CPU-less GemmSoC; the kernel's AXI masters and the host DMA bridge address one channel each).

**Which channel each matrix is on** is fixed per build by `memory_map` in the build config (`configs/gemm/*.json`):
`hw/gemm/gemm_soc.py` connects each kernel AXI master (A = `gmem0`, B = `gmem1`, C = `gmem2`) to a crossbar port of
that channel. The host must use the same map: `scripts/gemm_run.py` / `gemm_dse.py` read it from the build's
`config.resolved.json` and pass it to `gemm_runner --map`. Default (all builds so far):

```
memory_map: {"a": "ch0", "b": "ch1", "c": "ch1"}
```

**Where inside the channel** (`sw/gemm_host/tools/gemm_runner.cpp`):
- A at offset 0 of its channel.
- B at offset 0 of its channel, or at **0x2000_0000 (512 MB)** if it shares the channel with A.
- C at offset 0 of its channel, or at **0x4000_0000 (1 GB)** if it shares the channel with A or B.

With the default map this gives:

| Matrix | Shape (float32, row-major) | Channel | Start | End (exclusive) | Size | Room before the next region |
|---|---|---|---|---|---|---|
| A | N × K | **CH0** | 0x0000_0000 | 4·N·K (rounded up to 64 B) | 4·N·K B | whole 2 GB channel |
| B | K × M | **CH1** | 0x0000_0000 | 4·K·M (rounded up to 64 B) | 4·K·M B | 1 GB (C starts at 1 GB) |
| C | N × M | **CH1** | 0x4000_0000 | 0x4000_0000 + 4·N·M | 4·N·M B | 1 GB (to the end of CH1) |

Example 2048³: A = 16 MB at CH0 0x0000_0000–0x00FF_FFFF; B = 16 MB at CH1 0x0000_0000–0x00FF_FFFF; C = 16 MB at
CH1 0x4000_0000–0x40FF_FFFF.

Details that matter:
- **Traffic per channel (default map):** CH0 carries only the A reads; CH1 carries the B reads and the C writes. For
  1024³ the counters showed 32 MiB of A reads on CH0, and 32 MiB of B reads + 4 MiB of C writes on CH1.
- **Host DMA lengths** are whole 64-byte DDR words (A and B are zero-padded to that). C is read back in whole 8 KB DMA
  buffers (at least 2), so the DMA may read up to ~16 KB past the end of C. Harmless reads, but keep any other data out
  of that slack.
- **Kernel reads past A/B:** for N or M not a multiple of the 128-row/column tile, the kernel reads whole tiles of
  A (and B), i.e. past the end of the matrix. Harmless (reads only), but A/B must not end within one tile of the
  channel end. The kernel **writes only the valid N × M part of C** (`WriteC` checks the bounds).
- **Calibration overwrites DDR:** `scripts/ddr_init.sh` runs a 1 GB BIST from address 0 on both channels, so any
  data in the first 1 GB of either channel is destroyed. Calibrate first, then load matrices (the bring-up does this
  order; every run re-sends A and B anyway).
- **Capacity limits with the default map:** A ≤ 2 GB (N·K ≤ 512 M floats), B ≤ 1 GB (K·M ≤ 256 M floats),
  C ≤ 1 GB (N·M ≤ 256 M floats). `gemm_runner` checks them before any transfer.
- **Transposed-A builds** (`kernel.transposed_a = true`): the A region holds Aᵀ (K × N, row-major, same size), and
  N must be a multiple of 16.
- The DMA bridge (`hw/gemm/ddr_dma.py`, `sw/gemm_host/src/ddr_dma.cpp`) uses the same per-channel byte addresses; the
  host picks the channel with `ddr_dma_sel`.

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

### Phase 2: DMA ↔ DDR ✅

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
      `ddr_dma_ch{0,1}_{writer,reader}_{base,length,enable,done,loop,offset}`. Simulation test
      `hw/gemm/tests/test_ddr_dma.py` (LiteDRAM's `DRAMMemory` model, random gaps/back-pressure, drop path, aborted
      transfer): PASS. Optional `with_debug` (C2H pattern generator + protocol watchers, `--ddr-dma-debug`), built
      once (`build/gemm_soc_dbg/`), not needed in the end.
- [x] Bridge fix before the first hardware test (13:55): drop H2C data *before* the 128→512 converter while the selected
      writer is not running and hold the converter in reset then (a transfer stopped mid-stream must not leave a
      partial DDR word for the next one, and stale data must never wait in front of a writer that starts later);
      the C2H converter is in reset while its reader is disabled. C2H transfers are at least 2 DMA buffers (after a
      table reset `loop_status` reads (0, 0) both before and after descriptor 0 retires).
- [x] Full build of `GemmSoC` with the fixed bridge (2026-11-23 13:39:51–13:47:03, `build/gemm_soc/`): timing met
      (WNS +0.080 ns, WHS +0.012 ns); LUT 25522 (8.6 %), FF 19484 (3.3 %), BRAM 73.5 tiles (7.7 %), DSP 0. Identifier
      `LiteX SoC on DU-PCB-001-003 2026-11-23 13:39:51`.
- [x] `sw/gemm_host/` written and compiling cleanly (-Wall -Wextra, 2026-11-23 13:35): `src/card.{hpp,cpp}` (`Card`:
      CSRs via the driver ioctl, zero-copy rings, DMA enable, hardware loop status), `src/ddr_dma.{hpp,cpp}`
      (`DdrDma::write/read`, 2 MB chunks, the protocol from the design notes), `tools/ddr_dma_test.cpp` (random data,
      write, read back, compare, GB/s, `--json`). `Makefile` builds per SoC build: `make -C sw/gemm_host
      BUILD_DIR=build/gemm_soc` → `build/gemm_soc/sw/ddr_dma_test` (needs that build's `driver/user/liblitepcie.a`).
- GemmSoC driver built + loaded (13:48), `info` OK (identifier `... 13:39:51`, scratch OK); `ddr_dma_test` rebuilt
  against this build's final headers.
- [x] **`ddr_dma_test` on hardware: CH0 and CH1 round trips clean** (2026-11-23 14:08, after `driver.sh load` →
      `BUILD_DIR=build/gemm_soc scripts/ddr_init.sh` (driver path, 15 s, both PASS)): 1 MB random and 64 MB counter
      pattern OK on both channels; single-word BISTs 0 errors; no AER/DMAR. Throughput with 2 MB chunks: H2C 0.82 GB/s,
      C2H 0.84 GB/s (64 MB). The per-chunk DMA start/stop (the driver reprograms 256 descriptors over MMIO) dominates;
      to optimise later. Qualification (14:08): 256 MB × 3 repetitions × both channels OK (H2C 0.82, C2H 0.84 GB/s,
      `build/gemm_soc/logs/ddr_dma_256mb.log`); 64 MB at 0x40000000 and 0x7c000000 (top of the 2 GB) OK on both.
      History of the debugging below. **First runs (13:48–14:05) failed:**
  - Mechanics work: `h2c_beats` = 16384 (1 MB), `c2h_beats` = 16384, `h2c_dropped` 1024 (ring repeats after `length`),
    no AER/DMAR, no timeouts. Speed with 1 MB chunks ≈ 0.57 GB/s H2C, 0.6 GB/s C2H; 4 MB ≈ 0.8 GB/s.
  - Data: counter pattern (`--pattern counter`) shows read word *i* = written word *i*+4 at the start (32 B shift),
    then from ~2.4 KB on, in every 64 B DDR word the **lower 32 B are right and the upper 32 B wrong**.
  - **C2H is nondeterministic:** two reads of static DDR content (BIST-written, `--read-only-bist`) differ in 22–23 %
    (CH1) / 40 % (CH0) of the 8-byte words, starting at ~0x970 both channels.
  - Ruled out: zero-copy/cache coherence (`litepcie_util -z dma_test` loopback 13.4 Gb/s 0 errors), PCIe writer
    buffering depth (onset does not move with `pcie_dma0_buffering_writer_fifo_control` 1024/256/64), FIFO memory
    mapping (all four 32×512 bridge FIFOs are RAM32M distributed RAM), bridge logic against a single-outstanding DRAM
    model (`hw/gemm/tests/test_ddr_dma.py` PASS incl. back-pressure, drop path, aborted transfer).
  - The onset (~2.4 KB) is just past the LiteDRAM reader FIFO depth (32 × 64 B = 2 KB).
  - Next: debug build (`gemm_soc.py --ddr-dma-debug` → `build/gemm_soc_dbg/`): C2H test-pattern source straight into
    the PCIe writer (bypasses DDR/reader/converter) + protocol watchers (rdata overflow, data change / valid drop while
    stalled at reader output, converter output, PCIe writer input).
  - **ROOT CAUSE (14:08): the LitePCIe driver's probe resets the SoC** (`litepcie_writel(CSR_CTRL_RESET_ADDR, 1)`,
    "Reset LitePCIe core", main.c:1236), which clears every DDR PHY calibration register. `ddr_init.py` ran at
    13:47:57, the driver was loaded at 13:48:31, so **all DDR tests ran on an uncalibrated DDR**. Proof: a plain BIST
    (`misc/dma_debug/bist_probe.py`, same procedure as `ddr_init.py`) now fails on every word (16 MB sequential:
    262144/262144 bad; single words: all bad), although `ddr_init.py`'s 1 GB BIST passed before the driver load. The
    "half-burst" shifts and read-to-read differences are an uncalibrated read path; the bridge was fine (simulation).
    Probably also the cause of the unexpected completion (UnxCmplt) logged at that probe.
  - **Fix:** calibrate *after* loading the driver. `ddr_init.py` can now go through the driver (`--dev /dev/litepcie0`,
    `Driver(CSRBuilder)` using `LITEPCIE_IOCTL_REG`; the kernel refuses the BAR0 mmap while the driver is bound), and
    `scripts/ddr_init.sh` uses it automatically when `/dev/litepcie0` exists (and honours `BUILD_DIR`).
    Order: load bitstream → rescan → `driver.sh load` → `ddr_init.sh` → DMA tests.
- [x] **`ddr_init.py` passes without a CPU** (2026-11-23 13:47:57–13:48:10, 13 s; `python3 boards/dupcb/ddr_init.py
      --bar .../resource0 --build-dir build/gemm_soc`, log `build/gemm_soc/logs/ddr_init.log`): CH0 window 50 steps (893 ps), CK at +56 (6 s), CH1 window
      80 steps (1429 ps), CK at +72; all lanes 0 failed tests / 0 bit errors; 1 GB BIST per channel, 0 bad words,
      write 7.11 GB/s, read 7.20 GB/s. Result: CH0 PASS, CH1 PASS. (`scripts/ddr_init.sh` is fixed to `build/dupcb`;
      for other builds call `ddr_init.py` with `--build-dir`.)

### Phase 3: kernel port + HLS step ✅
- [x] Sources copied verbatim to `kernels/gemm/` (2026-11-23 13:45; `src/`, `include/`, `tb/test_gemm.cpp` from
      `test/TestSimulation.cpp`, `LICENSE.md`), `ORIGIN.md` written (upstream commits, file map, change list). The g++
      simulation needs `ap_int.h`/`ap_fixed.h`/`hls_stream.h` from `Vitis/common/technology/autopilot`.
- [x] Interface pragmas changed (2026-11-23 13:46): `ap_ctrl_hs`, `m_axi offset=direct` A/B/C with burst/outstanding/
      latency from the config (`MM_PRAGMA` two-level stringify), `ap_none` sizes; stream depths (pipe, aSplit, bMemory,
      cMemory) from the config (0 = upstream default). g++ simulation still passes for the same 4 sizes (warnings only
      inside Xilinx headers). 32-bit addresses: set in the HLS Tcl (`config_interface -m_axi_addr64=false`), next step.
- [x] `flow/gemmflow/config.py` (2026-11-23 13:50): dataclasses `KernelCfg`/`MemoryMap`/`ClockCfg`/`HlsCfg`/
      `VivadoCfg`/`SocCfg`/`BuildCfg`; unknown keys rejected; every gemm_hls CMake/`static_assert` rule checked
      (plus the transpose warning and a rough DSP budget); Vivado presets resolved (`max_threads` auto = min(8, nproc));
      `render_config_h()` fills `Config.h.in` incl. nested placeholders; `config_hash`, `hls_hash` (kernel + clocks +
      hls + kernel source tree), `build_id` = `<name>-<hash8>`. `configs/gemm/default.json` (f32, PE 8×8, tile 128,
      peak 25.6 GFLOP/s at 200 MHz) loads clean; negative cases (PM=3, tile_m=100, unknown key, bad type) rejected.
- [x] **Baseline g++ simulation of the (almost verbatim) upstream kernel passes** (2026-11-23 13:43): g++ 16.2.1,
      `-std=c++17 -O2 -DMM_DYNAMIC_SIZES -DHLSLIB_STREAM_TIMEOUT=16 -DHLSLIB_LEGACY_SDX=0`, includes: rendered
      `Config.h` (default config), `kernels/gemm/include`, `submodules/gemm_hls/hlslib/include`, and
      **`Vitis/include`** (not `Vitis/common/technology/autopilot`: that one lacks `etc/ap_private.h`). Builds in 3 s.
      N K M = 256 256 256, 257 256 272 (upstream's non-multiple case), 64 32 16, 300 512 128: all verified, each
      < 1 s. One change needed: `Utility.h` without hlslib's OpenCL header (see `kernels/gemm/ORIGIN.md`).
      hlslib `a67e02f` works unmodified with g++ 16 + Vitis 2026.1 headers in simulation.
- [x] `tcl/hls/run_hls.tcl` (inputs only via `params.tcl`, D7), `flow/gemmflow/tools.py` (`Tool` → `VitisHls`,
      `Vivado`: Xilinx env from `config.sh`, log file, `ToolError` with log path), `flow/gemmflow/hls.py` (`HlsRun`:
      `Config.h` + `params.tcl` into `build/gemm/hls/<hls_hash[:12]>/`, stamp file, up-to-date check; `HlsReport`:
      csynth.xml summary + RTL ports). Synthesis cflags as upstream: `-O3 -DMM_SYNTHESIS -DHLSLIB_SYNTHESIS
      -DMM_DYNAMIC_SIZES -DHLSLIB_STREAM_TIMEOUT=16 -DHLSLIB_LEGACY_SDX=0` (2026-11-23 13:46)
- [x] First HLS run of `default.json` (csynth + IP export): working since attempt 3 (details below). **Attempt 1 (13:46) failed in 19 s:**
      `vitis-run --mode hls --tcl` accepts the legacy Tcl (`open_project`/`open_solution`, warns it won't show in the IDE),
      but csynth stops with `ERROR: [HLS 214-177] ... Cannot apply disaggregate pragma/directive on object 'aSplit' /
      'aPipes' / 'bPipes' as there is an unresolved Phi node on it (Top.cpp:125)`. That is the hlslib-vs-2026.1 risk
      from the plan (gemm_hls was tested with Vitis 2021.1): arrays of `hlslib::Stream` objects indexed in the unrolled
      PE loop. Also found: **`vitis-run` exits 0 even when csynth fails**, so `VitisHls.run_tcl` now scans the log for
      `ERROR:` lines and raises `ToolError`.
      **Fix (14:01):** in synthesis `Stream` is now `hls::stream<T, depth>` (native, supports arrays; `hls::stream<T,N>`
      derives from `hls::stream<T>` so `Stream<T>&` parameters still bind); simulation keeps `hlslib::Stream`; call sites
      `Push/Pop` → `write/read` (35). g++ simulation still passes.
      **Attempt 2 (14:02, 33 s): csynth + IP export OK** (`build/gemm/hls/40084106d9c3/prj/sol/impl/ip`, component.xml).
      Estimates: DSP 381, BRAM18 313, FF 79183, LUT 72947; latency undef (dynamic sizes); 147 RTL ports (m_axi
      gmem0/1/2 with 512-bit data, 32-bit address, ap_ctrl_hs, a/b/c address and size ports). **Estimated clock 10.648 ns
      vs 5 ns**: all modules are at 4.0–4.2 ns (> 3.65 ns budget with 27 % uncertainty, < 5 ns) except
      `ConvertWidthC` (Memory.cpp:335): a 512-bit dynamic index between the stream read and a conditional write in an
      II=1 loop. **Fix (14:06):** read the packs of one memory word in one unrolled iteration (II = packs per word);
      simulation passes.
      **Attempt 3 (14:04, 33 s): estimated clock 4.236 ns ≤ 5 ns target** (all modules 4.1–4.2 ns; above the 3.65 ns
      budget that includes the 27 % uncertainty, so Vivado timing is the real check). DSP 381, BRAM18 313, FF 78062,
      LUT 67795. IP in `build/gemm/hls/bc6119de37db/prj/sol/impl/ip`.
- [x] hlslib with Vitis HLS 2026.1 synthesis: hlslib headers stay unmodified, but the kernel uses `hls::stream`
      instead of `hlslib::Stream` in synthesis (arrays of hlslib streams fail, HLS 214-177); DataPack etc. still hlslib

### Phase 4: SoC integration ✅
- [x] `hw/gemm/hls_kernel.py` (2026-11-23 14:15): `HlsGemmKernel`, Instance of `gemm_kernel_ip` generated from the
      csynth.xml port list (147 ports); `ap_ctrl_hs` (start held until `ap_ready`), active-low reset from the accel reset
      + `soft_reset` CSR; CSRs `gemm_{start,soft_reset,status(done,idle,busy),build_id,cycles,a,b,c,size_n,size_k,
      size_m}`; CDC: start via `PulseSynchronizer`, quasi-static args/soft reset and status/counters via `MultiReg`.
      Each m_axi bundle → LiteX `AXIInterface` (512 b, 32-bit addr, ID 1; AXI3 2-bit LOCK truncated, WID and user
      signals dropped/tied) → `LiteDRAMAXI2Native` (w/r buffers 32/64) → `get_port(mode="both", clock_domain="accel")`
      on the channel from `memory_map`.
- [x] `hw/gemm/axi_perf.py`: `AxiPerfMonitor` per bundle (ar/aw/r/w/b counts, ar/aw/w stalls, r_wait, r_backpressure,
      b_wait, rd_outstanding_sum, rd_lat sum/min/max via AR timestamp FIFO, ts_overflow); cleared at start, counting
      while busy; exposed as `gemm_perf_<a|b|c>_<counter>`. No separate sim test yet.
- [x] `tcl/vivado/import_hls_ip.tcl` (ip_repo_paths → update_ip_catalog → create_ip → generate_target → synth_ip),
      hooked through `platform.toolchain.pre_synthesis_commands` with `::P(...)` set by `gemm_soc.py` (no Tcl braces:
      LiteX `str.format()`s the hook commands). `project_settings`, `floorplan`, `reports_json`, `program` Tcl: later.
- [x] `gemm_soc.py --gemm-config <json>`: accel clock from the config, kernel added (needs the HLS run of that config),
      output `build/gemm_soc_<build_id>/`. Fast check OK: 59 `gemm_*` CSRs, `gemm_kernel_ip` black box, hook in the Tcl.
- [x] First full build with the kernel (OK at build 4, see below); history: started 2026-11-23 ~14:12 in the background
      (`python3 hw/gemm/gemm_soc.py --gemm-config configs/gemm/default.json --build --vivado-max-threads 8`, log
      `build/gemm_soc_kernel_build.log`, output `build/gemm_soc_f32_pe8x8_t128x128-4e52e7f5/`). **Failed (14:15):**
      `ERROR: [Synth 8-11365] ... named port connection 'm_axi_gmemX_*USER' does not exist`: csynth.xml lists the AXI
      user ports but the packaged IP has none. Fixed in `hls_kernel.py` (skip `m_axi_*USER`); **rebuild not run yet**.
      Also: `CRITICAL WARNING [Vivado 12-5447] synth_ip is not supported in project mode` (from import_hls_ip.tcl);
      check whether the IP then synthesizes with the design (it should) or drop `synth_ip` from the Tcl.
      **Build 2 (16:27–16:38):** synth + place OK (LUT 65438 = 21.9 %, FF 88266 = 14.8 %, BRAM 294.5 tiles = 30.8 %),
      **route failed:** `ERROR: [Route 35-13] Driverless net ... storage_58/52 ... DPRA0..4`: the write-response FIFOs
      of the `LiteDRAMAXI2Native` on the read-only bundles (A, B). The unused direction is constant once the IP netlist is
      linked and Vivado leaves the LUTRAM read-address pins without a driver. **Fix:** only the used direction per
      bundle (`LiteDRAMAXI2NativeR` for A/B, `LiteDRAMAXI2NativeW` for C, `cmd_grant` = 1). Fast check OK; build 3 next.
      **Build 3 (16:39–16:49):** A/B fixed, still `Route 35-13` on C's write-response FIFO (`LiteDRAMAXI2NativeW`):
      its payload is constant (AWID 0, resp OKAY), Vivado drops the LUTRAM read side but keeps the cell. **Fix:**
      `hw/gemm/axi_native.py` `AxiWriteToNative` (per-beat native commands, data+strobe FIFO, B responses from a
      counter); simulation `hw/gemm/tests/test_axi_native.py` PASS (5 bursts 1–256 beats with gaps, 340 words, 5 B).
      **Build 4 (16:52–17:04): OK, timing met** (WNS +0.049 ns, WHS +0.015 ns, accel 200 MHz included): LUT 64563
      (21.6 %), FF 87041 (14.6 %), BRAM 238.5 tiles (25.0 %). Output `build/gemm_soc_f32_pe8x8_t128x128-4e52e7f5/`.
- [x] Full build of `default.json`: timing met (build 4 above); the reports go to `reports/summary.json` (`VivadoReport`
      parses LiteX's text reports, no separate `vivado.json`)

### Phase 5: end to end ✅
- [x] `gemm_runner` written and compiling (2026-11-23 14:14, untested on hardware): `sw/gemm_host/src/gemm_kernel.{hpp,cpp}`
      (`GemmKernel`: args, start with CDC-safe status wait, cycles, measured accel Hz, all `gemm_perf_*` counters via an
      X-list from the build's csr.h; compiled in only if the build has the kernel), `tools/gemm_runner.cpp` (seeded
      float32 A/B, `--map a=chX,...`, A at its channel's 0, B 0 or 512 MB, C 0 or 1 GB if shared; verify full/sampled
      (4096 random entries + corners) with tolerance 4·K·eps·Σ|a·b|; text + `--json` line per rep incl. perf counters;
      `--expect-build-id`).
- [x] **First GEMM on the FPGA (2026-11-23 17:05): 256×256×256 float32, full verification OK** (65536 entries, 0 bad,
      max rel err 2.09e-07): 293718 cycles @ 200.000 MHz = 1.469 ms, **22.85 GFLOP/s = 89 % of the 25.6 GFLOP/s peak**.
      `scripts/gemm_run.py build/gemm_soc_f32_pe8x8_t128x128-4e52e7f5 --program --sizes 256x256x256 --verify full`:
      the whole bring-up (program, rescan, driver, DDR calibration after the driver, host tools) ran by itself, 29 s.
      Only the usual JTAG-reload AER lines; no UnxCmplt at the probe this time.
- [x] **Size sweep (17:05), all verified** (full verification up to ~1 M entries, sampled 4098 for 2048³), 2 reps each,
      repeatable to a few dozen cycles; `results/f32_pe8x8_t128x128-4e52e7f5/runs.jsonl`:

      | N×K×M | kernel ms | GFLOP/s | % of 25.6 peak | note |
      |---|---|---|---|---|
      | 256³ | 1.469 | 22.85 | 89 | |
      | 512³ | 11.56 | 23.22 | 91 | |
      | 1024³ | 91.95 | 23.35 | 91 | |
      | 2048³ | 733.5 | 23.42 | 91.5 | |
      | 1000×1008×1040 | 101.8 | 20.59 | 80 | partial tiles |
      | 300×512×128 | 2.17 | 18.12 | 71 | partial tiles |
      | 257×256×272 | 3.30 | 10.85 | 42 | 257 rows = 3 row tiles of 128 |

      Max relative error 1.3–2.9e-07 (tolerance 4·K·eps·Σ|ab|). End-to-end for 2048³: 796 ms (kernel 733 ms + DMA at
      0.81/0.86 GB/s). Only memory map a=ch0,b=ch1,c=ch1 tested so far (the map is a build-time knob).
- [x] **Counters match analytic traffic** (1024³): A read 32 MiB = N·K·4·(M/128) ✓, B read 32 MiB = K·M·4·(N/128) ✓,
      C written 4 MiB ✓. Observations: A is read in **single-beat bursts** (524288 AR, the transposing `ReadA` reads 64 B
      strided pieces), latency min/avg/max 33/85/164 cycles, the kernel waits on A 27.5 % of cycles; B in 8-beat
      bursts, avg latency 34 cycles; C: 120 k write-stall cycles. At PE 8×8 the kernel is compute-bound (91 % of peak);
      the A read path becomes relevant for larger PE arrays (`transposed_a` or a wider transpose width are DSE knobs).

### Phase 6: build driver ✅
- [x] Pieces for the run flow (2026-11-23 16:28): `tcl/vivado/program.tcl` (JTAG load, checks DONE),
      `flow/gemmflow/board.py` (`Board.bring_up()`: program → rescan → driver build/load → `ddr_init.sh` (after the
      driver) → host tools; logs in `<build>/logs/`), `scripts/gemm_run.py` (bring-up + `gemm_runner` per size with the
      build's memory map and build id → `results/<build_id>/runs.jsonl` with metadata). `gemm_soc.py --gemm-config`
      now writes `<build>/config.resolved.json`.
- [x] `scripts/gemm_build.py` (2026-11-23 16:40): stages `hls,soc,host`, `--force`, `--dry-run`; output
      `build/gemm/soc/<build_id>/` (config.json, manifest.json, reports/summary.json, logs/, stamps/). `flow/gemmflow/
      steps.py`: `Step` base (inputs hash → stamp, skip if up to date) → `HlsStep` (shares `build/gemm/hls/<hash>`),
      `SocStep` (runs `gemm_soc.py` with the resolved Vivado directives/threads), `HostStep` (liblitepcie + host tools).
      `reports.py` (`VivadoReport`: WNS/TNS/WHS/THS, LUT/FF/BRAM/DSP, kernel hierarchy) parses LiteX's text reports
      instead of a separate `reports_json.tcl` (simpler, same data); `manifest.py`. Dry run OK (HLS reused).
      Not yet run end to end.
- [x] Stamps: a rerun with no change skips everything (the shared 200 MHz build in `vivado_preset_sweep`: all steps
      up to date, 0 s); a Vivado-only change reuses the HLS IP (`explore`/`aggressive` points: `hls: up to date`)
- [x] `manifest.json`, `reports/summary.json` (22:35): only `gemm_build.py` wrote them, so none of the 17 DSE builds
      had them although the README promised them. Now `steps.write_build_reports()` is shared by `gemm_build.py` and
      `gemm_dse.py`: `reports/summary.json` always, `manifest.json` only when a step actually ran (an all-skipped rerun
      keeps the build-time manifest). `gemm_build.py --report-only` writes the summary from existing outputs without
      running anything; it backfilled the 16 earlier builds (no `manifest.json` for those: their build-time git state
      is in the campaigns' `builds.jsonl`). `up_to_date: false` for `host` in 9 of them only means the stamp is older
      than `sw/gemm_host`: `Board.bring_up()` rebuilds the host tools with make before any run.

### Phase 7: DSE ✅
- [x] `scripts/gemm_dse.py` + `flow/gemmflow/results.py` (`ResultStore`: JSONL source of truth + regenerated CSV,
      flattened keys, fixed leading columns, `schema_version` 1) (2026-11-23 17:15): grid expansion (dotted keys,
      cross product), validation (invalid points recorded with reason), stages hls (parallel, jobs = min(cores/4,
      free GB/3)) / build (sequential) / run (bring-up + sizes), `--resume`, `--dry-run`. Point names from the
      parameters (`BuildCfg.auto_name()`: `f32_pe16x8_t256x256_200mhz[_tA][_preset]`), so identical points share builds.
- [x] Campaigns in `configs/dse/`: `hls_pe_tile` (HLS only, PN×tile_n×tile_m = 12 points), `pe_sweep` (PN 8/16/32,
      tile 256, 4 sizes), `clock_sweep` (150/200/250 MHz), `vivado_preset_sweep` (default/explore/aggressive),
      `size_sweep` (one build, 5×3×3 sizes). All dry-run OK.
- [x] **Campaign `pe_sweep` complete** (2026-11-23 17:11–18:05, unattended; `results/pe_sweep/{dse.log,builds.*,runs.*}`).
      All three builds met timing, all 24 runs verified (sampled), every bring-up automatic (JTAG, rescan, driver,
      DDR calibration, host tools), no host problems. Tiles 256×256, 200 MHz, default memory map:

      | PE | Vivado build | WNS / WHS ns | LUT | FF | BRAM tiles | DSP |
      |---|---|---|---|---|---|---|
      | 8×8 | 710 s | +0.030 / +0.013 | 64.7k (21.7 %) | 87.1k | 239 | 383 |
      | 16×8 | 932 s | +0.024 / +0.016 | 94.6k (31.7 %) | 133.3k | 303 | 703 |
      | 32×8 | 1471 s | +0.035 / +0.013 | 154.2k (51.6 %) | 225.4k | 414.5 (43 %) | 1343 (70 %) |

      (HLS overestimated LUTs: 216k for PE 32×8 vs 154k placed.)

      | PE (peak) | 512³ | 1024³ | 2048³ | 1000×1008×1040 | A r_wait at 2048³ |
      |---|---|---|---|---|---|
      | 8×8 (25.6) | 24.28 (94.8 %) | 24.48 (95.6 %) | 24.56 (95.9 %) | 19.42 | 27.8 % |
      | 16×8 (51.2) | 46.17 (90.2 %) | 46.90 (91.6 %) | 47.19 (92.2 %) | 37.22 | 53.5 % |
      | 32×8 (102.4) | 84.10 (82.1 %) | **86.54 (84.5 %)** | 82.30 (80.4 %) | 68.67 | **93.4 %** |

      GFLOP/s (% of peak), kernel time only. 2048³ end-to-end incl. DMA: 763 / 427 / 272 ms. Max relative error
      ≤ 2.6e-07. Larger tiles help PE 8×8 (95.9 % vs 91.5 % at 128×128).
      **Bottleneck: the A read path.** The transposing `ReadA` reads A in single 64-byte strided beats; the share of
      cycles the kernel waits for A data grows with the PE count (28 → 54 → 93 % at 2048³), and A's average read
      latency grows with K (62 → 85 → 130 cycles for K = 512 → 1024 → 2048, more DRAM page misses for the K-strided
      accesses). B (8-beat bursts) waits 4.5–15 %, C writes stall ≤ 3 %.
      **Next knob:** `kernel.transposed_a = true` (host sends Aᵀ, the kernel reads it with bursts, no on-chip
      transpose). Done (~18:05): g++ simulation of the transposed path with PE 32×8 passes (256³, 512×256×512,
      272×512×256); `gemm_runner --transposed-a` sends Aᵀ (K × N; reference still uses A; N must be a multiple of
      16), passed automatically by `gemm_run.py` / `gemm_dse.py` from the config.
- [x] **Campaign `transposed_a` complete** (18:08–18:50, `results/transposed_a/`): both builds met timing (PE 16×8
      tA: 943 s, WNS +0.033, LUT 93.8k, DSP 701; PE 32×8 tA: 1485 s, WNS +0.038, LUT 153.5k, BRAM 414, DSP 1341),
      all 16 runs verified.

      | PE, transposed A (peak) | 512³ | 1024³ | 2048³ | 1008×1008×1040 | A / B r_wait at 2048³ |
      |---|---|---|---|---|---|
      | 16×8 (51.2) | 46.21 (90.3 %) | 46.91 (91.6 %) | 47.19 (92.2 %) | 37.52 | 8.6 % / 8.6 % |
      | 32×8 (102.4) | 84.25 (82.3 %) | 86.57 (84.5 %) | **87.54 (85.5 %)** | 69.25 | 15.9 % / 15.9 % |

      Effect: A is now read in 16-beat bursts (beats/bursts = 16 instead of 1), avg read latency 32–39 cycles (was up
      to 130), and the kernel waits on A no more than on B. PE 32×8 at 2048³ goes from 82.30 to **87.54 GFLOP/s**;
      the other points are unchanged (they were compute-bound already). Traffic is far below the DDR bandwidth
      (~0.7 GB/s per matrix at 87 GFLOP/s), so the remaining ~15 % at PE 32×8 is most likely per-tile fill/drain
      (A and B wait equally); next experiment: larger memory tiles (512) for PE 32×8 tA.
- [x] **Campaign `tiles_pe32_ta` complete** (PE 32×8, transposed A, tiles N×M ∈ {256, 512}², sizes
      1024³/2048³/4096³; 18:51–20:11, `results/tiles_pe32_ta/`). HLS 18:51–18:53 (all 4 OK, est 4.14–4.21 ns).
      The 256×256 build was reused from `transposed_a`; the other three built 18:53–20:09 (1471/1555/1486 s), all met
      timing; all 24 runs verified (sampled, max rel err ≤ 2.6e-07). Times in this file are from the result records
      (earlier entries had estimated times, corrected 19:02).

      | Tile N×M | WNS / WHS ns | LUT | BRAM tiles | DSP | 1024³ | 2048³ | 4096³ | A / B r_wait at 4096³ |
      |---|---|---|---|---|---|---|---|---|
      | 256×256 | +0.038 / +0.012 | 153.5k | 414.0 | 1341 | 86.57 (84.5 %) | 87.54 (85.5 %) | 88.34 (86.3 %) | 16.6 / 16.6 % |
      | 256×512 | +0.038 / +0.014 | 153.8k | 414.5 | 1335 | 87.53 (85.5 %) | 88.74 (86.7 %) | 89.40 (87.3 %) | 8.1 / 10.0 % |
      | 512×256 | **+0.007** / +0.016 | 153.2k | 430.5 | 1328 | 92.33 (90.2 %) | 93.45 (91.3 %) | 94.37 (92.2 %) | 10.5 / 8.6 % |
      | 512×512 | +0.031 / +0.016 | 153.9k | 430.5 | 1320 | 92.92 (90.8 %) | 94.31 (92.1 %) | **95.07 (92.8 %)** | 5.3 / 5.3 % |

      GFLOP/s (% of the 102.4 peak), kernel time only. 4096³ end to end incl. DMA: 1806 → 1695 ms.
      **Findings:** larger tiles cost almost nothing (+16.5 BRAM tiles for tile N 512, LUT/DSP unchanged) and halve
      the time the kernel waits for A and B. The N tile matters more than the M tile (512×256 beats 256×512 by ~5
      GFLOP/s): a larger N tile means fewer passes over B. **Best: 95.07 GFLOP/s** (512×512, 4096³). The 512×256
      build closed with only +0.007 ns, so this region is near the edge at 200 MHz.
- [x] **HLS-only campaign `pe32_ta_next_hls`** (20:11–20:15, 8 points: PE 32×8 tA, tiles {512, 1024}², 200/225 MHz):
      | Tile N×M | est ns (200 and 225 MHz targets) | BRAM18 (HLS) |
      |---|---|---|
      | 512×512 | 4.209 | 694 |
      | 512×1024 | 4.374 | 1142 |
      | 1024×512 | 4.209 | 1142 |
      | 1024×1024 | 4.374 | 2070 (> 1910 on the device: does not fit) |
      **225 MHz is not worth a build** (⚠️ wrong, see the correction below): HLS gives the same schedule as for 200 MHz, and the routed 200 MHz PE 32×8
      builds already have accel paths of 4.3 ns + skew (512×256: `ProcessingElement_17 trunc_ln_reg` → the dataflow
      `ap_sync_reg_ProcessingElement_*_ap_ready` resets, 8 logic levels, slack +0.007 ns); PE 16×8: `cPipes` FIFO
      4.94 ns. Beating 200 MHz would need kernel changes (e.g. registering the dataflow control fan-out).
      (In the 512×512 build the overall WNS +0.031 ns is in the PCIe user clock domain, not the kernel.)
- [x] **Campaign `tiles_pe32_ta_1024` complete** (PE 32×8 tA, tiles 512×512 (reused build) and 1024×512, sizes
      2048³/4096³/8192³; 20:15–20:44, `results/tiles_pe32_ta_1024/`). 1024×512 built in 1552 s, WNS +0.050 /
      WHS +0.012 ns, LUT 154.3k, BRAM 654.5 tiles (69 %, as estimated), DSP 1320. All 12 runs verified.

      | Tile N×M | 2048³ | 4096³ | 8192³ | A / B r_wait | 8192³ kernel / end to end |
      |---|---|---|---|---|---|
      | 512×512 | 94.31 (92.1 %) | 95.07 (92.8 %) | 95.45 (93.2 %) | 5.3 / 5.3 % | 11.52 s / 12.52 s |
      | 1024×512 | 97.37 (95.1 %) | 98.18 (95.9 %) | **98.59 (96.3 %)** | 3.8 / 2.8 % | 11.15 s / 12.15 s |

      **Best: 98.59 GFLOP/s** (1024×512, 8192³). The tile is now limited by BRAM (1024×1024 does not fit), and the
      kernel is within 4 % of its peak. At 8192³ the DMA (768 MB in, 256 MB out at 0.82–0.84 GB/s) is ~1 s of the
      12.15 s end to end.
- [x] **Campaign `memory_maps` complete** (20:47–21:13, `results/memory_maps/`): PE 8×8 t128 with maps A/B/C =
      CH0/CH0/CH0 and CH1/CH0/CH0 (every matrix on the channel it had not used, plus the shared-channel offsets B at
      512 MB and C at 1 GB). Both builds met timing (713 s, WNS +0.046; 730 s, WNS +0.033). **All 16 runs pass full
      verification** (every element checked, max rel err ≤ 2.9e-07). Build names end in `_m<a><b><c>` for non-default
      maps; default-map names are unchanged.

      | Map A/B/C | 512³ | 1024³ | 1000×1008×1040 | 2048³ | B avg read latency at 2048³ |
      |---|---|---|---|---|---|
      | CH0/CH1/CH1 (default, earlier build) | — | — | — | 23.42 | — |
      | CH0/CH0/CH0 | 23.17 | 23.27 | 20.53 | 23.35 | 46 cycles |
      | CH1/CH0/CH0 | 23.21 | 23.35 | 20.59 | 23.42 | 39 cycles |

      At PE 8×8 the kernel moves well under 1 GB/s, so sharing one channel costs only ~0.3 % (B waits 18 % instead
      of 15 %, read latency 46 instead of 39 cycles). The map matters more for PE 32×8 (not measured).
- [x] **Campaign `size_sweep`** (21:13–21:14, `results/size_sweep/`), on the best build (PE 32×8 tA, tile 1024×512):
      N ∈ {256…4096}, K, M ∈ {256, 1024, 4096}, 45 sizes × 2 reps + warmup, all 90 rows verified. Kernel GFLOP/s
      [end to end incl. DMA]:

      | N \ (K, M) | K 256, M 256 | K 256, M 4096 | K 1024, M 1024 | K 4096, M 1024 | K 4096, M 4096 |
      |---|---|---|---|---|---|
      | 256 | 11.0 [4.7] | 22.0 [15.0] | 24.0 [17.0] | 24.6 [18.6] | 24.6 [19.4] |
      | 512 | 22.0 [8.9] | 43.9 [27.8] | 48.0 [32.1] | 49.1 [35.2] | 49.1 [38.2] |
      | 1024 | 43.9 [16.1] | 87.1 [48.0] | 95.9 [57.6] | 98.2 [64.5] | 98.2 [73.7] |
      | 4096 | 44.1 [23.2] | 87.1 [53.8] | 95.9 [66.6] | 98.2 [73.7] | 98.2 [85.1] |

      **The kernel computes whole memory tiles:** N below the 1024 tile gives N/1024 of the speed, M below the 512 tile
      M/512 (N = 256, M = 256: 11 GFLOP/s, an eighth of 88). K sets the per-tile fill/drain share (K = 256: 87.5,
      K = 4096: 98.2 GFLOP/s). So the best tile depends on the workload: for matrices ≤ 512 the 256×256 builds are
      faster. End to end, the DMA (H2C 0.82 GB/s) dominates below ~1024³.
- [x] **Campaign `clock_sweep` complete** (21:14–21:54, `results/clock_sweep/`): PE 8×8 t128, accel clock 150/200/250
      MHz, 1024³, 2 reps, sampled verification (all 4 runs OK). Measured accel clock 149.9998 / 249.9996 MHz.

      | Accel | HLS FF est. | Build | WNS overall | WNS accel | WHS | LUT / FF | 1024³ GFLOP/s (% of peak) | Kernel cycles |
      |---|---|---|---|---|---|---|---|---|
      | 150 MHz | 68.7k | 728 s | +0.048 | +0.076 | +0.012 | 63.1k / 79.6k | 17.68 (92.1 % of 19.2) | 18.22 M |
      | 200 MHz | 78.1k | 724 s | **−0.009** (PCIe, see below) | +0.053 | +0.016 | 64.6k / 87.1k | not run (`timing_failed`) | — |
      | 250 MHz | 85.3k | 834 s | +0.045 | +0.049 | +0.016 | 67.1k / 92.4k | **29.02 (90.7 % of 32.0)** | 18.50 M |

      **Findings:**
      - **PE 8×8 closes timing at 250 MHz** although HLS estimates 4.236 ns (> 4 ns). With the tighter target HLS
        pipelines deeper (+7.2k FF estimated, +5.4k after Vivado) and Vivado meets 4 ns. The HLS estimate is not a limit.
      - Throughput follows the clock: 17.68 → 23.36 (200 MHz, preset builds below) → 29.02 GFLOP/s. Kernel cycles grow
        only 1.5 % from 150 to 250 MHz: DDR runs on the fixed 125 MHz sys clock, so the kernel waits a little more per
        cycle, but PE 8×8 is still compute-bound.
      - **The one failure is not in the kernel.** The failing path is in the PCIe hard block: the 500 MHz core clock
        `main_s7pciephyrxbe_clkout2` (Gen2 x8), from `PCIE_2_1` USERCLK to its own RX buffer (RAMB36, placed by the
        Xilinx IP's LOC at RAMB36_X10Y44). It has 0 logic levels and a 1.375 ns data path in a 2 ns period. This path
        has −0.009 … +0.076 ns of slack across all 16 GEMM builds, so whether a build passes on it comes down to tens
        of picoseconds of routing, whatever the kernel is (open issue below).
- [x] **Campaign `vivado_preset_sweep` complete** (21:54–22:22, `results/vivado_preset_sweep/`): PE 8×8 t128 at 200
      MHz, Vivado presets (`config.py` `VIVADO_PRESETS`); the default-preset build is the one shared with `clock_sweep`.

      | Preset | Build | WNS overall | WNS accel | WNS sys | LUT | 1024³ GFLOP/s |
      |---|---|---|---|---|---|---|
      | default | 724 s | −0.009 (PCIe 500 MHz path) | +0.053 | +0.165 | 64.6k | not run (`timing_failed`) |
      | explore | 789 s | +0.017 | +0.049 | +0.051 | 64.6k | 23.36 (91.2 %) |
      | aggressive | 793 s | +0.076 | +0.230 | +0.111 | 64.9k | 23.36 (91.2 %) |

      Presets change only timing closure: same size, identical cycles. `aggressive` had the most margin on every
      clock for +9 % build time. One build per preset, and the spread between near-identical builds (PCIe path
      −0.009 … +0.050) is as large as the gaps between presets, so this is a hint, not a ranking.
- [x] **Correction (22:25) to `pe32_ta_next_hls` above:** "HLS gives the same schedule for 225 MHz" was wrong. The
      estimate is the same (4.209 ns), but HLS adds ~18.6k FF of pipelining at 225 MHz (258.5k → 277.1k for every tile).
      That is the same kind of change that let PE 8×8 close at 250 MHz, so a 225 MHz build is worth trying.
- [x] **Campaign `clock_pe32_ta` complete** (22:26–23:30, `results/clock_pe32_ta/`): best config (PE 32×8 tA, tile
      1024×512) at 200 (reused build) / 225 / 250 MHz. It ran on the code from before the run-row fix, so its run rows
      carry the 8-hex `build_id`.

      | Accel | HLS FF est. | Build | WNS accel (failing endpoints, TNS) | WNS PCIe 500 | LUT / FF | GFLOP/s 2048³ / 4096³ / 8192³ |
      |---|---|---|---|---|---|---|
      | 200 MHz | 258.5k | reused | +0.080 | +0.050 | 154.3k / 225.4k | 97.37 / 98.18 / **98.59** (same as at 20:44, to 4 digits) |
      | 225 MHz | 277.1k | 1911 s | **−0.041** (34, −0.83 ns) | +0.057 | 151.5k / 236.0k | not run (`timing_failed`) |
      | 250 MHz | 283.5k | 1791 s | −0.775 (8455, −1308 ns) | +0.075 | 151.1k / 242.2k | not run |

      **225 MHz misses by only 41 ps, and on routing.** The failing paths are (a) the C-pipe FIFOs between neighbouring
      PEs (`cPipes_14/16/27`, 256-bit, depth-2 SRL): the write enable / address fans out to 256 shift-register bits,
      1 LUT and 4.3–4.5 ns of route (94 % routing); (b) the dataflow control (`ap_sync_reg_*_ap_ready` reset from a PE
      loop counter, 7 logic levels). 250 MHz is far off (−0.775 ns, the same dataflow-control paths): that would need
      kernel changes such as registering the dataflow control fan-out.
      End to end 8192³ is now 12.00 s (was 12.15 s before the C2H fix).
- [x] **Campaign `pe32_ta_225_presets` complete** (2026-11-23 23:31 – 2026-11-24 00:41, `results/pe32_ta_225_presets/`):
      PE 32×8 tA tile 1024×512 at **225 MHz** with the `aggressive` and `explore` presets (HLS reused). **Both met
      timing**, and all 12 runs verified (sampled, 4098 elements each, max rel err ≤ 2.0e-07), at a measured
      224.9996 MHz.

      | Preset | Build | WNS accel | WNS PCIe 500 | WNS sys | LUT / FF | GFLOP/s 2048³ / 4096³ / 8192³ |
      |---|---|---|---|---|---|---|
      | default (`clock_pe32_ta`) | 1911 s | −0.041 (34 endpoints) | +0.057 | +0.169 | 151.5k / 236.0k | not run |
      | aggressive | 2017 s | **+0.049** | +0.046 | +0.145 | 152.0k / 235.8k | 109.33 / 110.34 / **110.86** |
      | explore | 2063 s | +0.007 | +0.031 | +0.108 | 151.3k / 235.9k | the same to 4 digits |

      **New best: 110.86 GFLOP/s at 8192³ (96.2 % of 115.2)**, +12.4 % over 200 MHz. Kernel cycles are unchanged
      (2.2316 G vs 2.2304 G at 200 MHz), so the kernel is still compute-bound at 225 MHz. End to end 8192³: 10.77 s
      (kernel 9.92 s). DSP 1320 and BRAM 654.5 tiles, as at 200 MHz. Config: `configs/gemm/pe32x8_ta_t1024x512_225mhz.json`
      (`aggressive`, build `f32_pe32x8_t1024x512_225mhz_tA_aggressive-db5adf1c`, reused by `gemm_build.py`).
      **What made it close:** the preset's phys_opt (driver replication on the 256-bit C-pipe FIFO enables and the
      dataflow control). The kernel is unchanged. This campaign was the first to run on the fixed `gemm_dse.py`: run
      rows carry `build_id` = full id and `hw_build_id` = `db5adf1c`/`8c43d008`, plus `invocations.jsonl`, and the build
      dirs got `manifest.json` + `reports/summary.json`.
- [x] Phase 8 (docs), part 1 (20:16): README GEMM section has the measured-results table (best config later `pe32x8_ta_t1024x512.json`), the transposed-A knob and
      the DDR placement; `configs/gemm/pe32x8_ta_t512.json` is the best measured config (same build id as the
      campaign build, so `gemm_build.py` on it reuses `build/gemm/soc/f32_pe32x8_t512x512_200mhz_tA-dc42aa19`).
- [x] `gemm_runner` capacity check (was open): each matrix (C plus its C2H read slack) must end before the next
      region on its channel; e.g. B of 1.25 GB is rejected (`does not fit before 0x40000000`). Regression on PE 32×8:
      1024³ unchanged (86.53 GFLOP/s, full verification OK).
- [x] **Campaign `hls_pe_tile` run (17:08–17:10, 4 HLS jobs in parallel, 2.7 min for 12 points)**,
      `results/hls_pe_tile/builds.{jsonl,csv}`. All OK points estimate 4.20–4.24 ns at the 5 ns target. Resources:

      | PE | tiles (N×M) | DSP | BRAM18 | LUT |
      |---|---|---|---|---|
      | 8×8 | 128×128 / 128×256 / 256×256 | 381 / 373 / 361 | 313 | 67–68k |
      | 16×8 | 128×128 … 256×256 | 681–701 | 425–441 | 117–118k |
      | 32×8 | 128×128 … 256×256 | 1321–1341 | 665 | 210–216k |

      SoC without kernel ≈ 25k LUT, so PE 32×8 would be ~80 % of the 298.6k LUTs (routing risk); PE 16×8 ~48 %.
      One point failed in HLS: `f32_pe8x8_t256x128` hits `static_assert(kInnerTilesM >= kInnerTilesN)` in Compute.cpp
      (tile_m/PM ≥ tile_n/PN for double buffering), which `config.py` did not check: **rule added**; the point is now
      rejected as invalid before any tool runs.
- [x] `--resume` after an interruption. Tested (22:29) on a copy of the finished `vivado_preset_sweep`: no Vivado,
      no card access, the timing-failed point is re-checked from its stamps and skipped. **Two bugs fixed on the way:**
      (1) run rows took `build_id` from `gemm_runner`'s JSON, i.e. the 8-hex id read from the bitstream, so
      `--resume` never matched a recorded size and would have rerun everything; rows now carry the full build id
      plus `hw_build_id`, and resume matches on `config_hash` (also correct for older rows, which keep the 8-hex
      `build_id`). (2) Resume counted failed rows as done; now only `ok` rows. Also: a point with nothing left skips
      its bring-up (it used to reprogram the card first), `--dry-run` writes nothing, and every invocation is
      appended to `results/<campaign>/invocations.jsonl` (`campaign.json` only holds the latest one).
      **Partial resume on the card (23:32):** a copy of `clock_pe32_ta` with the 4096³ rows deleted, `--resume --stages
      run`: only the 200 MHz build was brought up, only 4096³ ran (98.18 GFLOP/s ×2, verified, during the Vivado build
      of the next campaign). 2048³/8192³ and the timing-failed points were skipped, and the new rows have the full
      `build_id` plus `hw_build_id` = `a88777ec`.

### Phase 8: docs 🟡
- [ ] README usage section
- [x] Final numbers and lessons: section "Summary and lessons" at the top (22:45)
- [x] Unit tests for the Python flow (2026-11-24 00:55): `flow/tests/test_gemmflow.py`, stdlib `unittest`, 20 tests in
      < 0.1 s, no tools or card. Covered: golden build ids of every `configs/gemm/*.json` (each matches a build dir on
      disk; a hashing or default change would silently stop all reuse), Vivado knobs leave the HLS hash alone, auto
      names, every rejection rule with its message, size multiples, presets and overrides, Config.h rendering,
      ResultStore CSV (union of keys, leading columns), campaign expansion (names, a build shared across campaigns,
      every campaign in `configs/dse/` expands), build reports (manifest only when a step ran). Gateware simulations
      `hw/gemm/tests/test_axi_native.py` and `test_ddr_dma.py` re-run: PASS.

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
| 2026-11-23 | GemmSoC (no CPU, DDR bridge) | Vivado timing / size | WNS +0.080 ns; 25.5k LUT, 19.5k FF, 73.5 BRAM |
| 2026-11-23 | GemmSoC, no CPU | DDR3 calibration + 1 GB BIST, both channels | PASS, 0 bad words, 7.11 GB/s write / 7.20 GB/s read |
| 2026-11-23 | HLS `default.json` (f32 PE 8×8, tile 128) | csynth estimate | 4.236 ns (target 5 ns), DSP 381, BRAM18 313, FF 78k, LUT 68k, 33 s |
| 2026-11-23 | GemmSoC, DDR calibrated after driver load | **DMA ↔ DDR round trip** | **OK** CH0+CH1, 64 MB: H2C 0.82 GB/s, C2H 0.84 GB/s (2 MB chunks) |
| 2026-11-23 | GemmSoC + kernel (f32 PE 8×8, 200 MHz) | Vivado timing / size | WNS +0.049 ns; 64.6k LUT, 87k FF, 238.5 BRAM tiles, 383 DSP |
| 2026-11-23 | GemmSoC + kernel (f32 PE 8×8, 200 MHz) | **GEMM 256³** | **verified, 1.469 ms, 22.85 GFLOP/s (89 % of peak)** |
| 2026-11-23 | GemmSoC + kernel (f32 PE 8×8, 200 MHz) | **GEMM 2048³** | **verified (sampled), 733.5 ms, 23.42 GFLOP/s (91.5 %)** |
| 2026-11-23 | pe_sweep, tile 256, 200 MHz | **best GEMM** | PE 8×8 24.56, PE 16×8 47.19, **PE 32×8 86.54 GFLOP/s** (1024³, 84.5 % of 102.4) |
| 2026-11-23 | transposed_a, PE 32×8, tile 256 | **best GEMM** | **87.54 GFLOP/s** (2048³, 85.5 % of 102.4), A in 16-beat bursts |
| 2026-11-23 | tiles_pe32_ta, PE 32×8 tA, tile 512×512 | **best GEMM** | **95.07 GFLOP/s** (4096³, 92.8 % of 102.4); 94.31 at 2048³ |
| 2026-11-23 | tiles_pe32_ta_1024, PE 32×8 tA, tile 1024×512 | **best GEMM** | **98.59 GFLOP/s** (8192³, 96.3 % of 102.4); 97.37 at 2048³ |
| 2026-11-23 | memory_maps, PE 8×8 t128 | maps CH0/CH0/CH0, CH1/CH0/CH0 | all verified (full); 23.35 / 23.42 GFLOP/s at 2048³ |
| 2026-11-23 | DDR DMA after C2H fix | 64 MB, both channels | H2C 0.82 GB/s, **C2H 1.49 GB/s** (was 0.84) |
| 2026-11-23 | clock_sweep, PE 8×8 t128 | 150 / 200 / 250 MHz | 17.68 / timing failed by 9 ps (PCIe path) / **29.02 GFLOP/s** at 1024³; 250 MHz met (WNS +0.045) |
| 2026-11-23 | vivado_preset_sweep, PE 8×8 t128, 200 MHz | default / explore / aggressive | WNS −0.009 / +0.017 / +0.076 ns, build 724 / 789 / 793 s, 23.36 GFLOP/s for both that met timing |
| 2026-11-23 | clock_pe32_ta, PE 32×8 tA t1024×512 | 200 / 225 / 250 MHz | 98.59 GFLOP/s (8192³, reproduced) / WNS −0.041 (34 endpoints, C-pipe fan-out) / −0.775 ns |
| 2026-11-24 | pe32_ta_225_presets, PE 32×8 tA t1024×512, 225 MHz | aggressive / explore | both met (WNS +0.049 / +0.007); **110.86 GFLOP/s** (8192³, 96.2 % of 115.2) |

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
- **DMA throughput (in progress, 20:20):** DDR transfers run at ~0.8 GB/s against 1.68 GB/s raw PCIe loopback.
  Suspect: the generated LitePCIe driver's `litepcie_dma_{reader,writer}_stop()` waits `udelay(1000)`, and
  `DdrDma` stops the DMA after every 2 MB chunk (~1.25 ms of data), so ~1 ms of every ~2.5 ms is that wait.
  Steps: (1) per-phase timing in `DdrDma` / `ddr_dma_test` (copy/start/wait/stop per chunk; commit 93feb59) to
  confirm; (2) C2H: enable the PCIe DMA writer once per `read()` instead of per chunk (the writer only moves what
  the bridge sends, and its loop status counts buffers since the enable), written and parked until the running
  campaign has built its host tools; (3) H2C cannot stay enabled (the reader loops over the ring without flow
  control), so the options there are a shorter stop delay in the generated driver copy, or a larger ring.
  **Measured (20:47, 64 MB, both channels, per 2 MB chunk):** H2C copy 0.15, start 0.12, wait 1.29, **stop 1.003**
  ms; C2H start 0.12, wait 1.16, copy 0.20, **stop 1.007** ms. Confirmed: the driver's 1 ms wait is ~40 % of the
  time. Data moves at 1.63 GB/s (H2C) and 1.80 GB/s (C2H) while running.
  **C2H fixed (20:50):** writer enabled once per `read()`: C2H 0.84 → **1.49 GB/s** (per chunk start 0.005, stop
  0.031 ms), verified with random and counter patterns at 1/3/37/64 MB on both channels, and GEMM with full
  verification (1008×1008×1040, 2048³, 4096×512×3008).
- **H2C: decision for the user.** The remaining 1 ms per 2 MB chunk is `udelay(1000)` in
  `litepcie_dma_reader_stop()` of the generated driver (`build/<id>/driver/kernel/main.c`, from deps/litepcie).
  The reader keeps streaming the ring until it is stopped (the bridge drops the excess), so the wait lets in-flight
  read completions land before `enable=0` resets the reader. Shortening it (e.g. to 100 µs, still ~5–10× the expected
  completion latency) would give H2C ≈ 1.3 GB/s (saves ~16 ms at 2048³, ~64 ms at 4096³), but a completion arriving
  after the reset could stall the PCIe path, the failure class behind the earlier host freezes. Not done unattended.
  Safer variant: the bridge back-pressures once `length` is reached (instead of dropping), so the reader stops
  issuing requests before the stop; needs a gateware change + rebuild as well as the driver patch.
- **PCIe core path at 500 MHz (22:25, decision for the user).** With the PHY set to Gen2 x8, the PCIe hard block
  runs its core at 500 MHz. The path `PCIE_2_1` USERCLK → RX buffer RAMB36 (placed by the Xilinx IP) has
  0 logic levels and only −0.009 … +0.076 ns of slack across all 16 GEMM builds. One build so far (PE 8×8, 200 MHz,
  default preset) failed timing on it alone. `gemm_dse.py` treats that as failed and never loads such a bitstream,
  which is right: a PCIe-core timing error is the kind of fault behind the earlier host freezes. Options:
  (a) build the PHY as Gen2 **x4**, the width this PC's slot trains to anyway. The core clock drops to 250 MHz and
  this path stops mattering, but x8 bandwidth in an x8 slot is lost (C2H already runs at 1.49 GB/s of x4's
  ~1.7). That needs a `boards/dupcb/target.py` option, so it touches the `--pack` question too.
  (b) Keep x8 and rebuild with `explore`/`aggressive` when only this path fails (manual today; could be automated
  in `gemm_dse.py`).
  (c) Make `aggressive` the default preset (+9 % build time; the most margin in the one sample so far).
- **Found while testing (fixed):** sizes the kernel cannot handle gave wrong results without an error:
  4096×512×3000 failed verification (12.26 M of 12.29 M elements wrong) with the old and the new DMA code alike;
  M must be a multiple of the M bus word (16 floats), as gemm_hls `RunHardware.cpp` checks. `BuildCfg.size_multiples()`
  gives the (N, K, M) multiples of a build: K a multiple of the K bus word and, without transposed A, of the transpose
  width; M of the M bus word; N of the N bus word with transposed A. `gemm_run.py` / `gemm_dse.py` pass them as
  `gemm_runner --multiples N,K,M`, which rejects other sizes before any transfer.

## Log

### 2026-11-24 00:41 — 225 MHz closes: 110.9 GFLOP/s
- PE 32×8 tA tile 1024×512 meets timing at 225 MHz with the `aggressive` (+0.049 ns) and `explore` (+0.007 ns) presets
  and runs at 110.86 GFLOP/s at 8192³ (96.2 % of 115.2), +12.4 % over 200 MHz, all verified. New best config
  `configs/gemm/pe32x8_ta_t1024x512_225mhz.json`.

### 2026-11-23 23:32 — PE 32×8 at 225/250 MHz
- Best config at 225 MHz misses timing by 41 ps (34 endpoints, route-dominated C-pipe FIFO enables and dataflow
  control); 250 MHz by 0.78 ns. The 200 MHz rerun reproduced 98.59 GFLOP/s exactly. Trying the explore/aggressive
  presets at 225 MHz next.

### 2026-11-23 22:25 — clock and Vivado preset sweeps
- PE 8×8 meets timing at 250 MHz: 29.0 GFLOP/s, up from 23.4 at 200. The only timing failure was a 9 ps miss on a
  PCIe hard-block path at 500 MHz (Gen2 x8 core clock), not in the kernel; that path is marginal in every build. The
  presets change only closure (aggressive: most margin, +9 % build time). The earlier "225 MHz is not worth a build"
  for PE 32×8 was wrong (HLS does add pipelining). Campaign `clock_pe32_ta` started: best config at 200/225/250 MHz.

### 2026-11-23 21:13 — DMA read fix, size rules, memory maps
- C2H 0.84 → 1.49 GB/s (writer enabled once per read); `gemm_runner` rejects sizes the kernel cannot handle;
  the two untested memory maps verified on hardware. H2C stop-delay change left for the user to decide.

### 2026-11-23 20:44 — tile 1024×512
- PE 32×8 transposed A with 1024×512 tiles: 98.59 GFLOP/s at 8192³ (96.3 % of peak), 69 % BRAM, timing met.

### 2026-11-23 20:11 — larger tiles
- Tile campaign for PE 32×8 transposed A done: 512×512 tiles reach 95.07 GFLOP/s at 4096³ (92.8 % of peak), up
  from 88.34 with 256×256; extra cost 16.5 BRAM tiles. All builds met timing (512×256 only by +0.007 ns).

### 2026-11-23 18:50 — transposed A
- Transposed-A builds (PE 16×8, 32×8) benchmarked: PE 32×8 2048³ 82.3 → 87.5 GFLOP/s; A and B now wait equally.

### 2026-11-23 18:05 — PE sweep done
- Three kernels (PE 8×8, 16×8, 32×8, tile 256) built, loaded and benchmarked unattended by `gemm_dse.py`; all
  verified; best 86.5 GFLOP/s with PE 32×8. The A read path (single-beat transposing reads) is the bottleneck at
  larger PE counts; next: `transposed_a`.

### 2026-11-23 17:05 — first GEMM on the FPGA
- Kernel build needed two Vivado workarounds (Route 35-13 driverless LUTRAM nets in unused/constant AXI converter
  FIFOs): single-direction converters, own `AxiWriteToNative` for C. Build 4 meets timing at 200 MHz.
- 256³ float32 verified on the card, 22.85 GFLOP/s (89 % of peak), fully automatic bring-up.

### 2026-11-23 16:28 (resumed)
- Rebuild with the kernel (USER-port fix) running. The `synth_ip ... project mode` critical warning also appears for
  LiteX's own PCIe IP, which builds fine, so it is harmless.
- Added the board bring-up automation (`gemmflow/board.py`, `scripts/gemm_run.py`, `tcl/vivado/program.tcl`).

### 2026-11-23 14:15 (stopped: usage limit) — where to resume
- Done today after phase 1: phase 2 complete (DMA ↔ DDR qualified; the driver-probe SoC reset was the bug), phase 3
  HLS csynth + IP export working (est. 4.24 ns), phase 4 integrated (fast check OK), phase 5 runner written.
- **Resume here:**
  1. Rebuild with the kernel (user-port fix applied) and check `build/gemm_soc_kernel_build.log` (EXIT, WNS/WHS in `build/gemm_soc_f32_pe8x8_t128x128-4e52e7f5/gateware/
     dupcb_timing.rpt`, utilization). If the IP import or timing fails, fix there first.
  2. Board sequence (crash capture armed: `misc/crash_debug/arm_crash_capture.sh` after every boot; IOMMU on):
     `python3 hw/gemm/gemm_soc.py --gemm-config configs/gemm/default.json --load` → `scripts/pcie_rescan.sh` →
     `BUILD_DIR=build/gemm_soc_f32_pe8x8_t128x128-4e52e7f5 scripts/driver.sh build && ... driver.sh load` →
     `BUILD_DIR=... scripts/ddr_init.sh` (after the driver!) → `make -C sw/gemm_host BUILD_DIR=...` →
     `build/.../sw/gemm_runner --n 256 --k 256 --m 256 --verify full` (then larger sizes, non-multiples of the tile).
  3. Then: phase 6 (`scripts/gemm_build.py` with stages/stamps/manifest), phase 7 (DSE), DMA throughput optimisation
     (keep the DMA running instead of per-2 MB start/stop), `reports_json.tcl`, AXI perf monitor sim test.
- Open: `setup.litex.du-pcb.py --pack` decision (README, .gitignore, scripts/env.sh, pcie_rescan.sh, ddr_init.sh,
  boards/dupcb/target.py, ddr_init.py and the new s7pciephy_rx_be.py changed since the archive).

### 2026-11-23 14:08 (phase 2, DMA ↔ DDR works)
- Root cause of the failing round trips: the LitePCIe driver probe resets the SoC (`CSR_CTRL_RESET`) and wipes the DDR
  calibration done before it. `ddr_init.py` now calibrates through the driver; round trips clean on both channels.
- Meanwhile (no hardware): HLS of the default config works (est. 4.24 ns at 5 ns) after two kernel changes
  (`hls::stream` in synthesis, `ConvertWidthC` without the dynamic 512-bit index).

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
