# ml/denseshift: DenseShift in PyTorch

DenseShift (Li et al., ICCV 2023, [arXiv:2208.09708](https://arxiv.org/abs/2208.09708)) trains n-bit power-of-two
weights w = ±2^e with e ∈ {0 … 2^(n−1)−1} and no zero. Plan and dated log: [docs/notes3](../../docs/notes3/denseshift-progress.md).
Report: section "DenseShift" in `docs/report/`.

All scripts run with the **system Python** (`/usr/bin/python3`: torch with CUDA, numpy). The flow's `.venv` stays as
it is; the two sides exchange files (`export.npz`, int64 code files, JSON). Data and outputs go to `build/ml/`
(git-ignored).

| File | What |
|---|---|
| `denseshift.py` | `DenseShiftConv2d`, `DenseShiftLinear` for any n ≥ 2: sign-scale decomposition (T + 1 float parameters per weight, straight-through estimator), Eq. 8 backward (`rescale=False`: Eq. 7), low-variance or Kaiming init, `export_codes()` (code = sign << (n−1) \| e), `decode_codes()`. `python3 denseshift.py` runs the self-test, which also checks the 3-bit layer against a transcription of the official one (values and gradients). |
| `data.py` | CIFAR-10 (binary release, md5-checked) and MNIST (idx files) into `build/ml/data/`. |
| `train_cifar.py` | ResNet-20 on CIFAR-10 as in the official code (first conv and FC float, SGD 0.1, cosine, 200 epochs). Data and augmentation on the GPU, bf16 autocast. Output `build/ml/cifar/<name>/` (`log.jsonl`, `final.json` with the decoded-weight accuracy and the code histogram, `model.pt`). |
| `run_cifar.sh`, `run_cifar_seeds.sh`, `run_cifar_more.sh` | The runs in the table below, one after another. |
| `mlp_mnist.py` | `train`: MLP 784 → DenseShift 1024 → BN → ReLU → uint8 → DenseShift 1024 → BN → ReLU → uint8 → FC 10, with quantisation-aware training, exported as an integer pipeline (exact GEMMs, BN folded into a per-channel a, b, requantisation). `fpga`: runs both hidden layers on a card build through `gemm_runner --a-file/--b-file/--c-out`, compares every sum with the CPU and computes the accuracy from the card's sums. |
| `summarize.py` | Tables of all CIFAR and MNIST runs, means over seeds, card results; `--csv` writes `docs/report/data/denseshift_cifar.csv`. |

```
/usr/bin/python3 ml/denseshift/denseshift.py                                  # self-test
/usr/bin/python3 ml/denseshift/train_cifar.py --bits 3 --epochs 200 --seed 0  # --bits 0 = float32 baseline;
                                                                              # --no-rescale, --init kaiming
/usr/bin/python3 ml/denseshift/mlp_mnist.py train --bits 3                    # -> build/ml/mnist/ds3_h1024/
/usr/bin/python3 ml/denseshift/mlp_mnist.py fpga --run build/ml/mnist/ds3_h1024 \
        --build build/gemm/soc/<u8xds3c32 build>                              # card loaded, takes the board lock
/usr/bin/python3 ml/denseshift/summarize.py
```

`mlp_mnist.py train --act-bits 7` keeps the activation codes in 0..127 (inputs: pixel >> 1), so the network also runs
on a signed int8 × dshift build. `--act-bits 0` gives float activations (input pixel / 255, no quantisation):
`--bits 0 --act-bits 0` is the float32 × float32 baseline for a float32 build, and `--bits 3 --act-bits 0` runs on a
float32 × dshift3 build. Float runs are checked bit for bit against float32 sums in the kernel's k order.
`fpga --reps N` times each layer N times (median, default 3); `summarize.py` prints the speedups against the
float × float network on the float32 PE 32×8 200 MHz build.

## Results

CIFAR-10 ResNet-20, test accuracy at the end of training (200 epochs: mean and range of seeds 0–2; 400 epochs: seed 0):

| Weights | 200 epochs | 400 epochs |
|---|---|---|
| float32 | 91.70 % (91.56–91.85) | 92.24 % |
| DenseShift 2-bit | 91.06 % (90.84–91.43) | |
| DenseShift 3-bit | 91.32 % (91.10–91.47) | 92.05 % |
| DenseShift 4-bit | 90.89 % (90.63–91.25) | 91.64 % |
| 3-bit, Eq. 7 gradient (no rescale) | 91.22 % (91.11–91.36) | |
| 3-bit, Kaiming init | 90.23 % (89.99–90.50) | |

- 3-bit is 0.4 points below float (0.2 at 400 epochs). 4-bit stays below 3-bit, unlike the paper's ImageNet results.
- The low-variance init is worth 1.1 points. Eq. 8 against Eq. 7 is +0.1 point, within the seed spread, though Eq. 7
  leaves 76 % of the weights at ±1.
- The decoded copies (exported codes as ordinary weights) give exactly the same accuracy in every run.

MNIST MLP, uint8 activations: float32 weights 98.65 %, DenseShift 2/3/4-bit 98.46 / 98.47 / 98.37 %; the integer
pipeline equals PyTorch in every case. Float activations: float32 weights 98.60 %, 3-bit DenseShift 98.40 %.

On the card, both hidden layers over all 10 000 test images (0 of 10 240 000 sums differ from the CPU in either
layer, predictions identical to the CPU; times: both layers, median of 3):

| Activations × weights | Build (200 MHz) | Accuracy | Kernel ms | End to end ms | Speedup kernel / e2e |
|---|---|---|---|---|---|
| float32 × float32 | f32 PE 32×8 | 98.60 % | 396.9 | 559.2 | 1 / 1 |
| float32 × dshift3 | f32xds3 PE 32×16 | 98.40 % | 212.7 | 367.7 | 1.87 / 1.52 |
| uint8 × dshift3 | u8xds3c32 PE 32×32 | 98.47 % | 115.7 | 204.9 | 3.43 / 2.73 |
| 7-bit × dshift3 | i8xds3c32 PE 32×32 | 98.48 % | 115.7 | 204.9 | 3.43 / 2.73 |

The CIFAR-10 convolutions have run only in PyTorch so far, not on the card.
