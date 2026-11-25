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
# Float activations (`--act-bits 0`, the float x float baseline and the paper's float-activation mode): no activation
# quantization, input = pixel / 255, h = relu(acc * a + b) in float. `--bits 0 --act-bits 0` is a plain float MLP
# whose GEMMs are float32 x float32 -> float32 (kernels/gemm float32 builds); `--bits n --act-bits 0` has DenseShift
# weights on float activations (float32 x dshift<n> -> float32 builds). The CPU reference sums the products in the
# kernel's order (c = c + a*w for k = 0, 1, ...; float32, round to nearest, no FMA), so the card's sums must be
# equal to it, not just close.
#
# Every `fpga` report records per layer the kernel time and the end-to-end time of the runner (A and B to the card,
# kernel, C back), so runs on different builds give speedups (summarize.py: against the float x float run).
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
    """qmax = 0: float activations (no quantization)."""

    def __init__(self, bits, hidden, qmax=255):
        super().__init__()
        lin = (lambda i, o: DenseShiftLinear(i, o, bits=bits)) if bits else (lambda i, o: nn.Linear(i, o, bias=False))
        quant = (lambda: FakeQuantU8(qmax)) if qmax else nn.Identity
        self.fc1, self.bn1, self.q1 = lin(784, hidden), nn.BatchNorm1d(hidden), quant()
        self.fc2, self.bn2, self.q2 = lin(hidden, hidden), nn.BatchNorm1d(hidden), quant()
        self.fc3 = nn.Linear(hidden, 10)

    def forward(self, x):
        h = self.q1(F.relu(self.bn1(self.fc1(x))))
        h = self.q2(F.relu(self.bn2(self.fc2(h))))
        return self.fc3(h * self.q2.scale if isinstance(self.q2, FakeQuantU8) else h)


# Integer pipeline (shared by the CPU reference and the card) -------------------------------------------------------

def fold(lin_bn_q):
    """(a, b, s) with q = clip(rint((acc * a + b) / s), 0, 255) for acc = codes(A) @ value(W)^T (s = 1 for float
    activations: h = relu(acc * a + b))."""
    _, bn, q = lin_bn_q
    inv = (bn.weight.detach() / torch.sqrt(bn.running_var + bn.eps)).double()
    a = inv
    b = bn.bias.detach().double() - bn.running_mean.double() * inv
    return a.cpu().numpy(), b.cpu().numpy(), float(q.scale) if isinstance(q, FakeQuantU8) else 1.0


def requant(acc, a, b, s, qmax=255):
    return np.clip(np.rint((acc.astype(np.float64) * a + b) / s), 0, qmax)


def gemm_ref(a_codes, w_values, layer=None):
    """Exact integer GEMM via float64 (all partial sums are integers below 2^53)."""
    return np.rint(a_codes.astype(np.float64) @ w_values.astype(np.float64).T).astype(np.int64)


def logits(q2, ex):
    return (q2 * ex["s2"]) @ ex["fc_w"].T.astype(np.float64) + ex["fc_b"].astype(np.float64)


# Float pipeline (float activations) -------------------------------------------------------------------------------

def gemm_ref_float(a, w_values, layer=None):
    """float32 sums in the kernel's order: c = 0; c = c + a[:, k] * w[:, k] for k = 0, 1, ... (separate multiply and
    add, both rounded to nearest), as the PEs accumulate. On the GPU when there is one (elementwise ops: exact IEEE)."""
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    at = torch.from_numpy(np.ascontiguousarray(a, np.float32)).to(dev)
    wt = torch.from_numpy(np.ascontiguousarray(w_values.T, np.float32)).to(dev)  # K x M
    c = torch.zeros(at.shape[0], wt.shape[1], dtype=torch.float32, device=dev)
    for k in range(at.shape[1]):
        c.add_(at[:, k:k + 1] * wt[k:k + 1, :])
    return c.cpu().numpy()


def float_weights(ex, layer):
    if int(ex["bits"]):
        return decode_codes(torch.from_numpy(ex[f"w{layer}_codes"]), int(ex["bits"])).numpy().astype(np.float32)
    return ex[f"w{layer}"].astype(np.float32)


