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
- [x] **HLS-only probe `dtype_hls` complete** (2026-11-24 03:25–04:45, `results/dtype_hls/`): 70 points (11 formats ×
      PE 16×{8…128} × mult {default, fabric}), stages sim + hls. **sim:** 32 ok (all float32/native-int points),
      38 skipped (ap_* types). **hls:** 62 ok, 8 failed (all fixed8.4). Every successful point has 512-bit m_axi ports,
      **including the packed 4-bit types** (tier T3 works through HLS: 128 lanes per 64-byte word).
      Default binding, PE 16×8 (128 MACs), HLS estimates:

      | Format | est. ns | DSP | LUT | FF | BRAM18 |
      |---|---|---|---|---|---|
      | float32 | 4.209 | 681 | 119.3k | 139.2k | 438 |
      | int32 | 4.209 | 425 | 67.1k | 88.9k | 438 |
      | fixed32.16 | 4.209 | 553 | 67.1k | 88.9k | 438 |
      | int16 / fixed16.8 | 4.144 | 167 | 61.8k / 64.0k | 55.1k / 63.8k | 306 |
      | int8 / uint8 | 4.048 | 167 | 59.5k | 50.1k | 240 |
      | int4 / uint4 | 4.048 | 36 | 56.1k | 36.3k | 207 |
      | int64 | 4.374 | 1316 | 109.6k | 139.4k | 1149 |

      Wider arrays (HLS): int8 PE 16×64 (1024 MACs) DSP 897, LUT 115k, BRAM18 1117; int4 PE 16×128 (2048 MACs) DSP 29,
      LUT 199k, BRAM18 1117 (43 min of HLS); int16 PE 16×32 DSP 545, BRAM18 1117; float PE 16×16 DSP 1315.
      **Findings:**
      - Narrow integers need far less of everything: int8 uses 1/4 of float's DSPs and half its LUTs at equal MACs;
        4-bit multiplies go to LUTs by themselves (DSP count stays at the ~30 of the address logic).
      - **`mult_resource: fabric` is useless:** the HLS estimate jumps to 7.6 ns for every format (even int4, whose
        multipliers are in LUTs anyway), so `config_op mul -impl fabric` also binds the kernel's 32-bit address
        multiplies. A per-variable binding would need a kernel change; the fabric variants are dropped.
      - **8-bit fixed point crashes Vitis HLS 2026.1:** `clang-3.9-csynth` (reflow pass) exits 254 for every
        `ap_fixed<8,*>`/`ap_ufixed<8,*>` tried (fixed8.0, 8.2, 8.4, ufixed8.4 — three extra probes at 04:55);
        fixed16/32 are fine. `numeric.py` now rejects 8-bit fixed point with this reason.
      - BRAM (the C tile, partitioned by lane width) limits the widest arrays before DSPs do for int8/int16.
