# DenseShift: progress

Plan: [denseshift-plan.md](denseshift-plan.md). Branch `denseshift` from `1ad6b99`.

| Phase | What | State |
|---|---|---|
| 0 | Read paper + official code, plan | ✅ |
| A1 | PyTorch DenseShift layers + unit checks | ✅ |
| A2 | CIFAR-10 ResNet-20: float32 vs DenseShift 2/3/4-bit | ✅ (3 seeds; 400 epochs for float32, 3-bit, 4-bit) |
| B1 | `kernels/gemm_mixed/` (A/B/C types, shift MAC) + g++ simulation | ✅ |
| B2 | Flow: config fields, numeric formats, kernel dir, hashes, names, columns | ✅ |
| B3 | Host: codec, mixed reference, runner options, unit tests | ✅ |
| C1 | `ds_hls` HLS probe | ✅ |
| C2 | `ds_same_array` builds + card runs + exact screens | ✅ (sampled exact checks; full screens not run, see log) |
| C3 | `ds_scale` | ✅ |
| C4 | MNIST MLP end to end on the card | ✅ (uint8 and 7-bit activation variants) |
| C5 | Float activations (stretch) | ✅ PE 32×16 (PE 40×16 build stopped by the memory guard) |
| D | README / report | ✅ |

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
  float32 array (PE 32×8: 1320 DSP, 154.3k LUT) with fewer DSPs. **PE 48×16 failed: 303 086 LUTs needed, 298 600 on
  the device** (DRC UTLZ-1 before placement; an earlier note here that it had placed came from reading the wrong log).
  PE 40×16 (640 MACs, tile 320×1024, C buffer depth 512) queued as `ds_float2`.
- **Float activations on the card:** f32×ds3 PE 32×16: 1024³ 179.44, 4096³ 187.59, **8192³ 189.10 GOP/s (92.3 % of
  204.8), verified bit for bit** against k-order float sums. The best float32 build (multiply-add, PE 32×8, 225 MHz)
  reached 110.9 GFLOP/s: 1.7× the throughput with 1063 instead of 1320 DSPs.
- **CIFAR-10 seeds** (in progress): seed 1 gives float32 91.85, 2-bit 91.43 (seed 0: 90.84), 3-bit 91.47: the seed
  spread is up to 0.6 points, so single-seed differences below that mean little.
