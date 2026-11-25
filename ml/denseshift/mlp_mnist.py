#
# mlp_mnist.py - a DenseShift MLP on MNIST whose two hidden layers run as GEMMs on the card
# (docs/notes3/denseshift-plan.md, Part C4).
#
#   /usr/bin/python3 ml/denseshift/mlp_mnist.py train --bits 3                       GPU, -> build/ml/mnist/ds3/
#   /usr/bin/python3 ml/denseshift/mlp_mnist.py fpga --run build/ml/mnist/ds3 --build build/gemm/soc/<build_id>
#
# Network: 784 -> DenseShift(hidden) -> BN -> ReLU -> uint8 -> DenseShift(hidden) -> BN -> ReLU -> uint8 -> FC(10).
# The input is the raw pixel code (uint8), so both DenseShift layers see uint8 activation codes: A = uint8 codes,
# B = DenseShift codes, C = int32 sums, exactly the u8xds<n>c32 kernel. BN (with the activation scale) folds into a
# per-channel a, b after each GEMM; the uint8 codes of the next layer are clip(rint((acc * a + b) / s), 0, 255). The
# last layer stays float (as in the official code). Activation scales s are learned as running maxima during
# quantization-aware training (fake quantization with a straight-through round).
#
# `train` writes export.npz (codes, folds, scales, FC) and final.json with three accuracies: the PyTorch model, and
# the integer pipeline evaluated on the CPU (float64 GEMM: every partial sum is an integer below 2^53, so exact).
# `fpga` runs the two GEMMs on the card through the build's gemm_runner (--a-file/--b-file/--c-out), compares every
# C entry with the CPU reference and evaluates the same integer pipeline on the card's sums.
#

import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

import data
from denseshift import DenseShiftLinear, decode_codes

ROOT = data.ROOT


class FakeQuantU8(nn.Module):
    """Unsigned codes of a non-negative activation: clip(round(x / s), 0, qmax), s = running max / qmax (EMA); the round
    passes gradients straight through inside the range. Returns codes (not values): the next layer's GEMM input.
    qmax 255 = uint8 codes; 127 = codes that also fit int8 (signed int8 builds)."""

    def __init__(self, qmax=255, momentum=0.99):
        super().__init__()
        self.qmax = qmax
        self.momentum = momentum
        self.register_buffer("scale", torch.tensor(0.0))

    def forward(self, x):
        if self.training:
            with torch.no_grad():
                m = x.detach().amax() / self.qmax
                self.scale.copy_(m if self.scale == 0 else self.momentum * self.scale + (1 - self.momentum) * m)
        s = self.scale.clamp_min(1e-8)
        y = torch.clamp(x / s, 0, self.qmax)
        return y + (torch.round(y) - y).detach()


class MLP(nn.Module):
    def __init__(self, bits, hidden, qmax=255):
        super().__init__()
        lin = (lambda i, o: DenseShiftLinear(i, o, bits=bits)) if bits else (lambda i, o: nn.Linear(i, o, bias=False))
        self.fc1, self.bn1, self.q1 = lin(784, hidden), nn.BatchNorm1d(hidden), FakeQuantU8(qmax)
        self.fc2, self.bn2, self.q2 = lin(hidden, hidden), nn.BatchNorm1d(hidden), FakeQuantU8(qmax)
        self.fc3 = nn.Linear(hidden, 10)

    def forward(self, x_codes):
        h = self.q1(F.relu(self.bn1(self.fc1(x_codes))))
        h = self.q2(F.relu(self.bn2(self.fc2(h))))
        return self.fc3(h * self.q2.scale)


# Integer pipeline (shared by the CPU reference and the card) -------------------------------------------------------

def fold(lin_bn_q):
    """(a, b, s) with q = clip(rint((acc * a + b) / s), 0, 255) for acc = codes(A) @ value(W)^T."""
    _, bn, q = lin_bn_q
    inv = (bn.weight.detach() / torch.sqrt(bn.running_var + bn.eps)).double()
    a = inv
    b = bn.bias.detach().double() - bn.running_mean.double() * inv
    return a.cpu().numpy(), b.cpu().numpy(), float(q.scale)


def requant(acc, a, b, s, qmax=255):
    return np.clip(np.rint((acc.astype(np.float64) * a + b) / s), 0, qmax)


def gemm_ref(a_codes, w_values, layer=None):
    """Exact integer GEMM via float64 (all partial sums are integers below 2^53)."""
    return np.rint(a_codes.astype(np.float64) @ w_values.astype(np.float64).T).astype(np.int64)


def logits(q2, ex):
    return (q2 * ex["s2"]) @ ex["fc_w"].T.astype(np.float64) + ex["fc_b"].astype(np.float64)