- [x] Campaign changes from the probe: `int8_scale` 3 arrays (32×16, 32×32, 16×64; no fabric, no 32×64),
      `int16_scale` 3 arrays (no fabric), `fixed_point` = fixed32.16 only. **New DSE resource gate:** after HLS, a
      point whose estimate exceeds the device (BRAM18 + 150 for the SoC > 1910, or DSP > 1920) is recorded as
      `over_budget` and gets no Vivado run (HLS BRAM estimates were within ~5 % of Vivado's for the float builds).

## Phase 6: FPGA campaigns 🟡
User: "start DSE on the fpga with our script. keep going" (2026-11-24 ~04:05, unattended overnight).
- [x] **`dtype_same_array` done** (2026-11-24 04:53–05:53, `results/dtype_same_array/`): PE 32×8, tile 1024×512,
      tA, 200 MHz, default preset; peak 102.4 GOP/s for every format. All builds meet timing.

      | Format | DSP | SoC LUT | SoC FF | BRAM tiles | kernel LUT | WNS ns | 2048³ / 4096³ / 8192³ GOP/s | Verify |
      |---|---|---|---|---|---|---|---|---|
      | float32 (reused) | 1320 | 154.3k | 225.4k | 654.5 | 125.6k | 0.050 | 97.37 / 98.18 / 98.59 | ok |
      | int8 | **40** | 69.4k | 75.7k | 299.5 | 40.8k | 0.005 | 97.53 / 98.27 / 98.64 | ok (exact) |
      | int16 | 296 | 67.0k | 83.0k | 428.0 | 38.3k | 0.038 | 97.53 / 98.27 / 98.64 | ok (exact) |
      | int32 | 808 | 81.4k | 102.2k | 654.5 | 52.6k | 0.028 | 97.37 / 98.18 / 98.59 | **failed** |
      | fixed16.8 | 296 | 67.3k | 79.0k | 428.5 | 38.6k | 0.046 | 97.53 / 98.27 / 98.64 | ok (exact) |

      - Equal ops per cycle give equal throughput (compute bound, ~96 % of peak). The narrow formats are 0.1 % faster
        (fewer bytes per tile).
      - The cost falls sharply with width: int8 needs 1/33 of float's DSPs (Vivado maps the 8-bit multiplies to LUTs,
        HLS had estimated DSPs), 45 % of its LUTs and 46 % of its BRAM. int16 and fixed16.8 cost the same.
      - **int32 fails exact verification** (10–19 % of sampled entries). Debugged on the card,
        [misc/int32_verify](../../misc/int32_verify/README.md):
        - Whole C rows inside one M tile are wrong, in PEs 6, 10, 12, 14, 22, 26, 28, 30 (12 and 28 in every tile).
        - Errors appear only when both operands toggle the upper bits of the multiply-add: ±100 and full-range data
          fail; 0…100, −100…0 and constant data pass.
        - **With B = I (C = A) everything is exact**, so A arrives intact. The DDR BIST is clean, and the read-data
          CDC FIFO is block RAM with a safe schedule. The rails are in spec (VCCINT 0.954 → 0.947 V averaged under
          load, 48 °C).
        - The worst `accel` paths are `aBuffer` BRAM → 32-bit multiplier input at 56 ps slack and PE carry chains at
          62 ps.

        Working diagnosis: silicon timing failure under high switching activity in this placement, although STA
        passes. `int32_margin` (same array: aggressive preset at 200 MHz, and 180 MHz) is queued to confirm it.
        Lesson: STA "met" with tens of ps is not enough for heavy-activity datapaths, and exact full-range
        verification catches it. For floats, uniform(−1, 1) data with a relative tolerance would hide small errors.
      - float32 with integer-valued data (`--value-range` in float mode, exact; 1024³ at ±100, ±127, 0…127): 0 bad.
        That data switches little, though. **Bit-exact check with random data** (`--float-exact`: fp32 reference
        accumulated in the kernel's order, `c = c + a·b` over k) on the same build `a88777ec` finds **rare small
        errors, all in PE 12** (rows ≡ 12 mod 32): one tile row (512 entries) per 1024³ run, and 1 entry per 4098
        sampled at 4096³. They are ~4·10⁻⁶ relative, so the old tolerance check (4·K·eps·Σ|ab|) passed them. Correction
        (07:00) to the "float unaffected" note above.
      - **The headline float build `…225mhz_tA_aggressive-db5adf1c` (110.9 GFLOP/s) is bit-exact**: 0 bad over
        2×1024³ full, 2048³ full (4.2 M entries, via `gemm_run.py`) and 2×4096³ sampled, max rel error 0.0. This also
        confirms the reference model: the kernel's fp32 arithmetic matches the host bit for bit.
      - **Full exact screens** (`screen.sh`: 2×2048³, full, full-range data, 8.4 M entries each): int8 PE 32×8, int16
        PE 32×8, fixed16.8 PE 32×8 and **int8 PE 32×32 (the 379 GOP/s build): all 0 bad.**
      - PE 12 fails in two different netlists, but its `aBuffer` sits at different BRAM sites (int32 X5Y56, float
        X8Y76), so it isn't a weak site. What the two builds share is the 32-bit A distribution: 16 elements per
        word, PE p takes lane p mod 16. The int32 failures are exactly lanes 6/10/12/14 (PEs p and p+16). int8 and
        int16 distribute A differently and are clean. So the weak paths come from the RTL structure of the 32-bit
        A feed, and switching activity decides whether they fail.
      - **All earlier float builds screened bit-exactly** (07:13–07:23, `screen_all.sh`, 2×2048³ full,
        `--float-exact`, 8.4 M entries each, `misc/int32_verify/logs/screen_all.log`). 17 of 18 have 0 bad, among
        them the 225 MHz default and explore builds, both 250 MHz builds, every tile-size and PE 8×8 variant, and
        the m000/m100 AXI variants. Together with the headline `db5adf1c`, every float data point in the report
        is from a bit-exact build except **`a88777ec`** (PE 32×8, 200 MHz, default preset: rare PE 12 errors of
        ~4·10⁻⁶ relative). Its GFLOP/s are unaffected, but it is not exact. The 18th directory
        (`f32_pe8x8_t128x128-4e52e7f5`) predates the flow (no `config.resolved.json` or host tools) and was skipped.
      - **Flow changes from this:**
        - `gemm_dse.py` and `gemm_run.py` pass `--float-exact` for float builds (bit-exact instead of tolerance).
        - Rows carry `verify_exact`.
        - New board lock `build/.board.lock` (flock): the DSE run stage, `gemm_run.py` and the misc scripts hold
          it, so debug runs can't collide with a campaign.
      - Tools added: `gemm_runner --dump-bad FILE` (mismatches as `i j got ref`), `--value-range` for float32
        (integer values, exact check) and `--b-identity` (B = I, so C shows A as the kernel read it).
- [x] **`int8_scale` done** (05:53–06:43, `results/int8_scale/`; 200 MHz, tA, default preset):

      | Array (tile) | MACs | DSP | SoC LUT | SoC FF | BRAM tiles | WNS ns | 2048³ / 4096³ / 8192³ GOP/s (% of peak) | Verify |
      |---|---|---|---|---|---|---|---|---|
      | 32×16 (512×1024) | 512 | 40 | 90.4k | 89.6k | 300.5 | 0.049 | 188.9 / 190.3 / **191.0** (93 %) | ok |
      | 32×32 (512×1024) | 1024 | 40 | 130.1k | 119.0k | 414.5 | 0.038 | 375.5 / 378.3 / **379.4** (93 %) | ok |
      | 16×64 (256×1024) | 1024 | 42 | 118.0k | 106.2k | 529.5 | 0.007 | 371.5 / 374.2 / 375.3 (92 %) | **failed** |

      - **int8 PE 32×32 delivers 379 GOP/s, verified exactly: 3.4× the best float build** (110.9 GFLOP/s at
        225 MHz aggressive). It uses 40 DSPs (all multiplies in LUTs) and 30 % of the device's LUTs, so wider arrays
        are limited by LUTs and BRAM, not DSPs.
      - **PE 16×64 fails exact verification** (4–6 % of sampled entries), the second build to do so. Same probes as
        for int32 ([misc/int32_verify](../../misc/int32_verify/README.md)):
        - Here whole *columns* of one N tile are wrong (j mod 64 ∈ {24, 25, 27, 40, 41, 43, 56, 57, 59}, bits 5–7),
          i.e. a B element is seen wrong for a whole tile pass, by every PE.
        - 0…3 and 0…1 data pass; −3…3, −3…0 and full-range data fail.
        - **B = I is exact (A arrives intact), and A = I (C = B, A not toggling) is exact too**, over 3 runs. At the
          random-A failure rate, about 6 bad entries per run would be expected.

        So the failure needs both operands switching, the same signature as int32: a timing failure under heavy
        switching in a build with almost no slack.
      - Summary of exact checks so far:

        | Build | MACs, multiplier | WNS ns | Result |
        |---|---|---|---|
        | int32 PE 32×8 | 256, 808 DSP | 0.028 | fails |
        | int8 PE 16×64 | 1024, LUT | 0.007 | fails |
        | int8 PE 32×32 | 1024, LUT | 0.038 | passes |
        | int8 PE 32×8 | 256, LUT | 0.005 | passes |
        | float32 PE 32×8 | 256 | 0.050 | passes |
- [x] **`int16_scale` done** (06:43–07:13, `results/int16_scale/`; 200 MHz, tA, default preset):

      | Array (tile) | MACs | DSP | SoC LUT | SoC FF | BRAM tiles | WNS ns | 2048³ / 4096³ / 8192³ GOP/s (% of peak) | Verify |
      |---|---|---|---|---|---|---|---|---|
      | 32×16 (512×1024) | 512 | 552 | 85.7k | 102.3k | 414.5 | 0.061 | 188.7 / 190.2 / **190.9** (93 %) | ok |
      | 16×32 (256×512) | 512 | 557 | 69.6k | 90.9k | 530.0 | 0.071 | 185.7 / 186.8 / 187.7 (92 %) | ok |
      | 32×32 (512×1024) | 1024 | — | — | — | — | — | not built | `over_budget` |

      - **The resource gate worked:** int16 PE 32×32 needs 2045 BRAM18 by the HLS estimate (+150 for the SoC >
        1910) and got no Vivado run. int16 doubles the C-tile width per lane against int8, whose PE 32×32 needed
        only 662.
      - int16 uses one DSP per MAC (16×16 multiplies fit a DSP48E1), so 1024 int16 MACs would also take ~1060 of
        the 1920 DSPs: BRAM runs out first.
      - At equal MACs int16 matches int8 (190.9 vs 191.0 GOP/s at PE 32×16) for 14× the DSPs and fewer LUTs.
- [ ] Queue after `int16_scale` (06:15): `int32_margin` → `int4_probe` → `fixed_point` → `dtype_clock`.
      `dtype_clock` now uses the best verified int8 array, PE 32×32 tile 512×1024 (06:50); its 200 MHz default
      point reuses that build.
- [ ] Queued behind it (05:05): `dtype_size_sweep` (runs on the int8 build), `int8_scale`, `int16_scale`.
      `dtype_size_sweep` finished 05:53 (results below); `int8_scale` started 05:53 (HLS done for all 3 arrays).
- [x] **`dtype_size_sweep` done** (int8 PE 32×8 build, 45 sizes × 2 reps, all verified exactly,
      `results/dtype_size_sweep/`). Same whole-tile behaviour as float: the kernel always computes full
      1024 (N) × 512 (M) memory tiles. The kernel time for N = 256/512/1024 is the same (1024×1024×1024: 22.36 ms,
      96.0 GOP/s; 256×1024×1024: 22.36 ms, 24.0 GOP/s), and M = 256 takes as long as M = 512 (half rate). K costs
      little: K = 256 already reaches 88 GOP/s on full tiles, K ≥ 1024 reaches 96–98 GOP/s. Host overhead (DMA plus
      packing, e2e − kernel) at 1024³ is 5.6 ms (20 % of e2e). The float32 build at the same array spends 15.5 ms
      (41 %; e2e 37.9 ms against a 22.4 ms kernel) because it moves 4× the bytes. At 4096³ int8 spends 55 ms (4 %).
- Then: `int4_probe` (HLS ports are 512 bits, so it is worth building), `fixed_point` (fixed32.16), `dtype_clock`
  (on the best int8 array).

## Log

### 2026-11-24 — plan
- Plan written after reading the kernel, hlslib, Vitis headers, host runner, flow and DenseShift (arXiv 2208.09708).
  Native ints need no kernel change; fixed point at 8/16/32 bits and 2/4-bit ints should work through the generated
  `Config.h` only; other widths and wider accumulators need kernel changes and are out of scope.
