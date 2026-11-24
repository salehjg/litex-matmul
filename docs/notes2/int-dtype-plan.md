# Integer and fixed-point data types of various widths: plan

Status and log: [int-dtype-progress.md](int-dtype-progress.md). Previous work (float32 kernel, flow, DSE, report):
[../notes1/gemm-hls-plan.md](../notes1/gemm-hls-plan.md), [../report/](../report/).

## 1. Goal

Explore integer (and fixed-point) element types of different widths on the existing GEMM accelerator **without
rewriting the HLS kernel**, with a proper design-space exploration:

- dtype and width are explicit parameters of the build config, of the DSE grids and of every driver script;
- every CSV/JSONL row says clearly which number format it measured (dtype, width, signedness, fractional bits);
- the schema is ready for later quantization schemes (fixed point, power-of-two such as DenseShift, mixed precision
  with a wider accumulator) even where the hardware does not support them yet;
- existing float32 builds keep their build ids, so nothing already built has to be rebuilt.

Scope of this round: plan, flow support, HLS-only feasibility probes. Vivado and board campaigns start when the
user says go (they are written and ready).

## 2. Facts this plan relies on (verified in the code, 2026-11-24)

### 2.1 Kernel
- One element type for everything: `kernels/gemm/include/Config.h.in:15` `using Data_t = ${MM_DATA_TYPE};`. A, B, C,
  the PE buffers (`cBuffer`, `Compute.cpp:26`) and the operators (`OperatorMap/OperatorReduce = hlslib::op::Multiply/
  Add<Data_t>`, `Config.h.in:34-35`) all use it. `hlslib::op::Product::Apply` returns `T` (`Operators.h:46-53`).
  **So integer products and sums wrap at the element width, and C is written at that width.** An int8 GEMM gives the
  exact result modulo 2^8. A wider accumulator needs a kernel change (a separate C/accumulator type).
- Native integer types work unchanged: `<cstdint>` is included (`Config.h.in:6-7`), the generic `DataPack` path packs
  `8*sizeof(T)` bits (`DataPack.h:28-37`), and the testbench (`tb/test_gemm.cpp:47-50, 77-86`) generates integers
  1..10 and compares integers exactly. Upstream supports integer types in its host code
  (`0c622a7`, `62c4e44 "Compare exact values when data type is integer"`).
- Arbitrary precision types (`ap_int<W>`, `ap_uint<W>`, `ap_fixed<W,I>`, `ap_ufixed<W,I>`):
  - `DataPack` packs them **densely by exact bit width W** (`DataPack.h:40-102`, `kBits = TypeHandler<T>::width`).
  - The kernel computes elements per bus word from `sizeof(Data_t)` (`MatrixMultiplication.h:26,32,45,57`), and
    `sizeof(ap_int<W>)` is 1/2/4/8 bytes for W ≤ 8/16/32/64 (`Vitis/include/etc/ap_private.h:1346`,
    `valtype<(W+7)/8>`).
  - So the m_axi port is `(bus_bytes / sizeof) * W` bits. The SoC needs exactly 512 (`hw/gemm/axi_native.py:20`,
    `hls_kernel.py:102`).
  - W = 8, 16, 32: dense = padded, works. Useful for fixed point, `ap_fixed<W, W-F>`.
  - W = 2, 4 (W divides 512, sizeof = 1): works if the kernel is told a *logical* bus width of `512/W` container
    bytes (128 for 4-bit). It then packs `512/W` elements densely into each 64-byte word. Needs an HLS probe.
  - W = 3, 5, 6, 12, 24, …: `(bus_bytes/sizeof)*W` cannot be 512, so these need a kernel change (elements from
    bits instead of `sizeof`). Out of scope.
  - `Config.h.in` does not include `ap_int.h`/`ap_fixed.h` before `Data_t`. The flow's renderer adds the include to
    the *generated* `Config.h` instead of editing the template.
  - The testbench cannot instantiate `ap_*` types (no `make_signed` overload, `Utility.h:110-126`): no g++/csim for
    them, board verification only.
- Resource binding: `mult_resource`/`add_resource` currently become `-DMM_*_RESOURCE` and the legacy
  `HLS RESOURCE core=` pragma (`Resource.h:24-26`), never used. Vitis 2026.1 has operator binding as
  `syn.op` / Tcl `config_op <op> -impl all|dsp|fabric|meddsp|fulldsp|maxdsp|primitivedsp`
  (`Vitis/data/hls/hls_ide_config_data.json`), with ops `mul`, `add`, `fmul`, `fadd`, ….

