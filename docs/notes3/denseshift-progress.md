# DenseShift: progress

Plan: [denseshift-plan.md](denseshift-plan.md). Branch `denseshift` from `1ad6b99`.

| Phase | What | State |
|---|---|---|
| 0 | Read paper + official code, plan | ✅ |
| A1 | PyTorch DenseShift layers + unit checks | ✅ |
| A2 | CIFAR-10 ResNet-20: float32 vs DenseShift 2/3/4-bit | 🟡 training |
| B1 | `kernels/gemm_mixed/` (A/B/C types, shift MAC) + g++ simulation | ✅ |
| B2 | Flow: config fields, numeric formats, kernel dir, hashes, names, columns | ✅ |
| B3 | Host: codec, mixed reference, runner options, unit tests | ✅ |
| C1 | `ds_hls` HLS probe | ✅ |
| C2 | `ds_same_array` builds + card runs + exact screens | ✅ (u8 build: PCIe path −6 ps, rebuild queued) |
| C3 | `ds_scale` | ⬜ |
| C4 | MNIST MLP end to end on the card | ✅ (7-bit activations on the int8 build) |
| C5 | Float activations (stretch) | 🟡 building |
| D | README / report | ⬜ |

## Log

### 2026-11-25
- Branch `denseshift` created from `1ad6b99`. Paper read (13 pages). Official demo code read
  (`S3-Training/dsconv2d.py`, `resnet.py`, `main.py`): forward `w_sign · 2^S`, backward through
  `w_sign · sqrt(S + 1)` (Eq. 8); `H(x) = 1 for x > 0`, sign(0) = +1, identity backward; T + 1 parameters per weight,
  all N(0, 1e-3); first conv and final FC float; no weight scale (BatchNorm); SGD lr 0.1, momentum 0.9, wd 1e-4,
  cosine, 200 epochs.
- PyTorch: system Python 3.14 has torch 2.15 + CUDA (RTX 5060 Ti, 16 GB) and numpy 2.5; no torchvision, no sklearn.
  The `.venv` is built from the same Python without system site packages; it is left alone: ML scripts run with
  `/usr/bin/python3` and exchange files with the flow.
- **A1 done.** `ml/denseshift/denseshift.py`: `DenseShiftConv2d` / `DenseShiftLinear` for any n >= 2 (T = 2^(n-1) - 1
  exponent parameters per weight), Eq. 8 backward (Eq. 7 with `rescale=False`), low-variance or Kaiming init,
  `export_codes()` (code = sign << (n-1) | S). Self-test passes: only {±2^e} values, every one of the 2^n codes
  reached, decode(export) == forward weight, and the 3-bit layer equals a transcription of the official
  `DenseShiftConv2d3bit` in values and gradients.
- `ml/denseshift/data.py`: CIFAR-10 binary release (no pickle; md5 `c32a1d4ab5d03f1284b67883e8d87530`) and MNIST idx
  files into `build/ml/data/`.
- `ml/denseshift/train_cifar.py`: ResNet-20 (first conv and FC float, as the official code), data and augmentation on
  the GPU, bf16 autocast. 2-epoch timing: float32 2.4 s/epoch, 3-bit DenseShift 3.5 s/epoch (1.08 M trainable
  parameters against 0.27 M: T + 1 per weight). The decoded copy (exported codes as ordinary conv weights) scores the
  same as the DenseShift model (0.5644 both). (`channels_last` dropped: it cannot convert the 5-D exponent tensors.)
- **A2 started** (background, `ml/denseshift/run_cifar.sh`, log `build/ml/run_cifar.log`): float32, DenseShift 2/3/4,
  3-bit with Eq. 7, 3-bit with Kaiming init, 200 epochs each.
