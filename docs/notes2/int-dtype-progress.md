# Integer and fixed-point data types: progress

Plan: [int-dtype-plan.md](int-dtype-plan.md). Updated at every step; newest log entries on top; absolute dates.

Status legend: ✅ done · 🟡 in progress · ⬜ not started · ⛔ blocked

## Phase overview

| Phase | Content | Status |
|---|---|---|
| 1 | Plan and progress files | ✅ 2026-11-24 |
| 2 | Config schema, `numeric.py`, hash compatibility, rendering, operator binding, unit tests | ✅ 2026-11-24 |
| 3 | Host: `numeric.hpp`, `gemm_runner --dtype`, `make test`, float regression on the board | ✅ 2026-11-24 |
| 4 | Result columns, scripts, port-width check, `sim` stage, report fallback | ✅ 2026-11-24 |
| 5 | Campaign files, HLS-only probe `dtype_hls` | 🟡 files written, probe next |
| 6 | Build and board campaigns (after "go") | 🟡 go given 2026-11-24 ~04:05 |

## Phase 2: config schema ✅
- [x] `flow/gemmflow/numeric.py`: `resolve()` / `parse_spec()` → `NumericFormat` (kind, bits, signed, frac_bits,
      `c_type`, container bytes, elements per 512-bit word, logical bus bytes, spec `int8`/`uint4`/`fixed16.8`, short
      name `i8`/`u4`/`fx16f8`, DSP-per-MAC estimate, result columns). Tiers T0–T3 as in the plan; everything else is
      rejected with the reason (12-bit: "kernel to count elements by bits", pow2: "DenseShift … not supported yet",
      `int32_t`: "use data_type 'int' with data_bits 32", wider `accum_bits`: "separate C type").
- [x] `config.py`: `kernel.data_bits/frac_bits/accum_bits` (default null), hashed only when set (`LATE_FIELDS`,
      `_hash_dict`); validation, `size_multiples`, `Config.h` widths all in elements; the nested
      `${MM_DATA_WIDTH_${MM_DATA_TYPE}}` resolved by the renderer; `#include <ap_fixed.h>` added to the generated
      `Config.h` for `ap_*` types; `auto_name` from the short name; `peak_gops` (alias `peak_gflops`).
- [x] Operator binding: `mult_resource`/`add_resource` = `dsp|fabric|all` (float also `meddsp|fulldsp|maxdsp|
      primitivedsp`) → `config_op <mul|add|fmul|fadd|hmul|hadd> -impl <v>` in `run_hls.tcl` via `params.tcl`; the
      old `-D` pragma path is gone; nothing is emitted when null.