### 2.2 Flow and host
- `gemm_runner` is float-only: `std::vector<float>`, uniform(-1,1) data, double reference with a `4K·2^-23`
  relative tolerance, `bytes = n*k*4`, default multiples 16 (= 64 B / 4 B), and a JSON field `gflops`.
- `config.py`: `DATA_TYPES` = {float, half, int32_t, int16_t, uint8_t}; all rules divide by `elem_bytes`;
  `auto_name` only knows those names; `peak_gflops` = 2·PN·PM·f.
- **Hashes:** `config_hash` = SHA-256 of the whole `to_dict()`. `hls_hash` = kernel + clocks + hls sections +
  `tree_hash(kernels/gemm)`. `SocStep` inputs = config_hash, hls_hash, trees of `hw/gemm`, `boards/dupcb`,
  `tcl/vivado`.
  - **Adding a dataclass field, or editing any of those trees, changes every build id or stamp, and all existing
    builds would have to be rebuilt.** `flow/tests` pins the golden build ids.
  - `sw/gemm_host` only re-runs `make`. `flow/`, `scripts/`, `tcl/hls/` are not in any build hash.
- `gemm_dse.py` already expands any dotted key (`kernel.data_type`, …). `ResultStore` writes the union of all keys to
  CSV.

### 2.3 Power-of-two quantization (DenseShift, arXiv 2208.09708v3)
- Weights are `w = ±2^p`: a sign bit plus (n−1) exponent bits, **no zero** ("dense"). 2-bit {±1, ±2}; 3-bit
  {±1, ±2, ±4, ±8}. Zero is dropped because it wastes a code at n ≤ 4 and needs a bypass branch in a shift MAC.
- Activations are INT8, or 4-bit with PACT, in their experiments. A MAC becomes `sign · (x << p)` plus an add. The
  paper gives no accumulator width or overflow policy and no FPGA results.
- On gemm_hls this needs different types for A (activations), B (pow2 codes) and C (accumulator), plus a shift
  `OperatorMap`: a kernel change. This round only prepares the schema: the `pow2` kind, `pow2_exp_bits`,
  `pow2_dense`, and separate `a/b/c_dtype` columns.

## 3. What is supported, in tiers

| Tier | Formats | `c_type` in `Config.h` | Elements / 64-B word | Kernel edit | Verification |
|---|---|---|---|---|---|
| T0 | float32 (as today), half (schema only) | `float`, `half` | 16, 32 | none | float tolerance |
| T1 | int/uint × 8, 16, 32, 64 | `int8_t` … `uint64_t` | 64, 32, 16, 8 | none | exact (mod 2^W) |
| T2 | fixed/ufixed × 8, 16, 32, frac 0..W−1 | `ap_fixed<W, W−F>` (AP_TRN, AP_WRAP) | 64, 32, 16 | none (include in generated header) | exact (bit-true model) |
| T3 (probe) | int/uint × 2, 4 | `ap_int<W>`, `ap_uint<W>` | 256, 128 | none (logical bus width 512/W) | exact |
| later | int/fixed × 3, 5, 6, 12, 24 … | — | — | elements by bits | — |
| later | wider accumulator (e.g. int8 × int8 → int32) | — | — | separate C type | — |
| later | pow2 (DenseShift), mixed A/B/C types | — | — | split types + shift operator | — |

Unsupported combinations are rejected by config validation with the reason and a pointer to this table, so DSE grids
can sweep freely and invalid points are recorded as such.

## 4. Design

### 4.1 Config schema (`flow/gemmflow/config.py`, new `flow/gemmflow/numeric.py`)
```json
"kernel": {
  "data_type": "int",        // family: float | half | int | uint | fixed | ufixed   (pow2 reserved)
  "data_bits": 8,            // null for float (32) and half (16); required otherwise
  "frac_bits": null,         // fixed/ufixed only: fractional bits F, ap_fixed<W, W-F>
  "accum_bits": null,        // reserved: must be null or equal to data_bits (wider = kernel change)
  ...
}
```
- **Hash compatibility.** `config_hash` and `hls_hash` hash a `_hash_dict()` that leaves out `data_bits`,
  `frac_bits` and `accum_bits` while they are null. The rule: fields added after 2026-11-24 are hashed only when set.
  Float build ids are unchanged; the golden-id test proves it. `to_dict()` still contains the fields, so records are
  complete.
