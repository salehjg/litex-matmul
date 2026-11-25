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
| C1 | `ds_hls` HLS probe | 🟡 running |
| C2 | `ds_same_array` builds + card runs + exact screens | ⬜ |
| C3 | `ds_scale` | ⬜ |
| C4 | MNIST MLP end to end on the card | 🟡 trained, waiting for a build |
| C5 | Float activations (stretch) | ⬜ |
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
