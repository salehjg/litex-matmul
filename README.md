# LiteX on the DU-PCB-001-003

LiteX SoC for the DU-PCB-001-003 Kintex-7 480T card (`xc7k480tiffg1156-2L`), used as a PCIe accelerator.
The board itself is documented in `my-fpga-ws/boards/DU-PCB-001-003/README.md`; section numbers (§) below refer to it.

## What is in the SoC

| Block | Details |
|---|---|
| PCIe | Gen2 x8, BAR0 = CSRs, 1 DMA channel, LitePCIe Linux driver generated into `build/dupcb/driver/` |
| CPU | VexRiscv + BIOS. Console over PCIe (`scripts/console.sh`); no UART pins on the card |
| DDR3 CH0 | CPU main RAM at `0x4000_0000` (CPU sees 1 GB of the 2 GB). BIST for testing |
| DDR3 CH1 | Second controller (2 GB) for your accelerator logic. BIST for testing |
| DDR3 PHY | `WL7DDRPHY`: one MMCM per channel, CK phase moved by the host. Both channels calibrated by the host: `scripts/ddr_init.sh` |
| Clocks | 200 MHz #0 → `sys` 125 MHz → one DDR MMCM per channel. 200 MHz #1 → `accel` domain 200 MHz. 50 MHz → `clk50` domain. Freq meters on `clk50` and `accel` |
| LEDs | Red/green/yellow bracket LEDs run a chaser = SoC alive. Host can take over via `leds_out` |
| Keep-alive | AC24 driven high **and** all unused pins pulled up (§3). Never remove this |

## Layout

```
setup.litex.du-pcb.py  the whole project in one file: unfolds everything below, installs LiteX
boards/dupcb/
  dupcb_platform.py    pins and bitstream settings
  target.py            the SoC
  wl7ddrphy.py         DDR3 PHY with host-adjustable CK phase (from openTPU)
  ddr_init.py          host-side DDR3 calibration (CK phase scan) + tests, both channels
scripts/               daily scripts
config.sh              tool paths on this machine (written by the setup script, git-ignored)
deps/  .venv/          LiteX and Python env (git-ignored, recreated by the setup script)
build/                 all build output (git-ignored)
```

## Setup (once)

```
python3 setup.litex.du-pcb.py                     # in the current directory
python3 setup.litex.du-pcb.py --workdir ~/dupcb   # or anywhere else
python3 setup.litex.du-pcb.py --help              # all options
```
Needs Python 3.6+ to run (LiteX itself needs 3.8+: `--python /path/to/python3.x` picks the one for the venv),
git, bash, and Vivado (found on PATH or in /tools/Xilinx, /opt/Xilinx, ~/Xilinx; else `--xilinx PATH`).
Standard library only. It pins everything to LiteX `2026.08` (`LITEX_TAG` at the top of the script). The RISC-V
GCC that comes with Vitis builds the BIOS. Safe to run again; files you changed are kept unless `--force`.

**Changing the project:** edit the files as usual, then `python3 setup.litex.du-pcb.py --pack` copies them back
into the script, so the single file stays complete. A new file: add its path to `FILES` at the top of the
script, then `--pack`.

## Daily use

```
scripts/build.sh --no-compile-gateware   # fast check (~1 min): Verilog, XDC, BIOS, driver
scripts/build.sh                         # full bitstream (Vivado, ~30-60 min)
scripts/load.sh                          # load over JTAG (volatile)
scripts/pcie_rescan.sh                   # card in this PC: let Linux find it again
scripts/status.sh                        # ident, measured clocks, LEDs
scripts/ddr_init.sh                      # calibrate + test both DDR3 channels (after every load, and after
                                         # scripts/driver.sh load: the driver's probe resets the SoC)
scripts/console.sh                       # BIOS console (Ctrl-C to quit)
scripts/sim.sh                           # SoC + firmware in Verilator (needs verilator)
scripts/clean.sh
```
Extra target options pass through `build.sh`, e.g. `--sys-clk-freq 100e6`, `--no-ch1`, `--no-pcie`,
`--uart-name jtag_uart`. See `python3 boards/dupcb/target.py --help`.

Adding accelerator logic: in `target.py`, take a DDR CH1 port with
`self.sdram1.crossbar.get_port(clock_domain="accel")` and connect it to your core.

## Things to know

- **AC24 keep-alive (§3).** If DONE drops and the red SMD LED blinks ~250 ms after loading, something removed it.
- **Reloading in a running PC** drops the PCIe link. Run `scripts/pcie_rescan.sh`; if the card is still missing,
  warm-reboot (the FPGA keeps its design). A bench supply avoids this.
