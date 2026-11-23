# GEMM HLS accelerator on the DU-PCB LiteX SoC: plan

Status and dates are tracked in [gemm-hls-progress.md](gemm-hls-progress.md). This file describes *what* we build and
*why*. Update it when a design decision changes, not when a task is done.

## 1. Goal

From this PC, a host program:
1. sends GEMM inputs A (N×K) and B (K×M) over PCIe DMA into the card's DDR3,
2. starts an HLS GEMM kernel (`C = A·B`) on the FPGA,
3. reads C back over PCIe DMA,
4. reports performance numbers: kernel cycles, GFLOP/s, PCIe throughput, memory stalls, memory latency, and
   end-to-end time.

The flow around it:
- **One build driver** turns a JSON config into a bitstream and host software. The config holds clock, PE counts,
  tiling, FIFO depths, Vivado directives, thread count and so on.
- **One DSE / benchmark driver** sweeps build configs and matrix sizes. It writes every data point to CSV and JSONL
  with enough metadata to trace it back to the exact config, sources and tools.

The kernel is adapted from [spcl/gemm_hls](https://github.com/spcl/gemm_hls), which sits in `submodules/gemm_hls` as a
**read-only reference**. We copy what we need into `kernels/gemm/` and edit only the copy.

Priorities: simple and maintainable first. Correctness matters in the places it counts: CDC, counters, verification
and traceability. No framework building.

## 2. Platform facts this plan relies on

| Item | Value | Source |
|---|---|---|
| FPGA | Kintex-7 480T, `xc7k480tiffg1156-2L`, 1920 DSP48E1, 955 BRAM36 | board README §1.1 |
| PCIe | Gen2 x8 hard block, runs x4 in this PC. LitePCIe 128-bit @ `sys`, so ≤ 2 GB/s per direction | `boards/dupcb/target.py` |
| DDR3 | 2 channels × 64-bit DDR3-1000 × 2 GB. Native port 512 b @ 125 MHz (8 GB/s peak, 7.2 GB/s measured by BIST) | README "Verified" |
| Clocks | `sys` 125 MHz (clk200 #0), `accel` 200 MHz (S7PLL on clk200 #1), domains false-pathed | `target.py` `_CRG` |
| Free resources | CH1 crossbar, `accel` domain, `pcie_dma0` streams (only loopback today) | `target.py` |
| Tools | Vivado/Vitis 2026.1. HLS via `vitis-run --mode hls --tcl` (no `vitis_hls` on PATH). LiteX 2026.08 in `.venv` | `config.sh` |
| Host | 18 cores, **15 GB RAM**, so one 480T Vivado run at a time (a full build of the base SoC takes ~5–6 min, measured 2026-11-23) | `nproc`, `free` |
| Not yet verified | LitePCIe DMA through the generated kernel driver; reloading a bitstream in a running PC is fragile | README |

Keep-alive (AC24 high, unused pins pulled up) lives in `dupcb_platform.py`/`target.py`. **The GEMM SoC must inherit
it, never re-implement it.**

## 3. Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | **LiteX keeps generating and running the Vivado script.** Our own `.tcl` files hook in through `project_commands`, `pre_synthesis_commands`, `pre_placement_commands` and `additional_commands`. Directives and threads go through LiteX's `vivado_*` build args. | The LiteX script already gets the PCIe IP, LOCs, bitstream settings and keep-alive right. Our Tcl is limited to the parts we own. |
| D2 | **No CPU in the GEMM SoC.** Both channels (2 + 2 GB) belong to the kernel and DMA. | No contention on CH0. The host already does DDR calibration (`ddr_init.py`). |
| D3 | **C++ runner plus Python orchestration.** A C++ CLI does DMA, kernel control, counters and verification, and prints JSON. Python builds, programs the board and runs sweeps. | DMA and ring buffers stay in C next to liblitepcie. Python stays pure orchestration. |
| D4 | **Kernel control through LiteX CSRs, not AXI-Lite.** `ap_ctrl_hs`, `m_axi offset=direct`, scalars `ap_none`. | BAR0 maps only the CSR region. This avoids an AXI-Lite slave, a bus bridge and a second CDC. |
| D5 | **HLS kernel imported as an IP-catalog IP** (`export_design -format ip_catalog`, then `create_ip` + `synth_ip`). | The Xilinx-supported path. It brings in the floating-point subcores and synthesizes out of context. |
| D6 | **Matrix sizes are runtime arguments** (`MM_DYNAMIC_SIZES`). | One bitstream serves a whole size sweep, so builds are only needed for hardware knobs. |
| D7 | **Python never builds Tcl logic.** It writes a `params.tcl` (`set ::P(key) value`) and then `source`s fixed `.tcl` files. | Tcl stays readable and testable on its own. Python stays free of Tcl. |

## 4. Architecture

```
 PC                                     FPGA (GemmSoC, no CPU)
┌──────────────────────┐   PCIe Gen2   ┌───────────────────────────────────────────────────────────────┐
│ gemm_runner (C++)    │◄═════════════►│ pcie_dma0 ──► ddr_dma (ch sel, base, len) ──┬─► CH0 port ─┐   │
│  liblitepcie + csr.h │   DMA ring    │           ◄──                              └─► CH1 port ─┤   │
│  /dev/litepcie0      │               │                                                         │   │
│                      │   BAR0 CSRs   │ gemm_ctrl: args, start, done, build_id                  │   │
│ Python gemmflow      │◄─────────────►│ gemm_perf: counters (snapshot)                          │   │
└──────────────────────┘               │                                                         ▼   │
                                       │ HLS gemm IP ─ m_axi A,B,C ─ AxiPerfMonitor ─ AXI2Native ─ port│
                                       │   (accel clk)                    (accel)   (clock_domain=accel)│
                                       │                           LiteDRAM CH0 / CH1 crossbars        │
                                       └───────────────────────────────────────────────────────────────┘
```

### 4.1 Kernel (`kernels/gemm/`)

Copied from `submodules/gemm_hls` at commit `7c790eb` (`kernel/{Top,Memory,Compute}.cpp`,
`include/{MatrixMultiplication,Memory,Compute,Utility}.h`, `include/Config.h.in`). The BSD-3 headers stay, and
`kernels/gemm/ORIGIN.md` lists every change. The changes we plan:

- **Interface pragmas in `Top.cpp`:**
  - `m_axi port=a/b/c offset=direct bundle=gmem0/1/2` with `max_read_burst_length`, `max_write_burst_length`,
    `num_read_outstanding`, `num_write_outstanding` and `latency` taken from the config.
  - `ap_none` on `size_n/k/m`.
  - `ap_ctrl_hs` on `return`.
  - 32-bit addresses (`config_interface -m_axi_addr64=false`), enough for 2 GB per channel.
- **`Config.h.in`:** rendered by Python (`gemmflow.config`) instead of CMake. Same macro names, plus the values CMake
  used to compute (`MM_KERNEL_WIDTH_M`, `MM_DATA_WIDTH_*`, `kFrequency`).
- **Stream depths:** upstream hardcodes `kPipeDepth = 4`. It becomes a config knob, plus optional depths for the
  `aSplit`, `bMemory`/`cMemory` FIFOs. These are the "FIFO depth" knobs.
- **hlslib:** included unmodified from `submodules/gemm_hls/hlslib/include`. The defines
  `HLSLIB_SYNTHESIS`, `HLSLIB_STREAM_TIMEOUT=16` and `HLSLIB_LEGACY_SDX=0` are set by our HLS Tcl.
  **Fallback** if it does not compile under Vitis HLS 2026.1: copy only the needed headers to
  `kernels/gemm/third_party/hlslib/`, patch them there, and record that in `ORIGIN.md`.
- **Testbench:** `tb/test_gemm.cpp`, adapted from `test/TestSimulation.cpp`. It runs as a plain g++ build (hlslib
  software simulation, fast, no Xilinx tool) and as HLS `csim_design`. Sizes include ones that are not tile multiples.

Matrix convention (upstream): row-major, A is N×K, B is K×M, C is N×M. `K` must be a multiple of the A/B bus width in
elements. The runner pads where needed.

### 4.2 SoC (`hw/gemm/`)

- **`gemm_soc.py`: `GemmSoC(BaseSoC)`.** It subclasses `boards/dupcb/target.py:BaseSoC` (so CRG, DDR PHYs, PCIe and
  keep-alive are inherited) with `cpu_type=None` and no UART.
  - CH0 becomes a `LiteDRAMCore` named `sdram`, so the CSR prefixes stay `sdram_dfii_*`, `sdram_generator_*`, ...
    and `ddr_init.py` works unchanged. If `BaseSoC` needs a small hook for that, edit `target.py` and run
    `python3 setup.litex.du-pcb.py --pack` (the repo convention).
  - The CLI is a normal LiteX target with extra args `--gemm-config <resolved.json>` and `--hls-ip <dir>`.
- **`ddr_dma.py`: `PCIeDDRBridge`.**
  - Host → FPGA: `pcie_dma0.reader.source` (128 b) → `stream.Converter(128→512)` → `LiteDRAMDMAWriter` on the
    selected channel.
  - FPGA → host: `LiteDRAMDMAReader` → `Converter(512→128)` → `pcie_dma0.writer.sink`.
  - One writer/reader pair per channel, selected by a mux on a CSR.
  - CSRs: `ch`, `wr_base`, `wr_len`, `rd_base`, `rd_len`, `start`, `done`, byte counters. Data past `len` is dropped,
    so transfers padded to the 8 KB ring buffers do no harm.
  - Runs in `sys`, so no CDC.
- **`hls_kernel.py`: `HlsKernel`.**
  - Reads the RTL port list (name, direction, width) from the HLS `csynth.xml` and builds the `Instance`. Port names
    are never hand-written.
  - Wires each `m_axi_gmemX_*` group to a LiteX `AXIInterface(data_width=bus_width*8, address_width=32)`.
  - Wires scalars and addresses from CSRs. Their values are quasi-static (written before start), so a `MultiReg`
    per bit over the existing false path is enough.
  - Start: `PulseSynchronizer` (sys → accel). `ap_done`/`ap_idle` go back through `MultiReg`, and `done` is latched
    sticky until the next start.
  - Reset: `ap_rst_n`/`ap_rst` (polarity taken from the port list) driven from the accel reset plus a soft-reset CSR.
- **Memory side:** each bundle goes through `AxiPerfMonitor` → `LiteDRAMAXI2Native(axi, port)` →
  `sdramX.crossbar.get_port(clock_domain="accel")` (LiteDRAM adds the CDC). The bundle → channel map comes from
  `memory_map` in the config. Kernel addresses are byte addresses inside a channel (0 .. 2 GB).
- **Build ID:** a `CSRConstant`/`CSRStatus` `gemm_ctrl_build_id` holds the first 32 bits of the config hash. The runner
  refuses to run if it does not match the build directory.

### 4.3 Performance counters (`hw/gemm/axi_perf.py`)

All counters are 64-bit and run in `accel`. A `start` pulse clears them; `done` freezes them and makes a snapshot the
host can read. So every number belongs to exactly one kernel run.

| Group | Counter | Meaning |
|---|---|---|
| kernel | `cycles` | `ap_start` accepted → `ap_done` |
| per bundle (a, b, c) | `ar_count`, `aw_count` | read/write bursts issued |
| | `r_beats`, `w_beats` | data beats (× bus width = bytes) |
| | `ar_stall` | `arvalid & !arready`: interconnect not accepting addresses |
| | `r_wait` | `rready & !rvalid` while reads are outstanding: waiting on memory |
| | `r_backpressure` | `rvalid & !rready`: kernel not consuming |
| | `aw_stall`, `w_stall`, `b_wait` | the same for writes |
| | `rd_lat_min/max/sum` | AR handshake → first R beat. Timestamps go into a small FIFO; this is in-order because there is a single ID |
| | `rd_outstanding_sum` | Σ outstanding reads per cycle (Little's law: average latency = sum / ar_count) |
| per native port | `cmd_stall`, `rdata_beats`, `wdata_beats` | LiteDRAM controller back-pressure at the crossbar port |
| clock | `accel_freq` | existing FreqMeter; the host turns cycles into seconds with the *measured* clock |

Host-side numbers in the runner: H2D/D2H bytes, seconds, GB/s; kernel time; GFLOP/s = 2·N·K·M / t_kernel;
end-to-end time; % of peak (2·PN·PM·f).

A sanity check that comes for free: `r_beats` and `w_beats` must match the traffic the runner computes from the
tiling. The runner reports the ratio.

### 4.4 Vivado integration (`tcl/vivado/`)

LiteX writes `build/gemm/soc/<id>/gateware/<name>.tcl` as usual. Python puts these into the LiteX toolchain hooks:

| Script | Hook | Does |
|---|---|---|
| `params.tcl` (generated) | `project_commands` | `set ::P(...)`: paths, IP name, report dir |
| `project_settings.tcl` | `project_commands` | `general.maxThreads`, message severities |
| `import_hls_ip.tcl` | `pre_synthesis_commands` | `ip_repo_paths` → `update_ip_catalog` → `create_ip` → `generate_target` → `synth_ip` |
| `floorplan.tcl` | `pre_placement_commands` | optional pblock for the gemm hierarchy (config `vivado.floorplan`) |
| `reports_json.tcl` | `additional_commands` | WNS/TNS/WHS, per-clock slack, utilization (total and `gemm` hierarchy) → `reports/vivado.json` |
| `program.tcl` | standalone | JTAG programming through `hw_server` (used by `gemmflow.board`) |

Directive presets (in `gemmflow.config`, any explicit field overrides them):

| Preset | synth | opt | place | post-place phys_opt | route | post-route phys_opt |
|---|---|---|---|---|---|---|
| `default` | default | default | default | none | default | default |
| `explore` | default | Explore | Explore | Explore | Explore | Explore |
| `aggressive` | PerformanceOptimized | ExploreWithRemap | ExtraTimingOpt | AggressiveExplore | AggressiveExplore | AggressiveExplore |

Threads: `max_threads: "auto"` resolves to `min(8, nproc)`. 8 is Vivado's Linux cap, and this CPU has 18 cores.

### 4.5 HLS step (`tcl/hls/run_hls.tcl`)

Python writes `params.tcl` and runs `vitis-run --mode hls --tcl run_hls.tcl` in `build/gemm/hls/<hls_hash>/`. The
script does `open_project` / `open_solution -flow_target vivado`, `set_part`, `create_clock -period`,
`set_clock_uncertainty`, `config_interface -m_axi_addr64=false`, `config_dataflow -strict_mode warning`, then
optionally `csim_design`, then `csynth_design`, then `export_design -format ip_catalog [-flow syn|impl]`. The Tcl API
under 2026.1 gets verified in phase 3. Upstream's `scripts/Synthesis.tcl.in` is the starting reference.

`HlsReport` parses `csynth.xml`: estimated clock, latency, II, LUT/FF/DSP/BRAM, and the RTL port list.

### 4.6 Host software (`sw/gemm_host/`)

C++17, plain `Makefile`, built **per SoC build** against that build's generated `driver/user/liblitepcie` and
`csr.h`. So CSR addresses can never be stale.
- **`ddr_dma_test`:** random write/read-back/compare on each channel, GB/s per direction.
- **`gemm_runner`:**
  - Arguments: `--n --k --m --dtype --reps --warmup --seed --verify full|sampled|off --map a=ch0,b=ch1,c=ch1
    --base-a ... --json out.json`.
  - Steps: check `build_id`; generate A and B (seeded); DMA them in (timed); set args; start; poll `done`; read the
    counters and `accel_freq`; DMA C out (timed); verify.
  - Verification: a multithreaded blocked reference, or `sampled`, which checks a seeded random subset of C so large
    sizes stay fast. The float tolerance is relative and scales with K.
  - Output: one JSON object per repetition.
- **Kernel driver:** `make` in `driver/kernel`, `sudo insmod litepcie.ko`, done by `gemmflow.board`.

### 4.7 Python package (`flow/gemmflow/`)

Small, with inheritance only where it removes repetition:

| Module | Classes | Role |
|---|---|---|
| `config.py` | `KernelCfg`, `ClockCfg`, `HlsCfg`, `VivadoCfg`, `SocCfg`, `BuildCfg` (dataclasses) | load, merge defaults, apply presets, validate every upstream `static_assert` and CMake rule plus BRAM/DSP budget hints, render `Config.h`, stable hashes |
| `tools.py` | `Tool` → `VitisHls`, `Vivado`, `Make` | environment from `config.sh` (same as `scripts/env.sh`), logged subprocess, timeout, returns log path |
| `steps.py` | `Step` → `HlsStep`, `SocStep`, `HostSwStep` | input hash → stamp file; skip when up to date (`--force` overrides); each step writes its outputs and a JSON summary |
| `reports.py` | `Report` → `HlsReport`, `VivadoReport` | parse into flat dicts for result rows |
| `board.py` | `Board` | program (`program.tcl`), `scripts/pcie_rescan.sh`, `boards/dupcb/ddr_init.py`, insmod, build-ID check |
| `runner.py` | `GemmRunner` | invokes `gemm_runner`, parses JSON |
| `results.py` | `ResultStore` | append-only CSV + JSONL, `schema_version`, fixed column order, flattened config |
| `manifest.py` | functions | git sha/dirty/diff hash, submodule sha, tool versions, host info, UTC timestamps |

Entry points (run with `.venv/bin/python`):
- `scripts/gemm_build.py CONFIG.json [--stages hls,soc,host] [--force] [--dry-run]`: HLS → LiteX/Vivado → host
  software.
- `scripts/gemm_run.py BUILD_DIR --sizes 512,1024 ... [--program] [--reps 5]`: program, init and run one build.
- `scripts/gemm_dse.py CAMPAIGN.json [--resume] [--stages ...]`: sweeps (section 6).

## 5. Layout and naming

```
submodules/gemm_hls/           reference only, never edited (+ nested hlslib, powermeter)
kernels/gemm/                  adapted HLS C++ (src/, include/, tb/, ORIGIN.md)
hw/gemm/                       Migen/LiteX: gemm_soc.py, hls_kernel.py, ddr_dma.py, axi_perf.py, tests/
tcl/hls/                       run_hls.tcl
tcl/vivado/                    project_settings, import_hls_ip, floorplan, reports_json, program (.tcl)
sw/gemm_host/                  C++ runner + ddr_dma_test + Makefile
flow/gemmflow/                 Python package (+ flow/tests/ for pytest)
scripts/gemm_{build,run,dse}.py
configs/gemm/                  build configs
configs/dse/                   campaign configs
build/gemm/hls/<hls_hash>/     HLS project, params.tcl, export/ip, summary.json
build/gemm/soc/<name>-<hash8>/ LiteX output, gateware/, driver/, sw/, config.resolved.json, manifest.json, reports/
results/<campaign>/            campaign.json, builds.csv/.jsonl, runs.csv/.jsonl, logs/
docs/notes1/                   this plan + progress
```

Conventions:
- **Python:** `snake_case` modules and functions, `CamelCase` classes.
- **Tcl:** `snake_case.tcl`, all inputs through `::P(...)`.
- **JSON keys:** `snake_case`, units in the name (`_mhz`, `_bytes`, `_s`, `_gbps`).
- **Config file names** describe the point: `<dtype>_pe<PN>x<PM>_t<tileN>x<tileM>[_<clk>mhz][_<preset>].json`.
  `default.json` is the small bring-up config.
- **Hashes:** `config_hash` = sha256 of canonical JSON (sorted keys, defaults resolved), shown as 8 hex characters.
  `hls_hash` = hash of `kernel` + `clocks` + `hls` + the `kernels/gemm` source tree. Changing Vivado knobs therefore
  reuses the HLS IP.

## 6. Configuration

### 6.1 Build config (`configs/gemm/default.json`, bring-up size)

```json
{
  "name": "f32_pe8x8_t128x128",
  "kernel": {
    "data_type": "float",
    "parallelism_n": 8, "parallelism_m": 8,
    "memory_tile_size_n": 128, "memory_tile_size_m": 128,
    "bus_width_bytes": {"n": 64, "k": 64, "m": 64},
    "transpose_width_bytes": 64,
    "transposed_a": false,
    "fifo_depths": {"pipe": 4, "a_split": null, "b_memory": null, "c_memory": null},
    "add_resource": null, "mult_resource": null,
    "m_axi": {"max_burst_len": 64, "num_outstanding": 16, "latency": 64}
  },
  "memory_map": {"a": "ch0", "b": "ch1", "c": "ch1"},
  "clocks": {"accel_mhz": 200, "hls_uncertainty_pct": 27},
  "hls": {"run_csim": false, "export_flow": "none"},
  "vivado": {"preset": "default", "max_threads": "auto",
             "synth_directive": null, "opt_directive": null, "place_directive": null,
             "post_place_phys_opt": null, "route_directive": null, "post_route_phys_opt": null,
             "floorplan": null},
  "soc": {"sys_clk_mhz": 125}
}
```

### 6.2 Rough budget (to choose sensible sweep ranges)

- float32 MAC ≈ 3 DSP (fmul) + 2 DSP (fadd, full_dsp) = **~5 DSP**. 1920 DSPs give about 380 MACs at most.
- Bring-up: PN=8, PM=8 → 64 MACs, about 320 DSP, peak 2·64·200 MHz = **25.6 GFLOP/s**.
- Larger: PN=32, PM=8 → 256 MACs, about 1280 DSP, **102 GFLOP/s** at 200 MHz (if timing closes).
- Memory: A on CH0, B and C on CH1, each ≤ 8 GB/s. gemm_hls reuses data per memory tile, so large tiles are
  compute-bound. BRAM use grows with the tile size. Upstream's `scripts/optimal_memory_tile_size.py` logic goes into
  `config.py` as a budget warning.
- PCIe: ≤ 2 GB/s per direction (128 b @ 125 MHz), less at x4. For small sizes, end-to-end time is dominated by PCIe.
  That is why kernel time and transfer time are reported separately.

## 7. Build flow and traceability

`gemm_build.py CONFIG`:
1. Resolve the config (defaults + preset + validation), compute the hashes, create `build/gemm/soc/<name>-<hash8>/`,
   write `config.resolved.json` and `manifest.json`.
2. `HlsStep`: render `Config.h` and `params.tcl`, run `vitis-run`, parse `csynth.xml` → `hls/summary.json`. Skipped
   if `build/gemm/hls/<hls_hash>/stamp.json` matches.
3. `SocStep`: run `hw/gemm/gemm_soc.py --build --driver ...` with the LiteX Vivado args and our Tcl hooks →
   bitstream, `csr.csv`, `driver/`, `reports/vivado.json`.
4. `HostSwStep`: build `sw/gemm_host` against this build's `driver/user` and `csr.h` → `<build>/sw/`.
5. Write `reports/summary.json` (HLS + Vivado + manifest, flat), which becomes one row of `builds.csv` in a campaign.

Every result row carries `build_id`, `config_hash`, `hls_hash`, `git_sha`, `git_dirty`, `gemm_hls_sha`, tool
versions and timestamps. From those, any data point can be rebuilt.

## 8. Run flow on the board

`gemmflow.board.Board.bring_up(build)`:
1. Program over JTAG (`program.tcl`).
2. `scripts/pcie_rescan.sh`.
3. `ddr_init.py --build-dir <build>` (both channels).
4. `make` + `sudo insmod` of that build's `litepcie.ko` (`rmmod` first).
5. Check `build_id`.

If the rescan fails, the board needs a warm reboot. The DSE stops cleanly, records where it was, and `--resume`
continues from there.

## 9. DSE and benchmarking

Campaign file (`configs/dse/<campaign>.json`):

```json
{
  "campaign": "pe_sweep",
  "base": "configs/gemm/default.json",
  "build_grid": {"kernel.parallelism_n": [8, 16, 32], "clocks.accel_mhz": [150, 200]},
  "runtime_grid": {"n": [256, 512, 1024, 2048], "k": [256, 1024, 4096], "m": [256, 512, 1024, 2048],
                   "triples": null, "repetitions": 5, "warmup": 1, "verify": "sampled"},
  "stages": ["hls", "build", "run"],
  "jobs": {"hls": "auto", "vivado": 1}
}
```

- **Stages:** `hls` only gives a fast estimate sweep with no Vivado and no board. `build` adds timing and utilization.
  `run` adds board numbers.
- **Validation:** invalid grid points (failed validation, timing failure, sizes the kernel cannot take) are recorded
  with a `status` and a reason. They are never silently dropped.
- **Parallelism:** `jobs.hls: auto` = min(nproc/4, free RAM GB / 3). Vivado runs one build at a time.
- **Outputs** in `results/<campaign>/`:
  - `campaign.json`: the input plus resolved grid and manifest.
  - `builds.csv` / `.jsonl`: one row per build: IDs, flattened config, HLS estimates, Vivado WNS/WHS/utilization,
    build times, status.
  - `runs.csv` / `.jsonl`: one row per (build, N, K, M, repetition): IDs, sizes, dtype, kernel cycles and seconds,
    GFLOP/s, % of peak, H2D/D2H bytes, seconds and GB/s, end-to-end seconds, every per-bundle counter and derived
    latency/stall ratios, native-port stalls, measured accel MHz, verify status, max error.
  - `logs/`: tool logs, linked by `build_id`/`run_id`.
- **Column order** is fixed and `schema_version` sits in every row. Adding a column bumps the version.
- No plotting is built in for now. The CSVs are meant for pandas or a notebook.

## 10. Phases

| Phase | Content | Pass criterion |
|---|---|---|
| 0 | Scaffolding: submodule, docs, `.gitignore`, README | listed in the progress file |
| 1 | Host DMA bring-up on the existing SoC: build + insmod the generated LitePCIe driver, `litepcie_util dma_test` | loopback with no errors, GB/s recorded |
| 2 | `GemmSoC` (no CPU) + `ddr_dma.py` + `ddr_dma_test` | `ddr_init.py` passes on both channels; random 256 MB write/read-back has no errors on CH0 and CH1; GB/s recorded |
| 3 | Kernel port + `config.py` (render/validate) + `HlsStep` + `HlsReport` | g++ sim and HLS csim pass (including non-tile-multiple sizes); csynth estimated clock ≤ target; IP exported |
| 4 | `hls_kernel.py`, `axi_perf.py`, Vivado Tcl hooks | fast `--no-compile-gateware` check passes; full build of `default.json` meets timing; `reports/vivado.json` written |
| 5 | `gemm_runner` end to end | correct C for several sizes and both memory maps; counters consistent with analytic traffic |
| 6 | `gemm_build.py` complete (stages, caching, manifest, summary) | rerun with no change skips every step; a Vivado-only change reuses the HLS IP |
| 7 | `gemm_dse.py` + example campaigns (PE, clock, preset, size sweeps) | campaign runs to completion, resumes after an interruption, CSV/JSONL complete |
| 8 | README usage + final notes | — |

Phases 1 and 3 do not depend on each other; phase 3 (HLS only) can start while board work is blocked.

## 11. Risks and open points

| Risk | Plan |
|---|---|
| ~~The LitePCIe kernel module may not build on Linux 7.3 (Arch), or the DMA path may misbehave (never verified on this card)~~ | **Resolved in phase 1 (2026-11-23):** the module builds unchanged on 7.3.5; the DMA stalled because the stock S7 PHY takes the 128-bit RX byte enables from `tkeep` (LitePCIe 2026.08 regression). Fixed by `boards/dupcb/s7pciephy_rx_be.py`: 13.43 Gb/s loopback, 0 errors |
| The host froze twice while debugging the DMA (2026-11-23), cause unknown, not reproduced by the bisection | Crash capture (`misc/crash_debug/`) armed for all board work; the IOMMU stays on; details in the progress file |
| hlslib @ a67e02f / gemm_hls (Vitis 2021.1) vs Vitis HLS 2026.1 | Fallback: copy and patch the needed headers (section 4.1). |
| `vitis-run` Tcl API changes in 2026.1 | Verify on a tiny kernel first (phase 3) |
| Timing at 200 MHz on -2L with large PE counts | `accel_mhz` and the presets are DSE knobs; floorplan option |
| Reloading the bitstream in a running PC drops PCIe; rescan may not bring it back | Board step detects it, and the DSE pauses and resumes. Group runs per build so each campaign reprograms as little as possible |
| 15 GB RAM | One Vivado build at a time; HLS jobs limited by free RAM |
| ddr_init.py without a CPU (it used to run alongside the BIOS) | Check in phase 2 |
| CH0 as `LiteDRAMCore` may change CSR names | Name the module `sdram`; compare `csr.csv` before and after |

## 12. References

- `submodules/gemm_hls` (`README.md`, `kernel/`, `include/`, `scripts/Synthesis.tcl.in`, `scripts/build_manager.py`)
- de Fine Licht et al., "Flexible Communication Avoiding Matrix Multiplication on FPGA with High-Level Synthesis",
  FPGA'20
- `my-fpga-ws/boards/DU-PCB-001-003/README.md` (board), `my-fpga-ws/submodules/openTPU/docs/` (LiteDRAM and the
  wide-DRAM notes from the sister card)
- `deps/litedram/litedram/frontend/{axi,dma}.py`, `deps/litepcie/litepcie/software/`,
  `deps/litex/litex/build/xilinx/vivado.py`
