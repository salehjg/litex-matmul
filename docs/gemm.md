# GEMM HLS accelerator: usage and notes

An HLS GEMM kernel (float32 and integer/fixed-point formats, adapted from [spcl/gemm_hls](https://github.com/spcl/gemm_hls))
in a CPU-less variant of the base SoC ([soc.md](soc.md); `hw/gemm/gemm_soc.py`): the host sends A and B over PCIe DMA
into the DDR3 channels, the kernel computes C = A·B at 180–225 MHz, the host reads C back together with performance
counters (cycles, per-matrix AXI bursts, stalls, read latency). JSON-driven build and design-space exploration. Plan:
[`docs/notes1/gemm-hls-plan.md`](notes1/gemm-hls-plan.md), status and measurements:
[`docs/notes1/gemm-hls-progress.md`](notes1/gemm-hls-progress.md).

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

Integer and fixed-point formats (same kernel, element type from the config; [`docs/notes2/`](notes2/int-dtype-progress.md)),
best builds so far, all verified exactly on the card:

| Format | Array, tile, clock | Peak | Best measured | LUT / DSP / BRAM tiles |
|---|---|---|---|---|
| int4 | PE 64×64, 512×4096, 180 MHz, `aggressive` | 1474.6 GOP/s | **1290.1** (8192³, 87.5 %) | 200.3k / 38 / 652.5 |
| int4 | PE 32×64, 512×2048, **225 MHz** | 921.6 | 854.1 (8192³, 92.7 %) | 117.5k / 39 / 414.5 |
| int8 | PE 32×32, 512×1024, **225 MHz** | 460.8 | **426.6** (8192³, 92.6 %) | 131.3k / 40 / 414.5 |
| int16 | PE 32×16, 512×1024, 200 MHz | 204.8 | 190.9 (8192³, 93.2 %) | 85.7k / 552 / 414.5 |
| int32 | PE 32×8, 1024×512, 200 MHz, `aggressive` | 102.4 | 98.6 (8192³, 96.3 %) | 81.3k / 808 / 654.5 |
| fixed32.16 | PE 32×8, 1024×512, 200 MHz, `aggressive` | 102.4 | 98.6 (8192³, 96.3 %) | 79.2k / 1064 / 654.5 |

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
  Vivado preset/directives/threads). Checked against every gemm_hls rule before any tool runs (`flow/gemmflow/config.py`),
  including two the kernel needs but upstream does not check: tile_m/PM ≥ PN, and tile_m/PM, tile_n/PN powers of two
  ([misc/pe_forwarding](../misc/pe_forwarding/README.md)).
- **Timing:** a build that misses timing anywhere is recorded as `timing_failed` and never loaded. The PCIe Gen2 x8
  core (500 MHz, inside the hard block) has only tens of ps of margin in every build and failed alone once; if a build
  fails only there, rebuild with `vivado.preset: explore` or `aggressive`.
- **Element formats** ([`docs/notes2/`](notes2/int-dtype-plan.md)): `kernel.data_type` = `float` | `int` | `uint` |
  `fixed` | `ufixed` with `data_bits` (8/16/32/64; 2/4 packed) and `frac_bits` (fixed point), no kernel change; the
  kernel accumulates in the element type (integer results wrap). Rows carry `dtype`, `dtype_kind`, `dtype_bits`, … and
  `gops`; `gemm_runner --dtype int8` etc. verifies exactly. Campaigns `configs/dse/dtype_*.json`, `int*_scale.json`.
- **Verification is exact:** integer and fixed-point formats always. float32 bit for bit against a reference that
  accumulates in the kernel's order (`--float-exact`, default in `gemm_dse.py` and `gemm_run.py`; rows carry
  `verify_exact`).
- **DDR read margin under load** ([misc/int32_verify](../misc/int32_verify/README.md)): with the GEMM array busy, the
  idle-calibrated read taps of some byte lanes sit at the edge of their window. Single DQ bits then flip on odd beats
  (CH0 DQ5 on this card), which gives wrong C entries in some builds although the kernel is right.
  - `gemm_run.py --ddr-load-tune`, or `"ddr_load_tune": true` in a campaign, re-tunes the taps under load after
    calibration (`gemmflow.ddr_tune`, ~30 s, host-only). Rows record `ddr_load_tuned` and the error counts before
    and after.
  - The board lock `build/.board.lock` keeps tools from sharing the card.
- **Sizes:** N, K, M must be whole bus words for the build (64-byte words: K and M multiples of 16 floats, N too with
  transposed A); `gemm_runner` rejects other sizes. The kernel computes whole memory tiles, so matrices smaller than
  the tile run at a fraction of the peak (size sweep in the progress file).
- **Host ↔ DDR transfers:** H2C 0.82 GB/s, C2H 1.49 GB/s (the driver's DMA stop costs 1 ms per 2 MB chunk on H2C).
- **DDR placement** (default `memory_map` a=ch0, b=ch1, c=ch1): A at CH0 0x0, B at CH1 0x0, C at CH1 0x4000_0000;
  limits A ≤ 2 GB, B ≤ 1 GB, C ≤ 1 GB, checked by `gemm_runner` (details in the progress file, "DDR memory map").
- **Report:** `docs/report/build.sh` builds `docs/report/report.pdf` (git-ignored; pdfLaTeX + latexmk from TeX Live):
  architecture (TikZ), build flow, DSE and every measurement as pgfplots plots. Plot data is extracted from
  `results/` into the committed `docs/report/data/*.csv` (`--no-data` builds from those only; `--clean` removes output).
  `docs/report/tools/export_figures.sh [plots/<x> ...]` renders single plots/figures to `docs/images/*.png` (README).
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