- **B1 done.** `kernels/gemm_mixed/` (first commit `b377caa` = verbatim copy of `kernels/gemm`; changes in its
  ORIGIN.md): `DataA_t/DataB_t/DataC_t`, own C memory width, `Mac.h` (multiply or DenseShift), `ConvertWidthC` that
  gathers, passes or splits C packs. `misc/gemm_mixed_sim/sim.py`: **all 12 cases exact** (int8 x dshift2/3/4 -> int32,
  uint8, int16 A, int4 A -> int16 C, int8 x int8 -> int32 multiply, int16 C with wrap-around, non-transposed A; C pack
  gather 2, pass, split 2 and split 4; whole and partial tiles). The split-4 case (int32 C, PM 64, a 2048-bit pack)
  first failed in simulation (`ap_uint<2048>` above the default `AP_INT_MAX_W` 1024): the flow now compiles mixed
  configs with `-DAP_INT_MAX_W=4096` and `Config.h.in` stops with an error without it.
- **B2 done.** `flow/gemmflow`: `kernel.b_type` / `b_bits` (late fields, hashed only when set), `accum_bits` wider than
  the element = mixed. `numeric.py`: `resolve_pow2` (spec `dshift<n>`, 3-bit codes in 4-bit lanes),
  `MixedFormat` (spec `int8xdshift3-int32`, short `i8xds3c32`, columns, `runner_args()`), `parse_mixed_spec`.
  `config.py`: `is_mixed()`, `kernel_dir()`, `operands()`, per-operand bus widths and size multiples (M: lcm of the B
  and C words), C rule (PM and C words per word divide one another), mixed `Config.h` values, HLS hash over the tree
  actually used. `hls.py`, `steps.py` (SimStep runs the mixed testbench), `runner.py` (`runner_args()`). 41 flow tests
  pass; the golden build ids are unchanged.
- **B3 done.** `sw/gemm_host/src/numeric.hpp`: `Kind::Pow2` (`dshift2/3/4`, 3-bit codes in 4-bit lanes, `value()`,
  `pow2_code()`), packing by storage bits, `ref_entry_mixed()` (sum of value(a)·value(b) wrapped to C), `mixed_spec()`.
  `gemm_runner`: `--b-dtype` / `--c-dtype` (A int/uint ≤ 16 bits, B int/uint ≤ 16 bits or dshift, C int16/int32;
  `--value-range` then applies to A only), `--a-file` / `--b-file` (int64 codes) and `--c-out`; JSON rows carry the
  combined spec (`int8xdshift3-int32`) plus `a_dtype`, `b_dtype`, `c_dtype`. `make test`: new DenseShift cases
  (values, wrap at int16, packing with clear high lane bits) and a cross-check of the reference against `ap_int`
  arithmetic written as in `Mac.h` (int8/uint8/int4 A, dshift2/3/4, int16/int32 C): 0 failures. The runner builds
  against an existing SoC build; it needs a mixed build on the card for a real run.
- **C4 model trained.** `ml/denseshift/mlp_mnist.py train --bits 3`: 784 → DenseShift 1024 → BN → ReLU → uint8 →
  DenseShift 1024 → BN → ReLU → uint8 → FC 10 (float). Inputs are the raw pixel codes, so both GEMMs are
  uint8 × dshift3 → int32 (the `u8xds3c32` kernel). QAT with running-max activation scales and a straight-through
  round, SGD 0.1 cosine, 30 epochs, 46 s on the GPU. **Test accuracy 98.47 %, and the integer pipeline (exact GEMMs
  via float64, BN folded into per-channel a, b, requantization clip(rint((acc·a + b)/s), 0, 255)) also 98.47 %.**
  Sums reach |acc| ≤ 255·8·784 ≈ 1.6 M in layer 1: int32 C needed (int16 would wrap). `mlp_mnist.py fpga` runs both
  GEMMs through the build's `gemm_runner` (`--a-file/--b-file/--c-out`), compares every sum with the CPU and
  evaluates the pipeline on the card's sums.
