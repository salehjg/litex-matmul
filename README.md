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
scripts/ddr_init.sh                      # calibrate + test both DDR3 channels (after every load)
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

## GEMM HLS accelerator (in progress)

An HLS GEMM kernel, adapted from [spcl/gemm_hls](https://github.com/spcl/gemm_hls), built into this SoC: host sends
A/B over PCIe DMA into both DDR3 channels, the kernel computes C, the host reads C back with performance counters.
JSON-driven build and design-space exploration. Plan: [`docs/notes1/gemm-hls-plan.md`](docs/notes1/gemm-hls-plan.md),
status: [`docs/notes1/gemm-hls-progress.md`](docs/notes1/gemm-hls-progress.md).

```
git submodule update --init --recursive   # submodules/gemm_hls (+ hlslib): reference only, never edited
```

Planned layout: `kernels/gemm/` (adapted HLS C++), `hw/gemm/` (LiteX SoC, DMA bridge, perf counters), `tcl/` (HLS and
Vivado hook scripts), `sw/gemm_host/` (C++ runner), `flow/gemmflow/` (Python build/DSE package),
`scripts/gemm_{build,run,dse}.py`, `configs/{gemm,dse}/`. Outputs go to `build/gemm/` and `results/` (git-ignored).