def int_pipeline(x_codes, ex, gemm=gemm_ref):
    w1 = decode_codes(torch.from_numpy(ex["w1_codes"]), int(ex["bits"])).numpy()
    w2 = decode_codes(torch.from_numpy(ex["w2_codes"]), int(ex["bits"])).numpy()
    qmax = int(ex.get("qmax", 255))
    acc1 = gemm(x_codes >> int(ex.get("in_shift", 0)), w1, 1)
    q1 = requant(acc1, ex["a1"], ex["b1"], ex["s1"], qmax)
    acc2 = gemm(q1, w2, 2)
    q2 = requant(acc2, ex["a2"], ex["b2"], ex["s2"], qmax)
    return logits(q2, ex), (acc1, acc2)


# train ------------------------------------------------------------------------------------------------------------

def train(args):
    out = os.path.join(ROOT, "build", "ml", "mnist",
                       args.name or (f"ds{args.bits}" if args.bits else "fp32") + f"_h{args.hidden}"
                       + ("" if args.act_bits == 8 else f"_a{args.act_bits}"))
    qmax, in_shift = 2 ** args.act_bits - 1, 8 - args.act_bits
    os.makedirs(out, exist_ok=True)
    torch.manual_seed(args.seed)
    dev = torch.device("cuda")
    xtr, ytr, xte, yte = data.mnist()
    xtr, xte = xtr.flatten(1).to(dev) >> in_shift, xte.flatten(1).to(dev) >> in_shift  # pixel codes 0..qmax
    xtr, xte = xtr.float(), xte.float()
    ytr, yte = ytr.to(dev), yte.to(dev)
    model = MLP(args.bits, args.hidden, qmax).to(dev)
    opt = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=0.9, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    t0 = time.time()
    for epoch in range(args.epochs):
        model.train()
        perm = torch.randperm(xtr.shape[0], device=dev)
        for i in range(0, xtr.shape[0], args.batch):
            idx = perm[i:i + args.batch]
            loss = F.cross_entropy(model(xtr[idx]), ytr[idx])
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
        sched.step()
        model.eval()
        with torch.no_grad():
            acc = (model(xte).argmax(1) == yte).float().mean().item()
        print(f"epoch {epoch + 1:3d}  loss {loss.item():.4f}  test {acc:.4f}", flush=True)

    model.eval().cpu()
    if not args.bits:  # float32 baseline: same network and uint8 activations, float weights; no integer export
        with torch.no_grad():
            torch_acc = (model(xte.cpu()).argmax(1) == yte.cpu()).float().mean().item()
        final = {"bits": 0, "hidden": args.hidden, "epochs": args.epochs, "seed": args.seed,
                 "torch_test_acc": torch_acc, "int_pipeline_test_acc": None, "minutes": (time.time() - t0) / 60}
        with open(os.path.join(out, "final.json"), "w") as f:
            json.dump(final, f, indent=1)
        print(json.dumps(final), flush=True)
        return
    a1, b1, s1 = fold((model.fc1, model.bn1, model.q1))
    a2, b2, s2 = fold((model.fc2, model.bn2, model.q2))
    ex = {"bits": args.bits, "hidden": args.hidden, "qmax": qmax, "in_shift": in_shift,
          "w1_codes": model.fc1.export_codes().numpy(), "w2_codes": model.fc2.export_codes().numpy(),
          "a1": a1, "b1": b1, "s1": s1, "a2": a2, "b2": b2, "s2": s2,
          "fc_w": model.fc3.weight.detach().numpy(), "fc_b": model.fc3.bias.detach().numpy()}
    np.savez(os.path.join(out, "export.npz"), **ex)
    x_codes = (data.mnist()[2].flatten(1).numpy().astype(np.int64))  # raw pixels; int_pipeline applies in_shift
    with torch.no_grad():
        torch_acc = (model(xte.cpu()).argmax(1) == yte.cpu()).float().mean().item()
    lg, _ = int_pipeline(x_codes, ex)
    int_acc = float((lg.argmax(1) == yte.cpu().numpy()).mean())
    hist = {n: np.bincount(ex[n].ravel(), minlength=2 ** args.bits).tolist() for n in ("w1_codes", "w2_codes")}
    final = {"bits": args.bits, "hidden": args.hidden, "epochs": args.epochs, "seed": args.seed,
             "act_bits": args.act_bits, "torch_test_acc": torch_acc, "int_pipeline_test_acc": int_acc, "minutes": (time.time() - t0) / 60,
             "s1": s1, "s2": s2, "code_histogram": hist}
    with open(os.path.join(out, "final.json"), "w") as f:
        json.dump(final, f, indent=1)
    torch.save(model.state_dict(), os.path.join(out, "model.pt"))
    print(json.dumps({k: v for k, v in final.items() if k != "code_histogram"}), flush=True)


# fpga -------------------------------------------------------------------------------------------------------------

