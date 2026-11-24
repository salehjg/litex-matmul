# Integer and fixed-point data types: progress

Plan: [int-dtype-plan.md](int-dtype-plan.md). Updated at every step; newest log entries on top; absolute dates.

Status legend: ✅ done · 🟡 in progress · ⬜ not started · ⛔ blocked

## Phase overview

| Phase | Content | Status |
|---|---|---|
| 1 | Plan and progress files | ✅ 2026-11-24 |
| 2 | Config schema, `numeric.py`, hash compatibility, rendering, operator binding, unit tests | ✅ 2026-11-24 |
| 3 | Host: `numeric.hpp`, `gemm_runner --dtype`, `make test`, float regression on the board | ✅ 2026-11-24 |
| 4 | Result columns, scripts, port-width check, `sim` stage, report fallback | ⬜ |
| 5 | Campaign files, HLS-only probe `dtype_hls` | ⬜ |
| 6 | Build and board campaigns (after "go") | ⬜ |

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

## Log

### 2026-11-24 — plan
- Plan written after reading the kernel, hlslib, Vitis headers, host runner, flow and DenseShift (arXiv 2208.09708).
  Native ints need no kernel change; fixed point at 8/16/32 bits and 2/4-bit ints should work through the generated
  `Config.h` only; other widths and wider accumulators need kernel changes and are out of scope.