- **C1 started** (`configs/dse/ds_hls.json`: 9 points, sim + hls). All 9 sim points exact. First HLS point,
  `i8xds3c32` PE 32×32 t512×1024: 39 DSP (no DSP in the shift MAC), LUT 331k / FF 174k (HLS estimates), 4.374 ns at
  a 5 ns target, **BRAM18 2002**, over the resource gate (2002 + 150 > 1910). The C buffer is 57 of them per PE
  (1024 bits × 512, counted in 18-bit slices). Checked against Vivado: in the int8 PE 32×32 build of kernels/gemm each
  PE's 256-bit × 512 C buffer took 7 RAMB36 + 1 RAMB18, i.e. 36-bit ports: HLS's default `ram_2p` lands in
  true-dual-port blocks and leaves half of each BRAM36 empty at depth 512. The buffer needs only one write and one
  read port, so `kernels/gemm_mixed/src/Compute.cpp` now binds it as `ram_s2p` (simple dual port: 72-bit BRAM36 at
  depth ≤ 512). Campaign restarted with the new tree (and with 2 HLS jobs instead of 3: three runs at 3.5 GB each plus
  CIFAR training left 2 GB free). Lesson: `pkill -f <pattern>` in a tool shell also kills that shell when the pattern
  is in its own command line; use `pgrep -f "[g]emm_dse"` or PIDs.
- **C buffer fix confirmed by HLS:** with `ram_s2p` the PE 32×32 int32-C points need ~1105 BRAM18 (was 2002), under
  the gate. (`--resume` skips by build id and stage, not by HLS hash: the first point still shows the old tree;
  rerun without `--resume` at the end.) Editing any file in `kernels/gemm_mixed/` (also ORIGIN.md) changes the HLS hash
  of every mixed config: doc edits there wait until no campaign runs.
  | PE 32×32, int8 A, int32 C, HLS | DSP | LUT | FF | BRAM18 | est. ns |
  |---|---|---|---|---|---|
  | int8 × int8 (multiply) | 1064 | 195.8k | 189.3k | 1110 | 4.374 |
  | int8 × dshift2 | 39 | 326.6k | 166.2k | 1104 | 4.374 |
  | int8 × dshift4 | 39 | 339.8k | 178.8k | 1106 | 4.374 |
  With a 32-bit sum, HLS puts multiply + add into a DSP48 per MAC (the single-type int8 kernel, whose product was
  cut to 8 bits, had its multipliers in LUTs). The DenseShift MAC needs no DSP and costs LUTs instead: on this FPGA
  the trade is DSPs for LUTs, not a smaller design.
- **MNIST MLP, other widths** (`mlp_mnist.py train --bits 0/2/4`, same network, uint8 activations, 30 epochs):
  float32 weights 98.65 %, DenseShift 2-bit 98.46 %, 3-bit 98.47 %, 4-bit 98.37 % (integer pipeline = PyTorch for
  all three).
- **C1 done** (`results/ds_hls/`; HLS estimates, 200 MHz target, all ports 512 bits):
  | Point | DSP | LUT | FF | BRAM18 | est. ns |
  |---|---|---|---|---|---|
  | i8×i8→i32 PE 32×32 (multiply) | 1064 | 195.8k | 189.3k | 1110 | 4.374 |
  | i8×ds2→i32 PE 32×32 | 39 | 326.6k | 166.2k | 1104 | 4.374 |
  | i8×ds3→i32 PE 32×32 (MAC A, ram_s2p) | 39 | 331.6k | 173.7k | 1106 | 4.374 |
  | i8×ds4→i32 PE 32×32 | 39 | 339.8k | 178.8k | 1106 | 4.374 |
  | u8×ds3→i32 PE 32×32 | 39 | 331.6k | 173.7k | 1106 | 4.374 |
  | i8×ds3→i16 PE 32×32 | 39 | 245.2k | 123.5k | 658 | 4.209 |
  | i8×ds3→i32 PE 32×16 | 39 | 203.7k | 114.1k | 1104 | 4.374 |
  | i8×ds3→i16 PE 32×64 (t512×2048) | 37 | 421.1k | 195.8k | 1110 | 4.374 |
  | i8×ds3→i32 PE 32×64 (t512×2048) | 37 | 585.0k | 295.2k | **2006** (over the gate) | 4.505 |
  PE 32×16 at t512×1024 needs as much BRAM as PE 32×32: half the width, twice the depth (1024 > 512: no 72-bit
  packing). PE 32×64 with int32 sums does not fit (BRAM, and LUTs very likely); with int16 sums it fits the BRAM.
