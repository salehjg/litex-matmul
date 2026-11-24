# Integer and fixed-point data types: progress

Plan: [int-dtype-plan.md](int-dtype-plan.md). Updated at every step; newest log entries on top; absolute dates.

Status legend: ✅ done · 🟡 in progress · ⬜ not started · ⛔ blocked

## Phase overview

| Phase | Content | Status |
|---|---|---|
| 1 | Plan and progress files | ✅ 2026-11-24 |
| 2 | Config schema, `numeric.py`, hash compatibility, rendering, operator binding, unit tests | ✅ 2026-11-24 |
| 3 | Host: `numeric.hpp`, `gemm_runner --dtype`, `make test`, float regression on the board | ⬜ |
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

## Log

### 2026-11-24 — plan
- Plan written after reading the kernel, hlslib, Vitis headers, host runner, flow and DenseShift (arXiv 2208.09708).
  Native ints need no kernel change; fixed point at 8/16/32 bits and 2/4-bit ints should work through the generated
  `Config.h` only; other widths and wider accumulators need kernel changes and are out of scope.
