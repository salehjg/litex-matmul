# DenseShift on the GEMM accelerator: plan

Branch `denseshift` (from `1ad6b99`). Paper: Li et al., *DenseShift: Towards Accurate and Efficient Low-Bit
Power-of-Two Quantization*, ICCV 2023, arXiv:2208.09708 (`docs/denseshift.pdf`, not committed). Official demo code:
`github.com/xinlinli1990/noah-research/tree/master/S3-Training` (`dsconv2d.py`, BSD-3), read for details the paper
leaves open. Progress: [denseshift-progress.md](denseshift-progress.md).

## 1. What the paper proposes

| Idea | Paper | What it means here |
|---|---|---|
| Zero-free shift weights | Sec. 3.1 | An n-bit weight is `±2^p`, sign bit + (n−1) exponent bits, no zero: 2-bit {±1, ±2}, 3-bit {±1, ±2, ±4, ±8}, 4-bit {±1 … ±128}. All 2^n codes are used. |
| MAC without a zero branch | Fig. 3b, Sec. 3.1.2 | Quantized activations: `acc += s ? −(x << p) : (x << p)`. No multiplier, no `w == 0` test. |
| Float activations | Sec. 3.1.1, Eq. 2 | `x · ±2^p` = add p to the exponent field and flip the sign bit of x (an integer add), then a float add. |
| Sign-scale decomposition | Sec. 3.2, Eq. 3–6 | Training: w = (2·H(w_sign) − 1) · 2^S, S_0 = 0, S_t = H(w_t)·(S_{t−1} + 1), t = 1…T, T = 2^(n−1) − 1. Each weight has T + 1 float parameters. |
| Gradient | Eq. 7–8 | STE through H (identity backward). The forward uses 2^S; the backward uses sign·√(S+1) (local learning-rate rescale, Eq. 8). |
| Low-variance init | Sec. 3.3 | All T + 1 parameters drawn from N(0, 10^−3²). |
| Network | Sec. 4.1 | All layers but the first conv are quantized. Official code: first conv and final FC stay float; BatchNorm after every conv absorbs the missing weight scale (no learned scale, no exponent bias). SGD 0.1, momentum 0.9, wd 1e-4, cosine schedule. |

## 2. What this repo implements

### Part A: training (PyTorch)
- PyTorch comes from the system Python (`/usr/bin/python3`, torch 2.15, CUDA on the RTX 5060 Ti). The `.venv` (no
  system site packages) stays as it is: the ML scripts need only torch and numpy and talk to the flow through files
  (`.npy`, raw arrays). No torchvision: datasets are downloaded and decoded by our own code.
- `ml/denseshift/denseshift.py`: `DenseShiftConv2d` / `DenseShiftLinear` for any n ≥ 2 (same forward/backward as the
  official 3-bit layer, generalised to T binary variables), options for Eq. 7 vs Eq. 8 and Kaiming vs low-variance
  init (ablations), and `export_codes()` that returns the hardware codes (sign bit, exponent).
- `ml/denseshift/train_cifar.py`: ResNet-20 on CIFAR-10, float32 vs DenseShift 2/3/4-bit: a small-scale check of the
  paper's claim (Table 1 is ImageNet ResNet-18, out of reach here).
- `ml/denseshift/mlp_mnist.py`: an MLP whose hidden layers are DenseShift GEMMs with int8 activations, used for the
  end-to-end run on the card (Part C).

### Part B: the kernel
The gemm_hls kernel has one `Data_t` for A, B, C and the sum, and only a multiply. DenseShift needs separate types:
A = activations (int8/uint8/int4), B = DenseShift codes (2/3/4 bits), C = a wider sum (int16/int32), and a shift
MAC. That is a kernel change. To keep every existing build id and HLS hash, `kernels/gemm/` stays untouched and the
change goes into a copy, **`kernels/gemm_mixed/`**:
- `DataA_t`, `DataB_t`, `DataC_t`; B and C get their own memory widths (B 4-bit codes: 128 per 512-bit word; C int32:
  16 per word). cBuffer, C pipes and write-back use `DataC_t`.
- The MAC is a functor chosen in `Config.h`: `MacMultiply` (C += A·B in C width; gives the int8×int8→int32 baseline
  for comparison) or `MacDenseShift` (C += ±(A << e)).
- 3-bit codes do not divide 512, so they are stored in 4-bit lanes (exponents 0…3 only).

Flow (`flow/gemmflow/`):
- New optional kernel fields `b_type` / `b_bits` (B format) and `accum_bits` now allowed to differ from `data_bits` (C
  format). They are hashed only when set, like the dtype fields, so old ids stay.
- `data_type: "pow2"` (with `b_type`) = DenseShift codes, spec `dshift<n>` (`dshift3`). Plain one-type configs keep
  using `kernels/gemm/`; anything with `b_type` or a wider `accum_bits` uses `kernels/gemm_mixed/` (the HLS hash
  covers that tree).
- Result columns: `a_dtype`, `b_dtype`, `c_dtype`, `acc_bits`, `pow2_exp_bits`, `pow2_dense` (already reserved in
  notes2) are filled. Build names like `i8xds3c32_pe32x32_t512x1024_200mhz_tA`.

Host (`sw/gemm_host`): `numeric.hpp` gains the DenseShift codec and a mixed reference (A·w summed in the C width,
wrapping); `gemm_runner --dtype int8 --b-dtype dshift3 --c-dtype int32`, exact checks; `--a-file/--b-file/--c-out`
for the end-to-end run. Unit tests in `tests/test_numeric.cpp`.

### Part C: on the card
| Step | Question |
|---|---|
| g++ simulation of `gemm_mixed` (exact) | Is the kernel right for every A/B/C combination before HLS? |
| `ds_hls` (HLS only) | Cost of shift vs multiply MACs per format; which arrays fit (the int32 C buffer is 4–8× wider than the int4/int8 ones, so BRAM sets the limit) |
| `ds_same_array` (build, run) | int8×int8→int32 vs int8×dshift{2,3,4}→int32 at the same array: LUT/DSP/BRAM and timing |
| `ds_scale` (build, run) | Largest DenseShift arrays and clocks (int16 C where the sums fit) |
| MLP end to end | DenseShift hidden layers on the card, bit-exact against the PyTorch integer model, same test accuracy |
| Float activations (stretch) | A float32, B dshift, C float32: exponent add + fadd, no fmul |

## 3. Rules kept from the earlier work
- One Vivado run at a time (15 GB machine); JTAG programming also starts Vivado. GPU training may run beside HLS or
  Vivado only while memory allows.
- No edits in `kernels/gemm/` or `submodules/`. Build outputs, datasets and checkpoints stay out of git (`build/`).
- Progress written to the progress file at every step, commits as work goes on (no push).

## 4. Risks
- int32 C buffers: PE 32×32 needs a 1024-bit wide C buffer per PE; PE 64×64 int32 does not fit the 955 BRAM36.
- The shift MAC still has a C-width adder; the saving against an int8 multiply is the multiplier only (the int8
  builds had no DSP multipliers anyway: Vivado put 8-bit multiplies in LUTs). Expected: fewer LUTs per MAC, maybe a
  better clock; not a different throughput at the same array.
- CIFAR-10 training cost: ResNet-20 with T + 1 parameters per weight; epoch time measured before choosing epochs.
