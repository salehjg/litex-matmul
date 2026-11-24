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
- [x] **`int32_margin` done** (07:13–07:50, resumed after a transient `cs_server` init error at JTAG programming):
      - aggressive preset at 200 MHz (WNS 0.044 ns): 95.9 / 98.2 GOP/s, **exact** (also 2×2048³ full).
      - 180 MHz default (WNS 0.022 ns): 86.3 / 88.4 GOP/s, **fails** (PEs 6 and 22, lane 6 again).

      A lower clock gave no margin, which led to the root-cause hunt below.
- [x] **Root cause of the wrong results: DDR3 read-capture margin under load** (07:50–08:10,
      [misc/int32_verify](../../misc/int32_verify/README.md); supersedes the timing-closure hypotheses above).
      - **Which bits flip.** `gemm_runner --dump-inputs` + `solve_delta.py` recover each corrupted A element. All
        are **single-bit flips at word bit ≡ 5 (or 4/6) mod 64, i.e. DQ5 of CH0, on odd beats 3/5/7**.
        - The PHY maps word bit = 64·beat + DQ.
        - The int8 PE 16×64 errors (B on CH1) map to DQ5–7/13–15/29–31 on beats 3/5/7.
        - The float PE 12 errors are mantissa flips of the same size.
      - **Moving only CH0 lane 0's read IDELAY tap** (same bitstream and data, `lane_sweep.sh`) changes the int32
        error rate from ~17 k (taps 13–15) to 97 k (calibrated tap 16) to ~524 k of 1 M (taps ≤ 11 and ≥ 17).
        Calibration at idle found an 11-tap window; under GEMM load it is ~3 taps, and not fully clean even there.
      - **Load, not traffic:** DMA streaming at the same taps with an idle core is clean (even at tap 17–18), and so
        is the kernel with B = I (same A traffic, almost no compute).
      - The WL7DDRPHY samples DQ with the internal `sys4x_w` clock (odd beats on `CLKB`), not DQS, and has one tap
        per byte lane (IDELAY `CE`/`LD` are not gated by `dly_sel_bits`). Core switching noise shifts the capture
        point, and the late bits of a lane fall off its window.
      - **Consequences:**
        - A build fails or passes depending on how strongly its core activity couples into the PHY, not on its
          own correctness.
        - "verify_failed" rows in these campaigns (int32 PE 32×8 200/180 MHz, int8 PE 16×64) are DDR read errors
          and say nothing about the formats. Their GOP/s are valid.
        - The integer formats themselves are correct on the card (many builds exact over 8.4 M entries each).
      - **Fix options (SoC/board changes, not made: user decision, every build would rebuild):**
        - per-bit IDELAY control plus per-bit deskew calibration;
        - calibrating the lane taps under GEMM load (host-only, prototype `lane_sweep.sh`);
        - DQS-based capture or a lower DDR rate.
- [x] **Host-only fix: read taps tuned under load** (08:10–08:30). `gemmflow.ddr_tune` keeps the bring-up's
      calibration and, per byte lane, sweeps the read tap around it while the build's own kernel runs an exact
      check, then takes the centre of the zero-error run (2 passes; misc prototype `load_tune.py`). Applied to every
      build that had failed (2×2048³ full afterwards, 8.4 M entries each):

      | Build | Bad before (2048³ full) | Bad after | Taps changed (calibrated → tuned) |
      |---|---|---|---|
      | int32 PE 32×8 200 MHz `c0ddddde` | 97–102 k per 1024³ | **0** | CH0 lanes 0/1: 16 → 14 (lane 1 13–15 clean) |
      | int32 PE 32×8 180 MHz | 2–4 k | **0** | CH0 lanes 0–4 down 1–2 taps |
      | int8 PE 16×64 | ~31 k | **0** | CH1 lanes 0–2/7 down 1–2 taps |
      | float32 PE 32×8 200 MHz `a88777ec` | ~34 k (bit-exact check) | **0**, max rel 0.0 | CH0 lanes 0–2 down 1–2 taps |

      - The under-load windows are 3–8 taps on CH0 and 2–7 on CH1; idle calibration reported 10–12. The idle
        centre sits at or just past the upper edge on CH0 lanes 0–2.
      - Throughput is unchanged (e.g. int32 98.59 GOP/s at 8192³).
      - In the flow: `Board.bring_up(tune_runner=…)`, `gemm_run.py --ddr-load-tune`, campaign key
        `"ddr_load_tune": true` (rows: `ddr_load_tuned`, `ddr_tune_bad_before/after`; result in
        `<build>/logs/ddr_tune.json`).
      - End to end `gemm_run.py --program --ddr-load-tune` on `c0ddddde`: 102 400 bad → 0, then 2048³ full OK,
        60 s including JTAG.
      - Off by default. Enabled for the campaigns that had not started yet (`fixed_point`, `dtype_clock`).
        `int4_probe` started before this and runs untuned (re-run with `--resume` and the key if it fails).
      - A permanent fix belongs in the SoC (per-bit IDELAY, DQS capture) or in `ddr_init.py` (calibrate under load).
        Both touch `boards/dupcb` and invalidate every build: user decision.