- [x] **Compatibility check:** all 24 configs (4 committed + 20 build dirs) give the same build id, HLS hash, size
      multiples and peak as before, and a byte-identical `Config.h`. `gemm_build.py --dry-run` on every committed
      config: nothing new to run (the two stale steps shown predate this change: the first build has no stamps, and
      the 512×512 build's host stamp is older than the C2H fix).
- [x] Tests: 30 pass (new: formats and specs, rejections, logical bus width, columns, int8/fixed/int4 configs and
      their `Config.h`, rules in elements, op bindings; updated two messages).

## Phase 3: host ✅
- [x] `sw/gemm_host/src/numeric.hpp` (header-only): spec parsing (same grammar as `numeric.py`), dense little-endian
      packing in the `DataPack` bit order (element e at bit e·W), and the kernel-arithmetic model (products and sums
      wrap at W bits; fixed point: product floored by 2^F = AP_TRN, AP_WRAP; accumulation mod 2^64 and one final
      wrap, which is equivalent).
- [x] `make -C sw/gemm_host test` (`tests/test_numeric.cpp`, no card): specs and rejections, wrap, hand-computed dot
      products (int8, int64, fixed16.8 incl. rounding toward −∞, ufixed8.4), pack/unpack round trips at 2…64 bits,
      nibble order. **Cross-check against Vitis `ap_int`/`ap_fixed`:** 2000 random 16-term dot products each for
      int2, uint4, int8, uint16, int32, int64, fixed8.4, fixed16.8, ufixed16.8, fixed32.16, ufixed32.12: all equal.
- [x] `gemm_runner --dtype <spec>` (default `float32`, unchanged behaviour) and `--value-range LO,HI` (codes):
      integer/fixed data as raw codes, packed for the card (A^T for transposed-A builds), byte sizes from bits,
      default multiples 512/bits, exact verification (`verify_bad`, `max_abs_err` in LSBs). JSON adds `dtype`,
      `dtype_kind`, `dtype_bits`, `dtype_signed`, `gops` (and keeps `gflops` and `max_rel_err` for float).
- [x] **Float regression on the card** (loaded build `…225mhz_tA_explore-8c43d008`): 2048³ 109.33 GFLOP/s, max rel
      1.30e-07 (identical to the campaign record, same seed); 1024³ full verification OK (1 048 576 entries).

## Phase 4: rows, scripts, checks ✅
- [x] Result rows: every build and run row of `gemm_dse.py` (and `gemm_run.py` meta) carries
      `NumericFormat.columns()`: `dtype`, `dtype_kind`, `dtype_bits`, `dtype_signed`, `dtype_frac_bits`,
      `elems_per_word`, `acc_dtype`, `acc_bits`, `a_dtype`, `b_dtype`, `c_dtype`, `quant_scale`, `quant_zero_point`,
      `pow2_exp_bits`, `pow2_dense`, plus `peak_gops`; runs also `gops`, `pct_of_peak` from GOP/s (`gflops` and
      `peak_gflops` kept for float). Invalid points record `data_type/data_bits/frac_bits`. `ResultStore`:
      `SCHEMA_VERSION` 2, leading CSV columns `dtype, dtype_kind, dtype_bits`.
- [x] `gemm_dse.py`: `--dtype` to the runner; status `dtype_mismatch` if the runner reports another format; prints
      dtype and GOP/s; **linked grid axes** (`"@format": [{"kernel.data_type": "int", "kernel.data_bits": 8}, …]`,
      `"@array": [{PE + matching tile}, …]`) so format and array sweeps contain no meaningless points.
- [x] `gemm_run.py`, `gemm_build.py`: `--dtype`, columns, dtype and peak GOP/s in the output.
- [x] Port-width check: `HlsReport.axi_data_bits()`; `HlsStep` fails a point whose m_axi data width is not 512
      bits (before Vivado); `summary.json` records `axi_data_bits`.
- [x] `sim` stage (`SimStep`, also `gemm_build.py --stages sim,...`): g++ build of the upstream testbench with the
      rendered `Config.h` at one small legal size, results under `build/gemm/sim/<hls_hash>/`. Tried: float32, int8,
      int16, uint8 on PE 8×8 pass in ~3.5 s each (integer results wrapped and compared exactly); fixed point is skipped
      with the reason.
- [x] `auto_name` adds the operator binding (`_mulfabric`, `_adddsp`); unchanged for all existing configs.
- [x] Report: `extract_data.py` reads `gops` (fallback `gflops`) and adds a `dtype` column to `builds.csv`; the
      report still builds (0 warnings).
- [x] Tests: 34 pass (new: linked axes and float-build reuse, invalid format names, leading CSV columns, `SimStep`).

## Phase 5: campaigns 🟡
- [x] `configs/dse/`: `dtype_hls` (70 valid points: 11 formats × 5 PE widths × mult {default, fabric}; 40 invalid
      combinations recorded), `dtype_same_array` (5; float32 = existing build `a88777ec`), `int8_scale` (8),
      `int16_scale` (6), `int4_probe` (4), `fixed_point` (3), `dtype_clock` (6), `dtype_size_sweep` (run only).
      All dry-run clean.
- [ ] HLS-only probe `dtype_hls` (stages sim, hls): started 2026-11-24 03:25 (an earlier note said ~03:50; the
      record says 03:25), log `results/dtype_hls/dse.log`; at 04:01 the sims were done and 56 of 70 HLS runs finished.

## Phase 6: FPGA campaigns 🟡
User: "start DSE on the fpga with our script. keep going" (2026-11-24 ~04:05, unattended overnight).
- [ ] `dtype_same_array` (int8, int16, int32, fixed16.8 at PE 32×8, tile 1024×512, 200 MHz; float32 reused):
      queued behind `dtype_hls` (Vivado needs the RAM the HLS jobs use), log `results/dtype_same_array/dse.log`.
- Order after that, decided from the HLS probe and the first results: `int8_scale` / `int16_scale` (pruned),
  `int4_probe` (only if the HLS ports are 512 bits), `fixed_point`, `dtype_clock`, `dtype_size_sweep`.

## Log

### 2026-11-24 — plan
- Plan written after reading the kernel, hlslib, Vitis headers, host runner, flow and DenseShift (arXiv 2208.09708).
  Native ints need no kernel change; fixed point at 8/16/32 bits and 2/4-bit ints should work through the generated
  `Config.h` only; other widths and wider accumulators need kernel changes and are out of scope.
