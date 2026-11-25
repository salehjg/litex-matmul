#
# summarize.py - table of the CIFAR-10 runs (build/ml/cifar/*/final.json) and the MNIST runs (build/ml/mnist/*/).
#   /usr/bin/python3 ml/denseshift/summarize.py [--csv docs/report/data/denseshift_cifar.csv]
#                                               [--mnist-csv docs/report/data/denseshift_mnist_card.csv]
# MNIST card runs with timings (mlp_mnist.py fpga since the float x float baseline) also get speedups against the
# float x float network (fp32_h1024_af) on the float32 PE 32x8 200 MHz build.
# Needs only the standard library (also runs with the .venv Python).
#

import argparse
import glob
import json
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def cifar_rows():
    rows = []
    for f in sorted(glob.glob(os.path.join(ROOT, "build", "ml", "cifar", "*", "final.json"))):
        r = json.load(open(f))
        hist = r.get("code_histogram") or {}
        # Share of each exponent over all DenseShift layers (codes c and c + 2^(n-1) have the same exponent).
        exp_share = None
        if hist and r["bits"]:
            half = 2 ** (r["bits"] - 1)
            tot = [0.0] * half
            for h in hist.values():
                for c, v in enumerate(h):
                    tot[c % half] += v
            exp_share = [round(t / len(hist), 3) for t in tot]
        rows.append({"run": r["name"], "weights": f"DenseShift {r['bits']}-bit" if r["bits"] else "float32",
                     "bits": r["bits"], "grad": "" if not r["bits"] else ("Eq. 8" if r["rescale"] else "Eq. 7"),
                     "init": "" if not r["bits"] else r["init"], "epochs": r["epochs"],
                     "test_acc": round(100 * r["final_test_acc"], 2), "best_acc": round(100 * r["best_test_acc"], 2),
                     "decoded_acc": round(100 * r["decoded_test_acc"], 2) if "decoded_test_acc" in r else None,
                     "params": r["params"], "minutes": round(r["minutes"], 1), "exp_share": exp_share})
    return rows


BASELINE_NET, BASELINE_BUILD = "fp32_h1024_af", "f32_pe32x8_t1024x512_200mhz_tA"