def float_pipeline(x, ex, gemm=gemm_ref_float):
    """x: float32 inputs (pixel / 255). Returns logits and the two GEMM results."""
    acc1 = gemm(x, float_weights(ex, 1), 1)
    h1 = np.maximum(acc1.astype(np.float64) * ex["a1"] + ex["b1"], 0).astype(np.float32)
    acc2 = gemm(h1, float_weights(ex, 2), 2)
    h2 = np.maximum(acc2.astype(np.float64) * ex["a2"] + ex["b2"], 0)
    return h2 @ ex["fc_w"].T.astype(np.float64) + ex["fc_b"].astype(np.float64), (acc1, acc2)


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
                       + ("" if args.act_bits == 8 else "_af" if args.act_bits == 0 else f"_a{args.act_bits}"))
    float_act = args.act_bits == 0
    qmax, in_shift = (0, 0) if float_act else (2 ** args.act_bits - 1, 8 - args.act_bits)
    os.makedirs(out, exist_ok=True)
    torch.manual_seed(args.seed)
    dev = torch.device("cuda")
    xtr, ytr, xte, yte = data.mnist()
    xtr, xte = xtr.flatten(1).to(dev) >> in_shift, xte.flatten(1).to(dev) >> in_shift  # pixel codes 0..qmax
    xtr, xte = xtr.float(), xte.float()
    if float_act:  # float activations: inputs in [0, 1]
        xtr, xte = xtr / 255, xte / 255
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
    if float_act:
        return export_float(args, model, xte.cpu(), yte.cpu(), out, t0)
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