- **DenseShift MAC form** (`misc/ds_mac/`, int8 × dshift3 → int32, PE 32×32, HLS): A `neg ? acc − x : acc + x`
  331.6k LUT / 173.7k FF; **B `acc + (neg ? −x : x)` (x 12 bits wide) 290.6k / 173.7k**; C `acc + (x ^ neg) + neg`
  (carry-in) 300.9k / 167.6k. B kept (`Mac.h`). Whether Vivado folds C's carry-in into one carry chain (and beats B)
  was not built: open point. All 24 simulation sizes still exact.
- **C2 started** (`configs/dse/ds_same_array.json`, PE 32×32 t512×1024 200 MHz tA; i8×i8→i32, i8×ds3→i32,
  u8×ds3→i32). First build **i8×i8→i32: timing met (WNS 0.049), LUT 167.5k, FF 259.4k, BRAM 638.5 tiles, DSP 1064**
  (kernel: LUT 138.9k, RAMB36 509 + RAMB18 37, DSP 1064; 26 min). Each PE's C buffer (1024 bits × 512) is 14 RAMB36
  + 1 RAMB18: the `ram_s2p` binding gives the 72-bit packing (true dual port would need ~28.5 per PE, 912 for the
  array, more than the device has with the rest). HLS had estimated LUT 195.8k for this point.
- Float activations × DenseShift prepared (paper Sec. 3.1.1): flow format `float32xdshift<n>-float32`, runner check
  bit for bit against k-order float sums; kernel part (`Mac.h` branch: add e to the exponent field, flip the sign,
  zero/denormal → signed zero; testbench float path) simulated from a scratch copy of the tree: 2/3/4-bit, C pass /
  split / gather, whole and partial tiles all exact. It goes into `kernels/gemm_mixed/` after the running builds (a
  tree change would make them rerun HLS). (First try: every entry wrong because the testbench decoded B only under
  `MM_MAC_DENSESHIFT`; the MAC itself was right.)
- **A2 seed 0 done** (`build/ml/cifar/`, 200 epochs, ResNet-20, first conv and FC float):
  | Weights | Test acc. | vs float32 | Exponent share (|w| = 1, 2, 4, …) |
  |---|---|---|---|
  | float32 | 91.68 % | | |
  | DenseShift 2-bit | 90.84 % | −0.84 | 0.49 / 0.51 |
  | DenseShift 3-bit | 91.40 % | −0.28 | 0.52 / 0.14 / 0.12 / 0.21 |
  | DenseShift 4-bit | 90.80 % | −0.88 | 0.33 / 0.12 / 0.11 / 0.10 / 0.10 / 0.09 / 0.07 / 0.07 |
  | 3-bit, Eq. 7 (no rescale) | 91.36 % | −0.32 | 0.76 / 0.18 / 0.03 / 0.03 |
  | 3-bit, Kaiming init | 90.50 % | −1.18 | 0.45 / 0.20 / 0.15 / 0.20 |
  The decoded copies score exactly the same in every run. Paper (ImageNet ResNet-18): float 69.6, 2-bit 68.90, 3-bit
  70.62, 4-bit 70.94; Eq. 8 +0.3/+0.7. Here 4-bit < 3-bit and Eq. 7 ≈ Eq. 8 with one seed each: seeds 1 and 2 of
  float32 and 2/3/4-bit are running (`ml/denseshift/run_cifar_seeds.sh`) before drawing conclusions. Eq. 7 piles the
  exponents up at 0 (76 % of weights at ±1), as Eq. 8 is meant to prevent; low-variance init beats Kaiming by 0.9
  points even when training from scratch.