def mnist_card_rows():
    """One row per card run with timings: kernel and end-to-end ms of the two hidden layers, speedups against the
    float x float network on the 200 MHz float32 build."""
    rows = []
    for g in sorted(glob.glob(os.path.join(ROOT, "build", "ml", "mnist", "*", "fpga_*.json"))):
        c = json.load(open(g))
        if not c["layers"] or "e2e_s" not in c["layers"][0]:
            continue  # runs before the timing columns
        acc = json.load(open(os.path.join(os.path.dirname(g), "final.json")))
        rows.append({"net": c.get("net", os.path.basename(os.path.dirname(g))), "build": c["build_id"],
                     "dtype": c["dtype"], "act": c.get("act", ""), "weights": f"dshift{c['bits']}" if c["bits"] else "float32",
                     "mhz": c.get("accel_mhz"), "pe": "x".join(map(str, c.get("pe", []))),
                     "k1_ms": 1e3 * c["layers"][0]["kernel_s"], "k2_ms": 1e3 * c["layers"][1]["kernel_s"],
                     "e1_ms": 1e3 * c["layers"][0]["e2e_s"], "e2_ms": 1e3 * c["layers"][1]["e2e_s"],
                     "h2c_mb": sum(l["h2c_bytes"] for l in c["layers"]) / 2 ** 20,
                     "mismatches": sum(l["c_mismatches_vs_cpu"] for l in c["layers"]),
                     "card_acc": 100 * c["card_test_acc"], "torch_acc": 100 * acc["torch_test_acc"],
                     "predictions_equal": c["predictions_equal"]})
    base = [r for r in rows if r["net"] == BASELINE_NET and r["build"].startswith(BASELINE_BUILD)]
    for r in rows:
        r["kernel_ms"], r["e2e_ms"] = r["k1_ms"] + r["k2_ms"], r["e1_ms"] + r["e2_ms"]
    for r in rows:
        r["speedup_kernel"] = base[0]["kernel_ms"] / r["kernel_ms"] if base else None
        r["speedup_e2e"] = base[0]["e2e_ms"] / r["e2e_ms"] if base else None
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--mnist-csv")
    args = ap.parse_args()
    rows = cifar_rows()
    print("| Run | Weights | Gradient | Init | Epochs | Test acc. (final) | Best | Decoded | Exponent share |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['run']} | {r['weights']} | {r['grad']} | {r['init']} | {r['epochs']} | {r['test_acc']:.2f} % | "
              f"{r['best_acc']:.2f} % | {'' if r['decoded_acc'] is None else f'{r['decoded_acc']:.2f} %'} | "
              f"{'' if r['exp_share'] is None else ' / '.join(f'{x:.2f}' for x in r['exp_share'])} |")
    for f in sorted(glob.glob(os.path.join(ROOT, "build", "ml", "mnist", "*", "final.json"))):
        r = json.load(open(f))
        ip = r.get("int_pipeline_test_acc")
        print(f"MNIST {os.path.basename(os.path.dirname(f))}: torch {100 * r['torch_test_acc']:.2f} %"
              + (f", integer pipeline {100 * ip:.2f} %" if ip is not None else ""))
        for g in sorted(glob.glob(os.path.join(os.path.dirname(f), "fpga_*.json"))):
            c = json.load(open(g))
            print(f"  card {c['build_id']}: {100 * c['card_test_acc']:.2f} % (CPU integer {100 * c['cpu_int_test_acc']:.2f} %), "
                  f"predictions equal: {c['predictions_equal']}, mismatching sums: "
                  f"{sum(l['c_mismatches_vs_cpu'] for l in c['layers'])}")
    # Over seeds: runs that differ only in the seed (name suffix _s<seed>).
    groups = {}
    for r in rows:
        groups.setdefault(r["run"].rsplit("_s", 1)[0], []).append(r["test_acc"])
    print()
    print("| Configuration | Seeds | Mean test acc. | Min | Max |")
    print("|---|---|---|---|---|")
    for g, accs in groups.items():
        print(f"| {g} | {len(accs)} | {sum(accs) / len(accs):.2f} % | {min(accs):.2f} % | {max(accs):.2f} % |")
    mrows = mnist_card_rows()
    if mrows:
        print()
        print(f"MNIST hidden layers on the card (10000 images; speedup against {BASELINE_NET} on {BASELINE_BUILD}):")
        print("| Network | Build | Format | Kernel ms (L1 + L2) | End to end ms | Speedup kernel | Speedup e2e | "
              "Card acc. | Sums != CPU |")
        print("|---|---|---|---|---|---|---|---|---|")
        for r in mrows:
            sk = "" if r["speedup_kernel"] is None else f"{r['speedup_kernel']:.2f}x"
            se = "" if r["speedup_e2e"] is None else f"{r['speedup_e2e']:.2f}x"
            print(f"| {r['net']} | {r['build'].split('-')[0]} | {r['dtype']} | {r['k1_ms']:.1f} + {r['k2_ms']:.1f} | "
                  f"{r['e2e_ms']:.1f} | {sk} | {se} | {r['card_acc']:.2f} % | {r['mismatches']} |")
    if args.mnist_csv and mrows:
        keys = ["net", "build", "dtype", "act", "weights", "pe", "mhz", "k1_ms", "k2_ms", "kernel_ms", "e1_ms", "e2_ms",
                "e2e_ms", "h2c_mb", "speedup_kernel", "speedup_e2e", "card_acc", "torch_acc", "mismatches"]
        fmt = lambda v: f"{v:.3f}" if isinstance(v, float) else str(v)
        with open(args.mnist_csv, "w") as f:
            f.write("# MNIST MLP hidden layers on the card (ml/denseshift/mlp_mnist.py fpga); generated by ml/denseshift/summarize.py\n")
            f.write(",".join(keys) + "\n")
            for r in mrows:
                f.write(",".join(fmt(r[k]) for k in keys) + "\n")
    if args.csv:
        keys = ["run", "weights", "bits", "grad", "init", "epochs", "test_acc", "best_acc", "decoded_acc", "params", "minutes"]
        with open(args.csv, "w") as f:
            f.write("# CIFAR-10 ResNet-20, float32 vs DenseShift (ml/denseshift/train_cifar.py); generated by ml/denseshift/summarize.py\n")
            f.write(",".join(keys) + "\n")
            for r in rows:
                f.write(",".join("" if r[k] is None else str(r[k]) for k in keys) + "\n")


if __name__ == "__main__":
    main()