- `NumericFormat` (`numeric.py`) is resolved from the config:
  - kind, bits, signed, frac_bits;
  - `c_type`, container bytes (= sizeof), elements per 512-bit word, logical bus bytes;
  - canonical `spec` (`float32`, `int8`, `uint4`, `fixed16.8`) and short name (`f32`, `i8`, `u4`, `fx16f8`);
  - a rough DSP/LUT-per-MAC estimate (warning only);
  - `columns()`, the result-row fields of §4.3.

  `parse_spec()` reads a spec string back (used by tests; `gemm_runner` uses the same grammar).
- **Rules in elements.** `validate()`, `size_multiples()` and `config_h_values()` count elements per bus word
  (`512/bits`) instead of `bus_bytes/elem_bytes`. `bus_width_bytes` stays the physical 64 B, and `Config.h` gets the
  logical bus bytes in container units. For native types and fixed point the logical and physical widths are the
  same.
- **Rendering.** `render_config_h()` resolves the nested `${MM_DATA_WIDTH_${MM_DATA_TYPE}}` itself (type names such
  as `ap_fixed<16,8>` are not placeholder identifiers) and inserts `#include <ap_fixed.h>` for `ap_*` types.
- `auto_name()` uses the short name (`i8_pe32x16_t1024x512_200mhz_tA`); float names are unchanged.
- `peak_gops()` = 2·PN·PM·f for any type; `peak_gflops()` stays as an alias.
- **Operator binding.**
  - `mult_resource`/`add_resource` take `null | dsp | fabric`, and float types also `meddsp | fulldsp | maxdsp |
    primitivedsp`.
  - Each becomes `config_op <op> -impl <value>` in `tcl/hls/run_hls.tcl` (through `params.tcl`). `<op>` is `mul`/`add`
    for int and fixed, `fmul`/`fadd` for float.
  - This replaces the unused `-D` path. Nothing is emitted when null, so existing HLS runs are unchanged.

### 4.2 Host runner (`sw/gemm_host`, host rebuild only)
- **`src/numeric.hpp`** (header-only, no card needed):
  - parses the spec;
  - packs and unpacks W-bit lanes in the `DataPack` layout (element i at bits [iW, iW+W) of each 512-bit word,
    little-endian bytes);
  - provides a bit-exact model of the kernel arithmetic: product truncated to W bits (fixed point: arithmetic shift
    right by F, i.e. AP_TRN), sum wrapped to W bits, sign-extended when signed.
- **`gemm_runner --dtype <spec>`.** The default `float32` keeps today's behaviour exactly.
  - Integer and fixed data are uniform over the full code range, or `--value-range LO,HI`.
  - A^T and B are packed through the codec; byte sizes come from bits, default multiples from `512/bits`.
  - Verification is exact: `verify_bad` counts mismatches and `max_abs_err` is in LSBs. Sampled mode checks 4096
    random entries plus 2 corners.
  - JSON adds the dtype columns and `gops`, and keeps `gflops` for float.
- **`tests/test_numeric.cpp` and `make test`:** codec round trips at 2/4/8/16/32/64 bits, wrap-around, fixed-point
  truncation, against hand-computed cases.

### 4.3 Result rows (CSV and JSONL)
Every build row and run row gets flat columns from `NumericFormat.columns()`:

| Column | Meaning | Now |
|---|---|---|
| `dtype` | canonical spec (`float32`, `int8`, `uint4`, `fixed16.8`) | set |
| `dtype_kind` | quantization type: `float` \| `int` \| `fixed` \| `pow2` | set |
| `dtype_bits`, `dtype_signed`, `dtype_frac_bits` | width, signedness, fractional bits | set |
| `elems_per_word` | elements per 64-byte memory word | set |
| `acc_dtype`, `acc_bits` | accumulator format | = dtype |
| `a_dtype`, `b_dtype`, `c_dtype` | per-operand formats (mixed precision, e.g. DenseShift A=int8, B=pow2-3, C=int32) | = dtype |
| `quant_scale`, `quant_zero_point` | affine quantization parameters (host side) | null |
| `pow2_exp_bits`, `pow2_dense` | power-of-two format (DenseShift: dense = no zero) | null |
| `gops`, `peak_gops`, `pct_of_peak` | throughput in operations (2 per MAC) | set |
| `gflops`, `peak_gflops` | kept for float rows (back-compatible) | float only |

`ResultStore.LEADING` gains `dtype, dtype_kind, dtype_bits`; `SCHEMA_VERSION` 2 (old rows stay version 1). The DSE
checks that the dtype the runner reports equals the build's.