def export_float(args, model, xte, yte, out, t0):
    """Float activations: export weights (float32 or DenseShift codes) and BN folds; check the float pipeline."""
    a1, b1, _ = fold((model.fc1, model.bn1, model.q1))
    a2, b2, _ = fold((model.fc2, model.bn2, model.q2))
    ex = {"bits": args.bits, "hidden": args.hidden, "act_bits": 0, "a1": a1, "b1": b1, "a2": a2, "b2": b2,
          "fc_w": model.fc3.weight.detach().numpy(), "fc_b": model.fc3.bias.detach().numpy()}
    for i, fc in ((1, model.fc1), (2, model.fc2)):
        if args.bits:
            ex[f"w{i}_codes"] = fc.export_codes().numpy()
        else:
            ex[f"w{i}"] = fc.weight.detach().numpy().astype(np.float32)
    np.savez(os.path.join(out, "export.npz"), **ex)
    with torch.no_grad():
        torch_acc = (model(xte).argmax(1) == yte).float().mean().item()
    lg, _ = float_pipeline(xte.numpy().astype(np.float32), ex)
    pipe_acc = float((lg.argmax(1) == yte.numpy()).mean())
    final = {"bits": args.bits, "hidden": args.hidden, "epochs": args.epochs, "seed": args.seed, "act_bits": 0,
             "torch_test_acc": torch_acc, "float_pipeline_test_acc": pipe_acc, "minutes": (time.time() - t0) / 60}
    if args.bits:
        final["code_histogram"] = {n: np.bincount(ex[n].ravel(), minlength=2 ** args.bits).tolist()
                                   for n in ("w1_codes", "w2_codes")}
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
    bits = int(ex["bits"])
    float_act = int(ex.get("act_bits", 8)) == 0
    qmax = int(ex.get("qmax", 255))
    if float_act:
        need = f"float32xdshift{bits}-float32" if bits else "float32"
        if nf.spec != need:
            sys.exit(f"build {cfg.build_id()} is {nf.spec}; this network needs {need}")
    else:
        a_ok = nf.a.spec == "uint8" or (nf.a.spec == "int8" and qmax <= 127)
        if not nf.is_mixed or nf.b.spec != f"dshift{bits}" or not a_ok or nf.c.spec != "int32":
            sys.exit(f"build {cfg.build_id()} is {nf.spec}; this network needs {'u' if qmax > 127 else '[u]'}int8 x "
                     f"dshift{bits} -> int32")
    mn, mk, mm = cfg.size_multiples()
    base = runner_cmd(cfg, args.build, info.get("config_hash"))
    _, _, xte, yte = data.mnist()
    x_codes = xte.flatten(1).numpy().astype(np.int64)
    work = os.path.join(run_dir, "fpga")
    os.makedirs(work, exist_ok=True)
    report = {"build_id": cfg.build_id(), "dtype": nf.spec, "net": os.path.basename(run_dir), "bits": bits,
              "act": "float32" if float_act else nf.a.spec, "accel_mhz": cfg.clocks.accel_mhz,
              "pe": [cfg.kernel.parallelism_n, cfg.kernel.parallelism_m], "layers": []}
    up = lambda x, m: -(-x // m) * m
    reps = args.reps

    def gemm_card(a_in, w_values, layer):
        n, k = a_in.shape
        m = w_values.shape[0]
        npad, kpad, mpad = up(n, mn), up(k, mk), up(m, mm)
        pa, pb, pc = (os.path.join(work, f"layer{layer}.{x}") for x in ("a", "b", "c"))
        a = np.zeros((npad, kpad), np.float32 if float_act else np.int64)
        a[:n, :k] = a_in
        a.tofile(pa)
        if float_act and not bits:                 # float x float: B = the float weights
            b = np.zeros((kpad, mpad), np.float32)
            b[:k, :m] = w_values.T
        else:                                      # DenseShift codes; padded rows/columns: code 0 (+1) times A = 0
            b = np.zeros((kpad, mpad), np.int64)
            b[:k, :m] = ex[f"w{layer}_codes"].T
        b.tofile(pb)
        cmd = base + ["--n", str(npad), "--k", str(kpad), "--m", str(mpad), "--a-file", pa, "--b-file", pb,
                      "--c-out", pc, "--verify", "sampled", "--json", "--reps", str(reps)]
        p = subprocess.run(cmd, capture_output=True, text=True)
        rows = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
        if p.returncode or not rows:
            sys.exit(f"gemm_runner failed (exit {p.returncode}):\n{p.stdout}\n{p.stderr}")
        c = np.fromfile(pc, np.float32 if float_act else np.int64).reshape(npad, mpad)[:n, :m]
        if float_act:
            ref = gemm_ref_float(a_in, w_values)
            bad = int((c != ref).sum() + np.isnan(c).sum())
        else:
            ref = gemm_ref(a_in, w_values)
            bad = int((c != ref).sum())
        med = lambda key: float(np.median([r[key] for r in rows]))
        report["layers"].append({"layer": layer, "n": npad, "k": kpad, "m": mpad, "reps": len(rows),
                                 "kernel_s": med("kernel_s"), "gops": med("gops"), "e2e_s": med("e2e_s"),
                                 "h2c_s": med("h2c_s"), "c2h_s": med("c2h_s"), "h2c_bytes": rows[0]["h2c_bytes"],
                                 "c2h_bytes": rows[0]["c2h_bytes"],
                                 "runner_verify_bad": sum(r["verify_bad"] for r in rows), "c_entries": int(c.size),
                                 "c_mismatches_vs_cpu": bad, "acc_min": float(c.min()), "acc_max": float(c.max())})
        l = report["layers"][-1]
        print(f"layer {layer}: {npad}x{kpad}x{mpad} on the card, kernel {l['kernel_s'] * 1e3:.2f} ms "
              f"({l['gops']:.1f} GOP/s), end to end {l['e2e_s'] * 1e3:.1f} ms, {bad} of {c.size} sums differ from "
              f"the CPU", flush=True)
        return c

    y = yte.numpy()
    if float_act:
        x = (xte.flatten(1).numpy().astype(np.float32) / 255).astype(np.float32)
        with board_lock():
            lg_card, _ = float_pipeline(x, ex, gemm=gemm_card)
        lg_cpu, _ = float_pipeline(x, ex)
    else:
        with board_lock():
            lg_card, _ = int_pipeline(x_codes, ex, gemm=gemm_card)
        lg_cpu, _ = int_pipeline(x_codes, ex)
    report["card_test_acc"] = float((lg_card.argmax(1) == y).mean())
    report["cpu_int_test_acc"] = float((lg_cpu.argmax(1) == y).mean())  # the CPU pipeline (integer or float)
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
    t.add_argument("--act-bits", type=int, default=8, choices=(0, 7, 8),
                   help="activation codes 0..2^bits-1 (7: also runs on signed int8 builds); 0 = float activations")
    t.add_argument("--name")
    f = sub.add_parser("fpga")
    f.add_argument("--run", required=True, help="training output dir (export.npz)")
    f.add_argument("--build", required=True, help="SoC build dir: [u]int8 x dshift<n> -> int32, float32 x "
                   "dshift<n> -> float32 or float32 (float activations)")
    f.add_argument("--reps", type=int, default=3, help="runner repetitions per layer (times: median)")
    args = ap.parse_args()
    train(args) if args.cmd == "train" else fpga(args)


if __name__ == "__main__":
    main()