- **Second build, i8×ds3→i32 PE 32×32: timing met (WNS 0.015), LUT 161.6k, FF 240.2k, BRAM 636.5, DSP 40**
  (kernel LUT 132.9k, FF 211.1k, RAMB36 507 + RAMB18 37; 24 min). Against the multiply build at the same array:
  **−1024 DSP, −5.9k LUT, −19.3k FF**, BRAM equal. Per PE (synthesis): 3433 LUT / 4174 FF / 0 DSP against
  4026 / 4742 / 32. So in Vivado the shift MAC is cheaper than the multiply MAC in every resource; the HLS estimates
  (+131k LUT for DenseShift) pointed the wrong way. Consequence: with int32 sums, the C buffers (BRAM, 14.5 RAMB36 per
  PE at PM 32) limit the array for both MACs (~PE 56×32 fits the BRAM; the multiply MAC also fits the DSPs there),
  so DenseShift's gain on this FPGA is freed DSPs and fewer LUT/FF, not a larger int32 array. With int16 sums the
  multiply MAC is DSP-bound (≤ ~1880 MACs) while DenseShift PE 32×64 (2048 MACs) fits BRAM: the C3 candidate.
- **C2 on the card** (`results/ds_same_array/`, DDR taps tuned under load: 4088 bad words before, 0 after):
  | Build (PE 32×32 t512×1024 200 MHz) | 1024³ | 4096³ | 8192³ | verify |
  |---|---|---|---|---|
  | int8 × int8 → int32 (1064 DSP) | 339.35 | 369.80 | **375.45 GOP/s** (91.7 %) | exact (sampled) |
  | int8 × dshift3 → int32 (40 DSP) | 339.33 | 369.80 | **375.45 GOP/s** (91.7 %) | exact (sampled) |
  Same cycles, as expected for the same array and schedule (the int8 kernels/gemm PE 32×32 build: 379.4).
  `u8xds3c32` missed timing by 6 ps, on the PCIe 250 MHz user clock (`clkout2`), not on the accelerator clock
  (0.000): the known flaky path. Rebuild with the `explore` preset queued.
- **C4 done: MNIST MLP on the card.** To run before the uint8 rebuild, `mlp_mnist.py --act-bits 7` trains the same
  network with activation codes 0..127 (inputs: pixel >> 1), which fit the signed int8 build: PyTorch 98.48 %,
  integer pipeline 98.48 %. `mlp_mnist.py fpga` on `i8xds3c32_pe32x32` (no new bring-up, board lock held):
  layer 1 10048×832×1024 in 52.59 ms (325.6 GOP/s), layer 2 10048×1024×1024 in 63.15 ms (333.7 GOP/s),
  **0 of 10 240 000 sums differ from the CPU in each layer; test accuracy on the card 98.48 %, predictions identical
  to the CPU integer pipeline.** (Result: `build/ml/mnist/ds3_h1024_a7/fpga_<build>.json`.)
- **Float activations in the kernel** (`e63e263`): `Mac.h` `MM_MAC_DENSESHIFT_FLOAT` and the testbench float path
  copied in after `ds_same_array`; all 32 simulation sizes exact (16 cases). The three earlier mixed builds were made
  from the previous tree, so their HLS stamps now look stale (rerun cards with `gemm_run.py`, not the DSE build stage).
- **C5 `ds_float`** (chain `build/ds_chain.sh`: ds_float → ds_u8 + MNIST uint8 → ds_scale). HLS: PE 32×16 DSP 1064,
  LUT 326.7k, BRAM18 688, 4.209 ns; PE 48×16 DSP 1793, LUT 485.3k, BRAM18 896, 4.374 ns. **Build PE 32×16
  t512×512: timing met (WNS 0.025), LUT 202.5k, FF 319.0k, BRAM 427.5, DSP 1063** (32 min): twice the MACs of the best
  float32 array (PE 32×8: 1320 DSP, 154.3k LUT) with fewer DSPs. PE 48×16 placed (the LUTs fit despite the HLS
  estimate).
- **CIFAR-10 seeds** (in progress): seed 1 gives float32 91.85, 2-bit 91.43 (seed 0: 90.84), 3-bit 91.47: the seed
  spread is up to 0.6 points, so single-seed differences below that mean little.