- **Flash boot is not supported yet:** §6.5 says there is no working way to write the flash. Load over JTAG.
- **DDR3 calibration.** The DDR banks are HR (no ODELAY, so no write leveling), and the chips sit on a fly-by
  bus, so the far chips see CK later. The stock `A7DDRPHY` can only move DQS, and on this card that leaves no
  write timing that works for every lane under traffic (CH0: lanes 4-7 fail; CH1: BIST ~21% bad even though
  every single-burst check passed). `wl7ddrphy.py` is openTPU's fix (`my-fpga-ws/submodules/openTPU`,
  `docs/litedram.md` sections 7-8, measured on the YPCB-00338): CK moves against DQ/DQS through the channel's
  MMCM, and `ddr_init.py` picks the CK phase with a BIST at every step. The BIOS still tries CH0 at boot
  without that scan and usually prints a memtest failure: ignore it, run `scripts/ddr_init.sh`.
- **ECC chips** (byte lane 8) are not used: 64-bit data per channel.
- **Verified on this card (2026-11-20):** keep-alive, PCIe (Gen2, x4 in this PC's slot), both 200 MHz clocks and
  the 50 MHz, both DDR3 channels (CH0 893 ps / CH1 1429 ps write window, 1 GB BIST clean, 7.2 GB/s).
  DDR3 VDDQ (1.35 or 1.5 V) is unmeasured; I/O is SSTL15 like the MIG design that calibrated on this card.
- **PCIe DMA (verified 2026-11-23):** `target.py` uses `S7PCIEPHYRXBE` (`boards/dupcb/s7pciephy_rx_be.py`), not
  LitePCIe's `S7PCIEPHY`. The stock PHY takes the 128-bit RX byte enables from `tkeep`; with LitePCIe 2026.08 that
  adds a garbage beat to every read completion and the DMA reader stalls after ~64 KB. With the fix:
  `scripts/driver.sh dma-test` loopback 13.4 Gb/s (Gen2 x4), 0 errors. Driver: `scripts/driver.sh build|load`;
  one-time `sudo scripts/setup_permissions.sh` for password-less use. Keep the IOMMU on (`scripts/iommu/`).

## GEMM HLS accelerator

An HLS GEMM kernel (float32, adapted from [spcl/gemm_hls](https://github.com/spcl/gemm_hls)) in a CPU-less variant of
this SoC (`hw/gemm/gemm_soc.py`): the host sends A and B over PCIe DMA into the DDR3 channels, the kernel computes
C = A·B at 200 MHz, the host reads C back together with performance counters (cycles, per-matrix AXI bursts, stalls,
read latency). JSON-driven build and design-space exploration. Plan:
[`docs/notes1/gemm-hls-plan.md`](docs/notes1/gemm-hls-plan.md), status and measurements:
[`docs/notes1/gemm-hls-progress.md`](docs/notes1/gemm-hls-progress.md).

Measured (kernel time, float32, all results verified against a host reference; 200 MHz unless noted):

| Config | Peak | Best measured | LUT / DSP / BRAM tiles |
|---|---|---|---|
| `default.json`: PE 8×8, tile 128 | 25.6 GFLOP/s | 23.4 (2048³, 91.5 %) | 64.6k / 383 / 238.5 |
| PE 8×8, tile 256 | 25.6 | 24.6 (2048³, 95.9 %) | 64.7k / 383 / 239 |
| PE 16×8, tile 256 | 51.2 | 47.2 (2048³, 92.2 %) | 94.6k / 703 / 303 |
| `pe32x8_ta_t512.json`: PE 32×8, transposed A, tile 512×512 | 102.4 | 95.5 (8192³, 93.2 %) | 153.9k / 1320 / 430.5 |
| `pe32x8_ta_t1024x512.json`: PE 32×8, transposed A, tile 1024×512 | 102.4 | 98.6 (8192³, 96.3 %) | 154.3k / 1320 / 654.5 |
| `pe32x8_ta_t1024x512_225mhz.json`: the same at **225 MHz**, Vivado preset `aggressive` | 115.2 | **110.9** (8192³, 96.2 %) | 152.0k / 1320 / 654.5 |
| `default.json` at 250 MHz (`clocks.accel_mhz`): PE 8×8, tile 128 | 32.0 | 29.0 (1024³, 90.7 %) | 67.1k / 383 / 231 |

`transposed_a: true` means the host sends Aᵀ (`gemm_runner --transposed-a`, passed automatically from the build's
config), so the kernel reads A in bursts instead of single strided words; this and larger tiles are what took PE 32×8
from 82 to 98.6 GFLOP/s, and 225 MHz (which only the `explore`/`aggressive` presets close) to 110.9.

```
git submodule update --init --recursive                     # submodules/gemm_hls (+ hlslib): reference only
sudo scripts/setup_permissions.sh                           # once: password-less board access
misc/crash_debug/arm_crash_capture.sh                       # once per boot (see docs: host freezes)

.venv/bin/python scripts/gemm_build.py configs/gemm/default.json          # HLS -> Vivado -> host tools (~15 min;
                                                                          #  PE 32x8 builds ~25 min)
.venv/bin/python scripts/gemm_run.py build/gemm/soc/<build_id> --program \
        --sizes 1024x1024x1024 2048x2048x2048 --reps 3 --verify sampled  # load + bring-up + runs
.venv/bin/python scripts/gemm_dse.py configs/dse/pe_sweep.json            # sweep: builds + runs -> results/<campaign>/
```

- **Order on the card:** load → `scripts/pcie_rescan.sh` → `scripts/driver.sh load` → `scripts/ddr_init.sh` →
  runs. The driver's probe resets the SoC, which clears the DDR calibration; `gemmflow.board.Board.bring_up()`
  (used by `gemm_run.py --program` and `gemm_dse.py`) does it in that order.
- **Config:** `configs/gemm/*.json` (PE counts, tiles, bus widths, FIFO depths, m_axi bursts, memory map, accel clock,
  Vivado preset/directives/threads). Checked against every gemm_hls rule before any tool runs (`flow/gemmflow/config.py`).
- **Timing:** a build that misses timing anywhere is recorded as `timing_failed` and never loaded. The PCIe Gen2 x8
  core (500 MHz, inside the hard block) has only tens of ps of margin in every build and failed alone once; if a build
  fails only there, rebuild with `vivado.preset: explore` or `aggressive`.
- **Sizes:** N, K, M must be whole bus words for the build (64-byte words: K and M multiples of 16 floats, N too with
  transposed A); `gemm_runner` rejects other sizes. The kernel computes whole memory tiles, so matrices smaller than
  the tile run at a fraction of the peak (size sweep in the progress file).
- **Host ↔ DDR transfers:** H2C 0.82 GB/s, C2H 1.49 GB/s (the driver's DMA stop costs 1 ms per 2 MB chunk on H2C).
- **DDR placement** (default `memory_map` a=ch0, b=ch1, c=ch1): A at CH0 0x0, B at CH1 0x0, C at CH1 0x4000_0000;
  limits A ≤ 2 GB, B ≤ 1 GB, C ≤ 1 GB, checked by `gemm_runner` (details in the progress file, "DDR memory map").
- **Report:** `docs/report/build.sh` builds `docs/report/report.pdf` (git-ignored; pdfLaTeX + latexmk from TeX Live):
  architecture (TikZ), build flow, DSE and every measurement as pgfplots plots. Plot data is extracted from
  `results/` into the committed `docs/report/data/*.csv` (`--no-data` builds from those only; `--clean` removes output).
- **Tests (no tools, no card):** `.venv/bin/python -m unittest discover -s flow/tests` (config rules, size rules,
  names, golden build ids, results, campaign expansion; < 1 s); gateware simulations
  `.venv/bin/python hw/gemm/tests/test_axi_native.py` and `hw/gemm/tests/test_ddr_dma.py` (Migen, ~1 min).
- **Outputs:** `build/gemm/hls/<hls_hash>/`, `build/gemm/soc/<build_id>/` (config, manifest, reports/summary.json,
  logs; `gemm_build.py <config> --report-only` rewrites the summary), `results/<campaign>/` (JSONL + CSV with build id,
  config hash, git sha, sizes, timing, counters; `invocations.jsonl`; `gemm_dse.py --resume` continues an interrupted
  campaign). All git-ignored.
- **Layout:** `kernels/gemm/` (adapted HLS C++, changes in `ORIGIN.md`), `hw/gemm/` (SoC, DMA↔DDR bridge, kernel
  wrapper, AXI perf counters, tests), `tcl/` (HLS flow, Vivado hooks, JTAG program), `sw/gemm_host/` (C++: DMA,
  kernel control, `gemm_runner`, `ddr_dma_test`), `flow/gemmflow/` (Python package), `scripts/gemm_*.py`,
  `configs/{gemm,dse}/`, `misc/` (debug cases).