- **CIFAR-10 over three seeds** (200 epochs): float32 91.70 % (91.56–91.85), DenseShift 2-bit 91.06 % (90.84–91.43),
  3-bit 91.32 % (91.10–91.47), 4-bit 90.72 % (90.63–90.80, 2 seeds so far). 3-bit is 0.4 points below float on
  average; 4-bit is below 3-bit on every seed, unlike the paper's ImageNet ordering. Queued
  (`ml/denseshift/run_cifar_more.sh`): seeds 1–2 of the two ablations, and 3-bit and 4-bit with 400 epochs (8 binary
  variables per weight may need longer training; the paper's Table 10 shows the epoch effect).
- **ds_u8:** `u8xds3c32` with the `explore` preset: timing met (WNS 0.014); 8192³ **375.45 GOP/s**, exact (sampled).
- **MNIST with uint8 activations on the card** (`mlp_mnist.py fpga --run build/ml/mnist/ds3_h1024`, chain step right
  after ds_u8): layer 1 52.59 ms (325.6 GOP/s), layer 2 63.15 ms (333.7 GOP/s), **0 of 10 240 000 sums differ in each
  layer, test accuracy 98.47 % = CPU integer pipeline = PyTorch, predictions identical.**
- **C3 `ds_scale` HLS:** i8×ds3→i32 PE 48×32 t384×2048: DSP 255, BRAM18 1570, LUT 427.7k, 4.505 ns;
  i8×ds3→i16 PE 32×64 t512×2048: DSP 37, BRAM18 1110, LUT 404.8k, 4.374 ns; i8×ds3→i32 PE 32×32 at 250 MHz:
  DSP 39, BRAM18 1106, LUT 292.8k, 4.374 ns (at a 4 ns target). All under the gate; builds running.
- **CIFAR-10, all with three seeds** (200 epochs): float32 91.70 % (91.56–91.85); DenseShift 2-bit 91.06 %
  (90.84–91.43), 3-bit 91.32 % (91.10–91.47), 4-bit 90.89 % (90.63–91.25); 3-bit with Eq. 7 91.22 % (91.11–91.36),
  3-bit with Kaiming init 90.23 % (89.99–90.50). Eq. 8 vs Eq. 7: +0.1 point, within the seed spread here (paper,
  ImageNet: +0.3/+0.7). Low-variance vs Kaiming init: +1.1 points, on every seed. 400-epoch 3/4-bit runs going.
- **ds_scale, PE 48×32 int32 sums: timing failed, WNS −2.534 ns** on the accelerator clock, and also −1.267 on the
  125 MHz SoC clock and −1.868 / −0.007 on PCIe clocks. Utilisation: BRAM 868.5 of 955 tiles (91 %), LUT 227.6k
  (76 %). Worst path: a PE's A-buffer register to its shift lanes, 6.97 ns, 95 % routing, one LUT level. The C buffers
  fill the BRAM columns and spread the design over the device; with the SoC clock failing too, a lower accelerator
  clock alone would not close it. With int32 sums, ~1024 MACs is the practical array on this device.
- **CIFAR-10 with 400 epochs** (seed 0): DenseShift 3-bit 92.05 %, 4-bit 91.64 % (200 epochs: 91.32 / 90.89 mean).
  Both gain ~0.7 points; 4-bit stays below 3-bit, so slower training of its 8 binary variables per weight is not the
  whole reason. float32 with 400 epochs running for a fair comparison.
- **float32 with 400 epochs: 92.24 %** (seed 0). At 400 epochs: 3-bit −0.19, 4-bit −0.60 points; at 200 epochs
  (3-seed means): 3-bit −0.38, 2-bit −0.64, 4-bit −0.81. 3-bit DenseShift stays within ~0.4 points of float.
- **ds_scale, PE 32×64 int8×ds3→int16 (2048 MACs): timing met (WNS 0.025), LUT 190.3k, FF 255.5k, BRAM 638,
  DSP 40** (27 min). The multiply MAC would need 2048 DSPs here (device: 1920). Card run after the 250 MHz build.
- **ds_scale, PE 32×32 int32 at 250 MHz: timing failed, WNS −0.932 ns** on the accelerator clock. Worst path:
  `ProcessingElement_4` counter → `ap_sync_reg_ProcessingElement_26_U0_ap_ready` reset, 4.23 ns, 7 levels, 82 %
  routing: the HLS dataflow control fan-out that already limited the int4 arrays (notes2), not the shift MAC.
- **C3 done.** PE 32×64 int8×ds3→int16 on the card: 1024³ 358.93 (M = 1024 fills half of the 2048-wide tile),
  4096³ 739.64, **8192³ 750.91 GOP/s (91.7 % of 819.2), exact** (sums wrap at 16 bits and are checked so). 40 DSPs.
  With int32 sums the largest array that closes stays PE 32×32 (PE 48×32: congestion; 250 MHz: dataflow control).
- **Chain 2 stopped by Claude Code's memory guard** (~08:20, system critically low on memory while the session was
  idle). It was in the Vivado run of `f32xds3c32_pe40x16_t320x1024` (HLS: DSP 1505, LUT 407.2k, BRAM18 776; the largest
  float build so far, no other job running). No process left; the build has no bitstream. Not run: that build's card
  runs and the full exact screens (2 × 2048³, every entry) of the 7 DenseShift builds that met timing. Not restarted
  (standing rule: memory-reaped tasks are restarted only when the user asks). To redo: `build/ds_chain2.sh` minus its
  wait line, or the screens alone (no Vivado run apart from JTAG programming).
