# DenseShift: progress

Plan: [denseshift-plan.md](denseshift-plan.md). Branch `denseshift` from `1ad6b99`.

| Phase | What | State |
|---|---|---|
| 0 | Read paper + official code, plan | ✅ |
| A1 | PyTorch DenseShift layers + unit checks | ✅ |
| A2 | CIFAR-10 ResNet-20: float32 vs DenseShift 2/3/4-bit | 🟡 training |
| B1 | `kernels/gemm_mixed/` (A/B/C types, shift MAC) + g++ simulation | ✅ |
| B2 | Flow: config fields, numeric formats, kernel dir, hashes, names, columns | ✅ |
| B3 | Host: codec, mixed reference, runner options, unit tests | ⬜ |
| C1 | `ds_hls` HLS probe | ⬜ |
| C2 | `ds_same_array` builds + card runs + exact screens | ⬜ |
| C3 | `ds_scale` | ⬜ |
| C4 | MNIST MLP end to end on the card | ⬜ |
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