- [x] **Hazard found and fixed (08:20):** importing `boards/dupcb/ddr_init.py` as a module (the tuner, the misc
      probes) wrote `boards/dupcb/__pycache__/ddr_init.cpython-314.pyc`. `tree_hash` hashes `__pycache__` too, and
      `boards/dupcb` is a SoC input, so every build looked stale. A smoke test of the DSE then started rebuilding
      `c0ddddde`; I stopped it in placement (08:20). Its synthesis outputs and `vivado.log` were regenerated, but the
      bitstream, routed checkpoint, timing reports and stamp are the originals, and its runs verify.
      - The `.pyc` was deleted, and all SoC stamps are up to date again (`gemm_build.py --dry-run`).
      - `gemmflow.ddr_tune` and `lane_delay.py` now import with `sys.dont_write_bytecode`.
      - Stamps are hashed at the end of a step, so the `int4_probe` build that was running records the clean hash.
      - **Suggestion:** make `tree_hash` skip `__pycache__`/`*.pyc`. That is a one-time hash migration (all
        existing stamps include today's cache files), so it is left for you.
      - The DSE smoke test with `"ddr_load_tune": true` (run-only campaign on `c0ddddde`, results removed):
        102 400 → 0 bad at bring-up, 2048³ full ok, row fields present.
- [x] **`int4_probe` done** (07:50–09:08, `results/int4_probe/`; untuned DDR; 200 MHz, tA, default preset).
      **The packed sub-byte tier T3 works end to end on the card:** 128 int4 lanes per 64-byte word, packed by the
      host, exact results.

      | Build | MACs | DSP | SoC LUT | SoC FF | BRAM tiles | WNS ns | 2048³ / 4096³ GOP/s (% of peak) | Verify |
      |---|---|---|---|---|---|---|---|---|
      | int4 PE 32×32 (512×1024) | 1024 | 39 | 82.1k | 81.7k | 300.5 | 0.002 | 375.8 / 378.6 (92 %) | ok |
      | uint4 PE 32×32 | 1024 | 39 | 82.1k | 81.7k | 300.5 | 0.043 | 375.8 / 378.6 | ok |
      | **int4 PE 32×64 (512×2048)** | 2048 | 39 | 116.9k | 103.9k | 414.5 | 0.015 | 751.0 / **756.7** (92 %) | ok, **full screen exact** |
      | uint4 PE 32×64 | 2048 | 39 | 116.8k | 103.9k | 414.5 | 0.043 | 751.0 / 756.7 | ok, full screen exact |

      - **756.7 GOP/s for int4: 6.8× the best float build and 2× int8 PE 32×32**, with 39 DSPs and 39 % of the
        LUTs. The 4-bit multiplies are tiny: Vivado needs 117k LUTs where HLS estimated 240k.
      - Full exact screens (2×2048³ each, 8.4 M entries) of both PE 32×64 builds: 0 bad, without DDR tuning.
      - The rough DSP-per-MAC estimate in `numeric.py` now counts 0 DSPs for ≤8-bit multiplies (the warning was
        wrong for int8/int4).
- [x] **`fixed_point` (fixed32.16, PE 32×8, 200 MHz, default preset): timing failed** (09:08–09:26).
      - WNS −0.076 ns (10 failing endpoints), so no runs. The failing path is the `cPipes` FIFO address fanout into
        256 SRL bits (95 % routing), a path class already near-critical in the int32 builds.
      - fixed32.16 needs 1064 DSPs against int32's 808: the 64-bit product before the shift by 16.
      - SoC: 79.3k LUT, 106.3k FF, 654.5 BRAM tiles. HLS estimate 4.209 ns, like int32.
      - Re-queued with `vivado.preset: aggressive` (which gave int32 at this array +0.044 ns), before `int4_wide`.
- [x] **`fixed_point` retry done (aggressive preset, 11:20–11:45): fixed32.16 PE 32×8 meets 200 MHz, exact.**

      | Format (PE 32×8, 200 MHz) | Preset | WNS ns | DSP | SoC LUT | SoC FF | BRAM tiles | 8192³ GOP/s (% of peak) | Verify |
      |---|---|---|---|---|---|---|---|---|
      | fixed16.8 (`dtype_same_array`) | default | — | 296 | — | — | — | 98.64 | ok |
      | int32 (`int32_margin`) | aggressive | 0.044 | 808 | — | — | — | 98.59 | ok, full screen exact |
      | **fixed32.16** | aggressive | **0.060** | **1064** | 79.2k | 106.4k | 654.5 | **98.59** (96.3 %) | ok, **full screen exact** |
      | fixed32.16 | default | −0.076 | 1064 | 79.3k | 106.3k | 654.5 | — | timing failed |

      - Same speed as int32 (the array, not the format, sets the rate), at 1.3× its DSPs: the full 64-bit product is
        built before the shift by 16 (AP_TRN) and the wrap to 32 bits.
      - Bit-exact against the host model (`numeric.hpp`: truncate each product, wrap each sum): 2048³ to 8192³
        sampled on every rep, plus the full screen (2 seeds × 2048³, 8.4 M entries, 0 bad,
        `misc/int32_verify/screen.sh`). DDR load tuning found 0 bad before, moved no tap.
      - As with int32, the aggressive preset is what closes 200 MHz at this array; the default preset misses by
        0.08 ns on the `cPipes` FIFO address fanout.
- [x] **Flow bug found and fixed (09:27): concurrent HLS runs in one directory.** `dtype_clock` is the first
      campaign whose points differ only in a Vivado setting (preset), so 225 MHz default and aggressive share an HLS
      hash and directory. The parallel HLS stage started both, and one died (225 MHz default: `vitis-run` exit 1,
      log cut off).
      - Fix: `HlsRun.run` takes an flock on `build/gemm/hls/<hash>.lock` and re-checks the stamp after acquiring
        it. `gemm_dse.py` schedules the first point of each HLS hash first; the rest wait and find it up to date.
      - The campaign was stopped (09:30), the two unstamped HLS dirs (225/250 MHz) removed, and the queue
        restarted: `dtype_clock --resume` → `fixed_point` (aggressive) → `int4_wide`. After the restart, one Vitis
        run per hash, duplicates waiting on the lock.
- [x] **`dtype_clock` done** (09:29–11:20, `results/dtype_clock/`): int8 PE 32×32, tile 512×1024, tA,
      × 200/225/250 MHz × default/aggressive preset. DDR load tuning was on; all three runnable builds had 0 bad entries
      before tuning, so no tap was moved.

      | Clock | Preset | WNS ns | TNS ns | SoC LUT | SoC FF | 4096³ / 8192³ GOP/s (% of peak) | Verify |
      |---|---|---|---|---|---|---|---|
      | 200 MHz | default | 0.038 | 0 | 130.1k | 119.0k | 378.2 / 379.4 (92.6 %) | ok |
      | 200 MHz | aggressive | 0.038 | 0 | 130.4k | 119.0k | 378.2 / 379.4 | ok |
      | **225 MHz** | default | **0.017** | 0 | 131.3k | 119.2k | 425.1 / **426.6** (92.6 %) | ok |
      | 225 MHz | aggressive | −0.130 | −3.5 | 131.5k | 119.3k | — | timing failed |
      | 250 MHz | default | −0.365 | −118 | 130.9k | 119.9k | — | timing failed |
      | 250 MHz | aggressive | −0.589 | −388 | 131.7k | 120.0k | — | timing failed |

      - **New int8 best: 426.6 GOP/s at 225 MHz**, exact (sampled check on every rep; tuning check 1024³ full).
        That is 3.8× the best float build (110.9 GFLOP/s), with 40 DSPs and 414.5 BRAM tiles for every point.
      - The aggressive preset does not help here (worse at 225 and 250 MHz). Place and route varies, so one seed per
        point says little at a margin of 0.1 ns.
      - At 225 and 250 MHz the limiting path is the HLS dataflow control, not the PEs. It starts at a PE's loop
        counter (`ProcessingElement_18_U0/trunc_ln_reg_427`, `ProcessingElement_25_U0/n0_fu_96`) and ends at the
        reset of the `ap_sync_reg_*_ap_ready` flops of every process in the dataflow region. 6 logic levels
        (CARRY4 ×2, LUT6), 81 % routing. With ~40 processes in one region (32 PEs plus the memory readers, converters and writer), that fanout grows with the array. Fixing
        it needs a kernel or HLS-directive change, not a flow setting. The HLS estimate (4.144 ns) does not see it.
- [x] **`int4_clock` done** (21:12–21:48, `results/int4_clock/dse_2.log`): int4 PE 32×64, tile 512×2048, 225 MHz
      (peak 921.6 GOP/s). Same 32-PE dataflow control as int8 PE 32×32, which met 225 MHz with the default preset.
      Both builds meet timing and verify (sampled) at 2048³ and 4096³ with DDR load tuning (0 bad before and after):

      | Preset | WNS / WHS ns | SoC LUT | FF | BRAM tiles | DSP | Build | 2048³ / 4096³ GOP/s |
      |---|---|---|---|---|---|---|---|
      | default | 0.037 / 0.021 | 117.5k | 104.0k | 414.5 | 39 | 17 min | 844.7 / **851.2** (92.4 %) |
      | explore | 0.023 / 0.017 | 117.7k | 104.1k | 414.5 | 39 | 17 min | 844.7 / 851.2 |

      - New int4 PE 32×64 best: 851.2 GOP/s, +12.5 % over 200 MHz (756.7), the clock ratio. The int8 PE 32×32 225 MHz
        build had already shown that 32 PEs close at 225 MHz.
- [x] `int4_wide` (started 11:45, stopped 15:45 (09:15, after `dtype_clock` and the fixed32.16 retry): int4 PE 32×128 and PE 64×64 (4096 MACs, peak 1638 GOP/s)
      and PE 48×64 (3072 MACs); at most 2 HLS jobs at once (RAM); DDR tuning on.
      - HLS done 13:02 (all ports 512 bits):

        | Array (tile) | MACs | HLS est. ns | DSP | BRAM18 | LUT (HLS) | Gate |
        |---|---|---|---|---|---|---|
        | PE 32×128 (512×4096) | 4096 | 4.209 | 30 | 2045 | 383k | **over budget** (2045 + 150 > 1910): not built |
        | PE 64×64 (1024×2048) | 4096 | 4.144 | 34 | 1142 | 446k | building |
        | PE 48×64 (768×2048) | 3072 | 4.144 | 252 | 902 | 345k | queued |

      - The gate is about right for PE 32×128. Across 24 built points the kernel uses 85–95 % of the HLS BRAM18
        estimate. The SoC outside the kernel takes 111 BRAM tiles (222 BRAM18; the gate assumes 150). So PE 32×128
        would need ~1950–2170 of 1910. HLS LUT estimates are 2–3× Vivado for int4 (PE 32×64: 240k → 117k), so the
        LUTs of the 4096-MAC arrays may still fit.
      - PE 48×64 gets 252 DSPs: the non-power-of-two PN brings index multiplies.
      - **PE 64×64 build (13:27): timing failed, WNS −0.215 ns** (TNS −4.9, 10 endpoints). SoC 199.1k LUT (67 %),
        154.9k FF, 654.5 BRAM tiles, 40 DSP; kernel 170.5k LUT (HLS said 446k). The path is the same as
        in `dtype_clock`: `ProcessingElement_38_U0/n0_fu_92_reg` → `ap_sync_reg_ProcessingElement_*_U0_ap_ready_reg/R`,
        6 levels, 85 % routing, now fanning out to 64 PEs.
- [x] **`int4_wide` PE 48×64 (tile 768×2048): wrong results, then a hung kernel. Cause: two tile rules the
      upstream kernel does not check** ([misc/pe_forwarding](../../misc/pe_forwarding/README.md)).
      - Build: WNS 0.000 ns (met), so it ran. The first exact check (1024³, DDR tuning) gave 983 555 of 1 048 576
        entries wrong. After that every kernel run hit the 60 s timeout. The tuning sweep would have spent ~5 h on
        timeouts, so `int4_wide` was stopped (15:45, EXIT=143). No run rows for PE 48×64.
      - Rule 1, **tile_m/PM ≥ PN:** the PEs forward the next A values in the slots `m1 = locationN … PN−1` of a loop
        that is tile_m/PM long. PE 48×64: 2048/64 = 32 < 48, so PEs 32–47 starve, the A stream falls out of step and
        the leftover values block the feeder. PE 64×64 at 1024×2048 (32 < 64) has the same fault.
      - Rule 2, **tile_m/PM and tile_n/PN powers of two:** the C write-back counters are
        `ap_uint<ConstLog2(x)>` (floor).
      - Both confirmed in g++ simulation (`misc/pe_forwarding/sim.py`: 10 small cases, wrong exactly when a rule
        is broken; PN 12 is fine when both hold). Every earlier board build met both rules.
      - Fix: both rules are errors in `config.py` `validate` (+3 cases in `test_rejected`; 37 tests pass). No existing campaign point
        other than the two `int4_wide` ones is affected.
- [x] **`int4_pe64` done** (15:46–21:12; round 2 below): PE 64×64 now with **tile 512×4096** (tile_n/PN 8, tile_m/PM 64 = PN,
      same per-PE C buffer as 1024×2048).
      - HLS (both clocks): est. 4.209 ns, 35 DSP, 1142 BRAM18 (as tile 1024×2048: the per-PE C buffer is the same),
        LUT 454k (HLS; Vivado was 2.6× lower for the 1024×2048 tile), ports 512 bits, under the resource gate.
        Builds in order: 200 MHz aggressive, 200 MHz explore, 180 MHz default.
      - **200 MHz aggressive (17:43): timing failed, WNS −0.051 ns** (TNS −0.56, 11 endpoints; the default preset
        at tile 1024×2048 missed by 0.215). Same path: `ProcessingElement_2_U0/n0_fu_98_reg` →
        `ap_sync_reg_ProcessingElement_*_U0_ap_ready_reg/R`, 6 levels, 84 % routing. SoC 199.6k LUT, 134.9k FF,
        654.5 BRAM tiles, 38 DSP; build 33 min.
      - **200 MHz explore (18:13): timing failed, WNS −0.336 ns** (TNS −20.4). Same path class
        (`ProcessingElement_57_U0/n0_fu_98_reg` → `ap_sync_reg_ProcessingElement_17_U0_ap_ready_reg/R`, 7 levels, 83 %
        routing). The three 64×64 builds at 200 MHz so far give −0.215 / −0.051 / −0.336 ns: placement noise on one
        control net. 180 MHz default builds next.
      - **180 MHz default (18:37): the kernel met timing (accel clock WNS +0.018 ns), but the PCIe 250 MHz user clock
        failed: WNS −0.447 ns**, 6 endpoints. The path runs from a LitePCIe 16×146-bit FIFO in block RAM
        (`storage_25_reg_2`, 1.97 ns of BRAM clock-to-out and LUTs) into the PCIe hard block's TX throttle
        (`tready_thrtl_reg`, `TRNTCFGGNT`). This is the known PCIe-path failure that a rebuild with another preset fixes.
        The design is at 67 % LUT, so the PCIe logic gets worse placement.
- [x] **`int4_pe64` round 2 done** (rerun 20:23–21:12, `dse_3.log`; the first try at 18:45 was stopped, see below):
      180 MHz aggressive and explore (same HLS run). **Both meet timing, PCIe clock included, and verify (sampled)
      at 2048³ and 4096³** with DDR load tuning (0 bad before and after). Peak 1474.56 GOP/s.

      | Preset | WNS / WHS ns | SoC LUT | FF | BRAM tiles | DSP | Build | 2048³ / 4096³ GOP/s |
      |---|---|---|---|---|---|---|---|
      | aggressive | 0.045 / 0.015 | 200.3k (67 %) | 134.8k | 652.5 | 38 | 24 min | 632.2 / **1281.0** (86.9 %) |
      | explore | 0.018 / 0.014 | 199.5k | 134.6k | 654.5 | 38 | 23 min | 632.2 / 1281.0 |

      - **New int4 best: 1281 GOP/s, 1.5× the PE 32×64 225 MHz build**, still with 38 DSPs (kernel 170.7k LUT,
        509 RAMB36 + 69 RAMB18).
      - 2048³ runs at 42.9 % because M = 2048 fills only half of a 4096-wide memory tile (the kernel always computes
        whole tiles, as in `dtype_size_sweep`). On the padded work that is 85.7 %.
      - 4096³ reaches 86.9 % of peak, below the ~92 % of the 32-PE builds. Not explained yet; 8192³ and a full
        exact screen are next.
      - 180 MHz default had failed only on the PCIe 250 MHz path (−0.447 ns); aggressive and explore close it.
- [x] **Both campaigns stopped (~18:58) by Claude Code's memory guard**, not by a failure in the flow. `int4_clock`'s HLS
      finished sooner than expected (both 225 MHz points: est. 4.144 ns, 34 DSP, 662 BRAM18, stamped), and its first
      Vivado build started next to `int4_pe64`'s 180 MHz aggressive build. Two Vivado runs of this size (~7 GB each)
      plus the rest left the machine critically low, and the session's background shells were stopped. The kernel OOM
      killer did not fire.
      - State: no tool processes left. The two interrupted SoC builds (`i4_pe32x64_..._225mhz_tA-9fd9e3a8`,
        `i4_pe64x64_..._180mhz_tA_aggressive-2b2028f3`) have no stamp and no bitstream, so a restart rebuilds them.
        All HLS runs are stamped.
      - Lesson: one Vivado build at a time on this 15 GB machine. A DSE campaign can move from HLS to Vivado at any
        point.
      - Restarted on the user's go (19:10), one after the other in one shell: `int4_pe64 --resume` (`dse_3.log`; 180 MHz
        aggressive, explore), then `int4_clock --resume` (225 MHz default, explore). Only one Vivado run at a time.
- [x] (superseded: the tile broke rule 1) Queued `int4_pe64` (13:35, after `int4_wide`, before `int4_clock`): PE 64×64 at 200 MHz with the aggressive
      and explore presets (one HLS run), and at 180 MHz default (peak 1475 GOP/s, 0.56 ns more period). It goes first:
      4096 MACs at any of these clocks beats int4 PE 32×64 at 225 MHz (921.6 GOP/s peak).
- [x] (stale queue notes, kept for the record) Queue after `int16_scale` (06:15): `int32_margin` → `int4_probe` →
      `fixed_point` → `dtype_clock`, all run; `dtype_size_sweep`, `int8_scale`, `int16_scale` done.
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