- **D done** (docs pass after chain 2):
  - Report: `tab:ds-cifar` now shows the 3-seed means with ranges and the 400-epoch runs; the text says what the
    seeds changed (2-bit and 4-bit looked equal with seed 0 alone; Eq. 7 vs Eq. 8 within the spread). New paragraph
    on verification (sampled 4098 entries per card run, MNIST every sum, full screens not run), the PE 40×16 float
    build as stopped, a DenseShift campaign table (`tab:ds-campaigns`), and an appendix table of every mixed build
    (`tab:ds-builds`, `data/ds_builds_table.csv`, the two builds without a report listed with their reason).
    Abstract (3-seed number, PE 32×64) and open points updated. 33 pages, 0 warnings, no overfull boxes.
  - `docs/report/tools/extract_data.py`: mixed builds go to `ds_builds_table.csv` instead of the integer/fixed-point
    table. The rerun also adds the six `ds_*` campaigns to `campaigns.csv` and moves a few DMA rate points by ≤ 0.004
    GB/s (the new runs' transfers are averaged in). `data/denseshift_cifar.csv` from `summarize.py --csv`.
  - READMEs: `ml/denseshift/README.md` (scripts, commands, results), `misc/gemm_mixed_sim/README.md`,
    `misc/ds_mac/README.md`; top-level README (4-bit < 3-bit, the int32 array limit, verification state) and
    `docs/gemm.md` (best builds, README link).
  - Still open (needs the user's go): full exact screens of the 7 DenseShift builds that met timing, and the PE 40×16
    float build (`build/ds_chain2.sh`; with fewer Vivado threads to lower its memory peak).

### 2026-11-25 (afternoon): float × float baseline
- **Request:** use float32 × float32 GEMM as the baseline for every DenseShift speedup (so far the DenseShift MAC was
  compared with int8 × int8 at the same array). Conv2d: DenseShift convolutions exist only in the PyTorch training
  (ResNet-20, with its float32 baseline for accuracy); no convolution has run on the card yet.
- The float × float baseline is the best float32 build that fits the device: PE 32×8, tile 1024×512, transposed A
  (1320 DSP; a float MAC is 3 + 2 DSP48, so PE 32×16 = 2560 would not fit). Two clocks: 200 MHz (`a88777ec`, same
  clock as every DenseShift build) and 225 MHz `aggressive` (`db5adf1c`, the best float32 result).
- `gemm_runner`: `--a-file/--b-file/--c-out` now also take float32 files (float A and B; float C), so real float
  layers run on float32 and float32 × dshift builds. `make test` passes; host tools rebuilt for the 7 builds below.
- `mlp_mnist.py --act-bits 0`: float activations (input pixel / 255, h = relu(acc·a + b), no quantization). The CPU
  reference sums in the kernel's order (c = c + a·w, k = 0, 1, …, float32 without FMA), so card sums must be equal.
  `fpga` now records kernel and end-to-end time per layer (median of `--reps`, default 3) for speedups.
  - float weights × float activations (`fp32_h1024_af`): **98.60 %**, float pipeline 98.60 % (the float × float
    network for the baseline run).
  - 3-bit DenseShift × float activations (`ds3_h1024_af`): **98.40 %**, float pipeline 98.40 %.
- Next: one card session, build after build (`build/ds_baseline.sh`, results `results/ds_baseline/`): random GEMMs
  1024³/4096³/8192³ on both float32 builds and the DenseShift builds, and the MNIST layers on each matching build.
- **Card session done** (`build/ds_baseline.sh`, 10:32–10:42, `results/ds_baseline/`: 42 rows, 0 wrong entries;
  every build loaded, DDR taps tuned under load, 0 bad words after tuning each time). GOP/s at 8192³ (median of 2)
  and speedup against float32 × float32 at 200 MHz at the same size (`docs/report/data/ds_speedup.csv`):
  | Build (all PE arrays transposed A) | MACs | DSP | 8192³ | ×float (1024³ / 4096³ / 8192³) | ×float 225 MHz |
  |---|---|---|---|---|---|
  | float32 × float32, PE 32×8, 200 MHz | 256 | 1320 | 98.59 GFLOP/s | 1 | 0.89 |
  | float32 × float32, PE 32×8, 225 MHz `aggressive` | 256 | 1320 | 110.86 | 1.12 | 1 |
  | float32 × dshift3 → float32, PE 32×16 | 512 | 1063 | 189.10 | 1.87 / 1.91 / **1.92** | 1.71 |
  | uint8 × dshift3 → int32, PE 32×32 (`explore`) | 1024 | 40 | 375.45 | 3.54 / 3.77 / **3.81** | 3.39 |
  | int8 × dshift3 → int32, PE 32×32 | 1024 | 40 | 375.45 | 3.54 / 3.77 / **3.81** | 3.39 |
  | int8 × dshift3 → int16, PE 32×64 | 2048 | 40 | 750.91 | 3.75 / 7.53 / **7.62** | 6.77 |
  | int8 × int8 → int32 multiply, PE 32×32 (reference) | 1024 | 1064 | 375.45 | 3.81 | 3.39 |
  The speedup is the array size: a float32 MAC costs ~5 DSP48 (3 multiply + 2 add), so PE 32×8 is the largest float
  × float array (PE 32×16 would need ~2600 of 1920 DSPs). DenseShift with float activations replaces the multiply by
  an exponent add (~2 DSP per MAC: twice the MACs), and with integer activations it needs no DSP at all. At 1024³ the
  PE 32×64 build fills only half of its 2048-wide tile (M = 1024), hence 3.75×.
- **MNIST on the card against float × float** (10 000 test images, both hidden layers; kernel = sum of the two
  layers' kernel times, end to end = A and B to the card + kernel + C back; median of 3; every sum equal to the CPU):
  | Network (activations × weights) | Build | Accuracy | Kernel ms | End to end ms | Speedup kernel / e2e |
  |---|---|---|---|---|---|
  | float32 × float32 (`fp32_h1024_af`) | f32 PE 32×8 200 MHz | 98.60 % | 173.1 + 223.9 = 396.9 | 559.2 | 1 / 1 |
  | float32 × float32 | f32 PE 32×8 225 MHz | 98.60 % | 154.0 + 199.1 = 353.1 | 515.1 | 1.12 / 1.09 |
  | float32 × dshift3 (`ds3_h1024_af`) | f32xds3 PE 32×16 | 98.40 % | 93.1 + 119.5 = 212.7 | 367.7 | **1.87 / 1.52** |
  | uint8 × dshift3 (`ds3_h1024`) | u8xds3c32 PE 32×32 | 98.47 % | 52.6 + 63.2 = 115.7 | 204.9 | **3.43 / 2.73** |
  | 7-bit × dshift3 (`ds3_h1024_a7`) | i8xds3c32 PE 32×32 | 98.48 % | 115.7 | 204.9 | 3.43 / 2.73 |
  Float sums equal the CPU's kernel-order float32 sums bit for bit (2 × 10.24 M per run), integer sums exactly. Per
  layer, uint8 × dshift3 is 3.29× (layer 1) and 3.55× (layer 2) faster in the kernel: layer 2 (K = M = 1024) matches
  the 1024³ GEMM (3.54×), where the fixed cost per tile and k step weighs more on the larger array than at 8192³
  (3.81×); layer 1 also pads K from 784 to 832 for the integer build's 64-element words (−6 %). End to end is lower
  again because the transfers shrink less: C goes back as 4-byte values in every format (2 × 41 MB), and
  float A is 4 bytes per element against 1 for uint8 codes. `summarize.py` prints the table
  (`--mnist-csv docs/report/data/denseshift_mnist_card.csv`).
- **Conv2d:** DenseShift convolutions are trained (ResNet-20, all 3×3 and 1×1 convolutions except the first) with
  the float32 network as the accuracy baseline (91.70 % float32 against 91.32 % 3-bit, 3 seeds). No convolution ran
  on the card, in either format: that needs im2col on the host, and ResNet-20's layers have only 16–64 output
  channels (GEMM M) against a tile width of ≥ 512 (tile_m ≥ PN·PM by the forwarding rule), so they would use
  3–12 % of either array. Left for the user to decide (asked).
- **Docs for the float × float baseline:** report subsection "Speedup against float32 × float32"
  (`sec:ds:speedup`: `tab:ds-speedup` GEMMs, `tab:ds-mnist` MNIST layers, the conv2d state), `ds_baseline` in the
  campaign table, abstract / introduction / conclusion now state speedups against float32 × float32 (3.8× int8
  activations, 7.6× int16 sums, 1.9× float activations, MNIST 3.4×). 34 pages, 0 warnings. README (baseline row and
  speedup column, MNIST table), `ml/denseshift/README.md`, `docs/gemm.md` updated.
