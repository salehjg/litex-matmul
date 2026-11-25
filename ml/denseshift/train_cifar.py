#
# train_cifar.py - ResNet-20 on CIFAR-10, float32 vs DenseShift n-bit (docs/notes3/denseshift-plan.md, Part A2).
#
#   /usr/bin/python3 ml/denseshift/train_cifar.py --bits 0 --epochs 200       float32 baseline
#   /usr/bin/python3 ml/denseshift/train_cifar.py --bits 3 --epochs 200       3-bit DenseShift
#   options: --no-rescale (Eq. 7 instead of Eq. 8), --init kaiming (instead of low-variance), --seed, --name
#
# As in the official code (S3-Training/resnet.py, main.py): first conv and final FC float, every other conv (3x3 and
# the 1x1 downsample) DenseShift, BatchNorm after each conv, SGD lr 0.1 momentum 0.9 wd 1e-4, cosine schedule.
# CIFAR-10 sized: ResNet-20 (3 stages x 3 blocks, 16/32/64 channels), batch 128, random crop (pad 4) + flip.
# The whole dataset sits on the GPU; augmentation runs there too. Output: build/ml/cifar/<name>/ (log.jsonl per
# epoch, final.json, model.pt).
#

import argparse
import json
import os
import time

import torch
import torch.nn as nn
import torch.nn.functional as F

import data
from denseshift import DenseShiftConv2d, DenseShiftMixin, decode_codes

MEAN = (0.4914, 0.4822, 0.4465)
STD = (0.2470, 0.2435, 0.2616)


def make_conv(args):
    def conv(cin, cout, k, stride, pad):
        if args.bits:
            return DenseShiftConv2d(cin, cout, k, stride=stride, padding=pad, bits=args.bits,
                                    rescale=not args.no_rescale, init=args.init)
        return nn.Conv2d(cin, cout, k, stride=stride, padding=pad, bias=False)
    return conv


class Block(nn.Module):
    def __init__(self, cin, cout, stride, conv):
        super().__init__()
        self.conv1, self.bn1 = conv(cin, cout, 3, stride, 1), nn.BatchNorm2d(cout)
        self.conv2, self.bn2 = conv(cout, cout, 3, 1, 1), nn.BatchNorm2d(cout)
        self.short = nn.Sequential()
        if stride != 1 or cin != cout:
            self.short = nn.Sequential(conv(cin, cout, 1, stride, 0), nn.BatchNorm2d(cout))

    def forward(self, x):
        out = F.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        return F.relu(out + self.short(x))


class ResNet20(nn.Module):
    def __init__(self, conv, n=3, classes=10):
        super().__init__()
        self.conv1 = nn.Conv2d(3, 16, 3, padding=1, bias=False)   # float, as in the paper
        self.bn1 = nn.BatchNorm2d(16)
        layers, cin = [], 16
        for cout, stride in ((16, 1), (32, 2), (64, 2)):
            for i in range(n):
                layers.append(Block(cin, cout, stride if i == 0 else 1, conv))
                cin = cout
        self.layers = nn.Sequential(*layers)
        self.fc = nn.Linear(64, classes)                          # float, as in the official code
        for m in self.modules():
            if isinstance(m, nn.Conv2d) and not isinstance(m, DenseShiftMixin):
                k = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                nn.init.normal_(m.weight, 0, (2.0 / k) ** 0.5)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.ones_(m.weight)
                nn.init.zeros_(m.bias)

    def forward(self, x):
        x = F.relu(self.bn1(self.conv1(x)))
        x = self.layers(x)
        x = F.adaptive_avg_pool2d(x, 1).flatten(1)
        return self.fc(x)


def augment(x_u8, gen):
    """Random crop 32 from the zero-padded 40x40 image and random horizontal flip, on the GPU."""
    b = x_u8.shape[0]
    x = F.pad(x_u8.float() / 255.0, (4, 4, 4, 4))
    oy = torch.randint(0, 9, (b,), device=x.device, generator=gen)
    ox = torch.randint(0, 9, (b,), device=x.device, generator=gen)
    ar = torch.arange(32, device=x.device)
    rows = (oy[:, None] + ar)[:, None, :, None]
    cols = (ox[:, None] + ar)[:, None, None, :]
    x = x[torch.arange(b, device=x.device)[:, None, None, None], torch.arange(3, device=x.device)[None, :, None, None],
          rows, cols]
    flip = torch.rand(b, device=x.device, generator=gen) < 0.5
    return torch.where(flip[:, None, None, None], x.flip(3), x)


def normalize(x):
    m = torch.tensor(MEAN, device=x.device)[None, :, None, None]
    s = torch.tensor(STD, device=x.device)[None, :, None, None]
    return (x - m) / s


@torch.no_grad()
def evaluate(model, x_u8, y, amp, batch=1000):
    model.eval()
    correct = 0
    for i in range(0, x_u8.shape[0], batch):
        with torch.autocast("cuda", dtype=torch.bfloat16, enabled=amp):
            out = model(normalize(x_u8[i:i + batch].float() / 255.0))
        correct += (out.argmax(1) == y[i:i + batch]).sum().item()
    return correct / x_u8.shape[0]