def fpga(args):
    sys.path.insert(0, os.path.join(ROOT, "flow"))
    from gemmflow.board import board_lock
    from gemmflow.config import BuildCfg
    from gemmflow.runner import runner_cmd

    run_dir = os.path.abspath(args.run)
    ex = dict(np.load(os.path.join(run_dir, "export.npz")))
    with open(os.path.join(args.build, "config.resolved.json")) as f:
        info = json.load(f)
    cfg = BuildCfg.from_dict(info["config"])
    nf = cfg.numeric()
    qmax = int(ex.get("qmax", 255))
    a_ok = nf.a.spec == "uint8" or (nf.a.spec == "int8" and qmax <= 127)
    if not nf.is_mixed or nf.b.spec != f"dshift{int(ex['bits'])}" or not a_ok or nf.c.spec != "int32":
        sys.exit(f"build {cfg.build_id()} is {nf.spec}; this network needs {'u' if qmax > 127 else '[u]'}int8 x "
                 f"dshift{int(ex['bits'])} -> int32")
    mn, mk, mm = cfg.size_multiples()
    base = runner_cmd(cfg, args.build, info.get("config_hash"))
    _, _, xte, yte = data.mnist()
    x_codes = xte.flatten(1).numpy().astype(np.int64)
    work = os.path.join(run_dir, "fpga")
    os.makedirs(work, exist_ok=True)
    report = {"build_id": cfg.build_id(), "dtype": nf.spec, "layers": []}
    up = lambda x, m: -(-x // m) * m

    def gemm_card(a_codes, w_values, layer):
        n, k = a_codes.shape
        m = w_values.shape[0]
        npad, kpad, mpad = up(n, mn), up(k, mk), up(m, mm)
        a = np.zeros((npad, kpad), np.int64)
        a[:n, :k] = a_codes
        codes = ex[f"w{layer}_codes"]
        b = np.zeros((kpad, mpad), np.int64)       # padded rows/columns: code 0 (+1) times A = 0, or dropped
        b[:k, :m] = codes.T
        pa, pb, pc = (os.path.join(work, f"layer{layer}.{x}") for x in ("a", "b", "c"))
        a.tofile(pa)
        b.tofile(pb)
        cmd = base + ["--n", str(npad), "--k", str(kpad), "--m", str(mpad), "--a-file", pa, "--b-file", pb,
                      "--c-out", pc, "--verify", "sampled", "--json", "--reps", "1"]
        p = subprocess.run(cmd, capture_output=True, text=True)
        rows = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
        if p.returncode or not rows:
            sys.exit(f"gemm_runner failed (exit {p.returncode}):\n{p.stdout}\n{p.stderr}")
        c = np.fromfile(pc, np.int64).reshape(npad, mpad)[:n, :m]
        ref = gemm_ref(a_codes, w_values)
        bad = int((c != ref).sum())
        r = rows[0]
        report["layers"].append({"layer": layer, "n": npad, "k": kpad, "m": mpad, "kernel_s": r["kernel_s"],
                                 "gops": r["gops"], "runner_verify_bad": r["verify_bad"], "c_entries": int(c.size),
                                 "c_mismatches_vs_cpu": bad, "acc_min": int(c.min()), "acc_max": int(c.max())})
        print(f"layer {layer}: {npad}x{kpad}x{mpad} on the card, {r['kernel_s'] * 1e3:.2f} ms, {r['gops']:.1f} GOP/s, "
              f"{bad} of {c.size} sums differ from the CPU", flush=True)
        return c

    with board_lock():
        lg_card, _ = int_pipeline(x_codes, ex, gemm=gemm_card)
    lg_cpu, _ = int_pipeline(x_codes, ex)
    y = yte.numpy()
    report["card_test_acc"] = float((lg_card.argmax(1) == y).mean())
    report["cpu_int_test_acc"] = float((lg_cpu.argmax(1) == y).mean())
    report["predictions_equal"] = bool((lg_card.argmax(1) == lg_cpu.argmax(1)).all())
    with open(os.path.join(run_dir, f"fpga_{cfg.build_id()}.json"), "w") as f:
        json.dump(report, f, indent=1)
    print(json.dumps({k: v for k, v in report.items() if k != "layers"}), flush=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--bits", type=int, default=3)
    t.add_argument("--hidden", type=int, default=1024)
    t.add_argument("--epochs", type=int, default=30)
    t.add_argument("--batch", type=int, default=256)
    t.add_argument("--lr", type=float, default=0.1)
    t.add_argument("--seed", type=int, default=0)
    t.add_argument("--act-bits", type=int, default=8, choices=(7, 8),
                   help="activation codes 0..2^bits-1 (7: also runs on signed int8 builds)")
    t.add_argument("--name")
    f = sub.add_parser("fpga")
    f.add_argument("--run", required=True, help="training output dir (export.npz)")
    f.add_argument("--build", required=True, help="SoC build dir of a uint8 x dshift<n> -> int32 build")
    args = ap.parse_args()
    train(args) if args.cmd == "train" else fpga(args)


if __name__ == "__main__":
    main()
