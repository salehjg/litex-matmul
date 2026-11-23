#!/usr/bin/env python3
#
# bist_probe.py - run LiteDRAM BIST generator/checker runs of chosen length at chosen addresses through the driver
# (no CPU needed), to tell continuous-traffic correctness from isolated-access correctness. Debug case for the
# GemmSoC DDR round-trip failure (docs/notes1/gemm-hls-progress.md, phase 2).
#   misc/dma_debug/bist_probe.py --build-dir build/gemm_soc --channel 1 --length 64 --addr 0 0x1000 0x12340
#
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from csr_peek import CSRMap, DriverBus  # noqa: E402


def run(csrs, bus, prefix, base, length, random_data):
    def w(name, v): csrs.write(bus, f"{prefix}_{name}", v)
    def r(name): return csrs.read(bus, f"{prefix}_{name}")
    results = {}
    for unit in ("generator", "checker"):
        u = f"{unit}"
        w(f"{u}_reset", 1); w(f"{u}_reset", 0)
        w(f"{u}_base", base); w(f"{u}_end", base + length); w(f"{u}_length", length)
        w(f"{u}_random", 1 if random_data else 0)
        w(f"{u}_start", 1)
        t0 = time.time()
        while not r(f"{u}_done"):
            if time.time() - t0 > 5:
                raise RuntimeError(f"{prefix}_{u} timeout")
        if unit == "checker":
            results["errors"] = r("checker_errors")
    return results["errors"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--build-dir", default="build/gemm_soc")
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--length", type=lambda x: int(x, 0), default=64, help="Bytes per BIST run.")
    p.add_argument("--random-data", action="store_true")
    p.add_argument("--addr", nargs="+", type=lambda x: int(x, 0), default=[0])
    a = p.parse_args()
    csrs = CSRMap(os.path.join(a.build_dir, "csr.csv"))
    bus = DriverBus()
    prefix = "sdram" if a.channel == 0 else "sdram1"
    total = 0
    for base in a.addr:
        e = run(csrs, bus, prefix, base, a.length, a.random_data)
        total += e
        print(f"ch{a.channel} base 0x{base:08x} length {a.length:>9} random_data={int(a.random_data)}: {e} errors")
    print(f"total errors: {total}")


if __name__ == "__main__":
    main()