def code_histogram(model):
    """Per DenseShift layer: fraction of weights at each code (sign, exponent)."""
    hist = {}
    for name, m in model.named_modules():
        if isinstance(m, DenseShiftMixin):
            codes = m.export_codes().flatten()
            hist[name] = (torch.bincount(codes, minlength=2 ** m.ds_bits).float() / codes.numel()).tolist()
    return hist


@torch.no_grad()
def decoded_copy(model, args):
    """Float ResNet-20 with the exported codes decoded into ordinary conv weights: must score the same."""
    ref = ResNet20(make_conv(argparse.Namespace(bits=0))).to(next(model.parameters()).device)
    sd = {}
    for name, t in model.state_dict().items():
        if name.endswith("weight_t"):
            continue
        sd[name] = t
    for name, m in model.named_modules():
        if isinstance(m, DenseShiftMixin):
            sd[name + ".weight"] = decode_codes(m.export_codes(), m.ds_bits).to(sd[name + ".weight"].device)
    ref.load_state_dict(sd)
    return ref


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bits", type=int, default=3, help="DenseShift weight bits; 0 = float32 baseline")
    ap.add_argument("--epochs", type=int, default=200)
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--lr", type=float, default=0.1)
    ap.add_argument("--wd", type=float, default=1e-4)
    ap.add_argument("--no-rescale", action="store_true", help="Eq. 7 gradient instead of Eq. 8")
    ap.add_argument("--init", default="lowvar", choices=("lowvar", "kaiming"))
    ap.add_argument("--no-amp", action="store_true", help="float32 instead of bf16 autocast")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--name", default=None)
    args = ap.parse_args()
    name = args.name or (f"ds{args.bits}" if args.bits else "fp32") + \
        ("_eq7" if args.no_rescale else "") + ("_kaiming" if args.init == "kaiming" else "") + f"_e{args.epochs}_s{args.seed}"
    out = os.path.join(data.ROOT, "build", "ml", "cifar", name)
    os.makedirs(out, exist_ok=True)

    torch.manual_seed(args.seed)
    torch.backends.cudnn.benchmark = True
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    dev = torch.device("cuda")
    amp = not args.no_amp
    xtr, ytr, xte, yte = (t.to(dev) for t in data.cifar10())
    model = ResNet20(make_conv(args)).to(dev)
    opt = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=0.9, weight_decay=args.wd)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    gen = torch.Generator(device=dev)
    gen.manual_seed(args.seed)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"{name}: {n_params} trainable parameters, out {out}", flush=True)

    best, t0 = 0.0, time.time()
    with open(os.path.join(out, "log.jsonl"), "w") as log:
        for epoch in range(args.epochs):
            model.train()
            te = time.time()
            perm = torch.randperm(xtr.shape[0], device=dev, generator=gen)
            loss_sum, correct, seen = 0.0, 0, 0
            for i in range(0, xtr.shape[0] - args.batch + 1, args.batch):
                idx = perm[i:i + args.batch]
                x = normalize(augment(xtr[idx], gen))
                with torch.autocast("cuda", dtype=torch.bfloat16, enabled=amp):
                    logits = model(x)
                    loss = F.cross_entropy(logits, ytr[idx])
                opt.zero_grad(set_to_none=True)
                loss.backward()
                opt.step()
                loss_sum += loss.item() * idx.numel()
                correct += (logits.argmax(1) == ytr[idx]).sum().item()
                seen += idx.numel()
            sched.step()
            acc = evaluate(model, xte, yte, amp)
            best = max(best, acc)
            row = {"epoch": epoch + 1, "lr": opt.param_groups[0]["lr"], "train_loss": loss_sum / seen,
                   "train_acc": correct / seen, "test_acc": acc, "epoch_s": time.time() - te}
            log.write(json.dumps(row) + "\n")
            log.flush()
            print(f"{name} ep {epoch + 1:3d}  loss {row['train_loss']:.3f}  train {row['train_acc']:.4f}  "
                  f"test {acc:.4f}  ({row['epoch_s']:.1f} s)", flush=True)

    final = {"name": name, "bits": args.bits, "epochs": args.epochs, "batch": args.batch, "lr": args.lr,
             "wd": args.wd, "rescale": not args.no_rescale, "init": args.init, "amp": amp, "seed": args.seed,
             "params": n_params, "final_test_acc": acc, "best_test_acc": best, "minutes": (time.time() - t0) / 60}
    if args.bits:
        final["decoded_test_acc"] = evaluate(decoded_copy(model, args), xte, yte, amp)
        final["code_histogram"] = code_histogram(model)
    with open(os.path.join(out, "final.json"), "w") as f:
        json.dump(final, f, indent=1)
    torch.save(model.state_dict(), os.path.join(out, "model.pt"))
    print(json.dumps({k: v for k, v in final.items() if k != "code_histogram"}), flush=True)


if __name__ == "__main__":
    main()
