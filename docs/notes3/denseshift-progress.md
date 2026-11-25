# DenseShift: progress

Plan: [denseshift-plan.md](denseshift-plan.md). Branch `denseshift` from `1ad6b99`.

| Phase | What | State |
|---|---|---|
| 0 | Read paper + official code, plan | ✅ |
| A1 | PyTorch DenseShift layers + unit checks | ⬜ |
| A2 | CIFAR-10 ResNet-20: float32 vs DenseShift 2/3/4-bit | ⬜ |
| B1 | `kernels/gemm_mixed/` (A/B/C types, shift MAC) + g++ simulation | ⬜ |
| B2 | Flow: config fields, numeric formats, kernel dir, hashes, names, columns | ⬜ |
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