### 4.4 Driver scripts
- **`gemm_dse.py`:**
  - passes `--dtype`, adds the columns, computes `pct_of_peak` from GOP/s, and prints the dtype;
  - the HLS stage records the m_axi data width from `csynth.xml` and fails a point whose port is not 512 bits;
  - new optional `sim` stage: g++ build of `kernels/gemm/tb/test_gemm.cpp` with the rendered `Config.h`, seconds per
    point, for float and native ints; skipped with the reason for `ap_*` types.
- **`gemm_run.py`, `gemm_build.py`:** pass `--dtype`, add the columns, print dtype and peak GOP/s.
- **`docs/report/tools/extract_data.py`:** reads `gops` and falls back to `gflops`.

## 5. Campaigns (`configs/dse/`)

| Campaign | Grid | Stages | Builds / time | Question → decision |
|---|---|---|---|---|
| `dtype_hls` | f32, i8, u8, i16, i32, i64, fx8.4, fx16.8, fx32.16, i4, u4 × PM 8…128 (valid ones) × mult {default, fabric}; PE N 16, tile 512×512, tA | (sim,) hls | 0 / ~30 min | Which formats synthesise with a 512-bit port; DSP/LUT/BRAM per MAC; clock estimate → prune the build campaigns |
| `dtype_same_array` | f32 (reused), i8, i16, i32, fx16.8 at PE 32×8, tA, tile 1024×512, 200 MHz | build, run | 4 / ~2 h | Cost of each format at equal operations per cycle → which narrow types free DSP/BRAM |
| `int8_scale` | i8, PN {16, 32} × PM {16, 32} × mult {dsp, fabric}, pruned by `dtype_hls` | build, run | ≤ 8 / ~3 h | Highest int8 GOP/s with the freed resources |
| `int16_scale` | the same for i16, PM {8, 16, 32} | build, run | ≤ 6 / ~2.5 h | Highest int16 GOP/s |
| `int4_probe` | i4, u4 at PE 32 × {32, 64} | hls, build, run | 2 / ~1 h | Sub-byte lanes end to end (packing, port width, exact results) |
| `fixed_point` | fx8.4, fx16.8, fx32.16 at the best array | build, run | 3 / ~1.5 h | Fixed point vs integer of the same width |
| `dtype_clock` | best int config × 200/225/250 MHz × presets | build, run | ≤ 6 / ~3 h | Clock headroom of narrow datapaths |
| `dtype_size_sweep` | best int8 build, N/K/M grid | run | 0 / minutes | Whole-tile effect and DMA share with fewer bytes per element |

**Expected (to be checked).**
- At equal PE count the GOP/s match float. DSP drops sharply for int8/int16: ~1 DSP or 0 per MAC, against 5 for
  float. BRAM halves or quarters, since C tiles are narrower.
- int8 allows PM up to 64: 64 elements per word. The upper bound is about 2·32·64·200 MHz ≈ 819 GOP/s if LUTs and
  timing allow it.
- Bandwidth stays ample: intensity per byte grows as elements shrink.

## 6. Verification semantics
Integer and fixed-point results are exact. The kernel computes `C = Σ A·B` with every product and every partial sum
reduced to W bits; the host models this bit for bit, and verification requires equality.

With full-range random data the values wrap, which is fine for a throughput and resource study and still checks
every bit. Overflow-free data is possible only for small K or wide types; `--value-range` sets the input range. A
numerically meaningful low-precision GEMM needs the wider accumulator (§3, "later").

## 7. Phases
1. Plan and progress files (this).
2. Config schema, `numeric.py`, hash compatibility, rendering, operator binding, unit tests. Check: golden ids
   unchanged, every `configs/gemm/*.json` reported "up to date" in a dry run.
3. Host: `numeric.hpp`, `gemm_runner --dtype`, `make test`. Board regression with float32 on the loaded build.
4. Result columns, scripts (DSE, run, build), port-width check, optional `sim` stage, report extraction fallback.
5. Campaign files; HLS-only probe `dtype_hls` (and `sim`); results in the progress file.
6. On "go": build and board campaigns of §5, documented as in notes1.

## 8. Risks
- Vitis may compute `sizeof(ap_int<W>)` differently in synthesis than in C simulation. The probe checks the port
  width; T3 is dropped if it is not 512.
- `config_op` syntax or behaviour. The probe checks it; if it fails, the fields stay unused.
- Wide PM for int8 (64 lanes) gives wide C pipes and high fanout. The timing issues seen at 225 MHz for float
  (C-pipe enables) may come back; presets first.
- Full-range int64 multiplies are expensive in DSP and may not fit at large PM. That is a valid DSE outcome.
