#!/usr/bin/env python3
#
# gemm_run.py - run the GEMM kernel of one build on the card for a list of sizes (plan section 4.7).
#   .venv/bin/python scripts/gemm_run.py build/gemm_soc_<build_id> --sizes 256x256x256 1024x1024x1024 --reps 3
#   options: --program (JTAG load + rescan first), --no-bringup (driver/DDR/tools already done), --verify full|sampled|off
# Brings the board up in the right order (gemmflow.board), then calls the build's gemm_runner with the build's memory
# map, element format (--dtype) and build id, and appends one JSON row per repetition (plus run metadata and the
# element-format columns of docs/notes2) to results/<build_id>/runs.jsonl.
#

import argparse
import datetime
import json
import os
import platform
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "flow"))

from gemmflow.board import Board, board_lock  # noqa: E402
from gemmflow.config import BuildCfg      # noqa: E402


def git_rev():
    sha = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", ROOT, "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
    return sha, bool(dirty)


def load_build_info(build_dir, config_path):
    path = os.path.join(build_dir, "config.resolved.json")
    if os.path.exists(path):
        with open(path) as f:
            info = json.load(f)
        cfg = BuildCfg.from_dict(info["config"])
        return cfg, info
    if not config_path:
        sys.exit(f"{path} missing: pass --config <the build's JSON config>")
    cfg = BuildCfg.load(config_path)
    return cfg, {"build_id": cfg.build_id(), "config_hash": cfg.config_hash(), "hls_hash": cfg.hls_hash(),
                 "config": cfg.to_dict()}


def main():
    p = argparse.ArgumentParser(description="Run the GEMM kernel of one build for a list of sizes.")
    p.add_argument("build_dir")
    p.add_argument("--config", help="Build config JSON (only if the build has no config.resolved.json).")
    p.add_argument("--sizes", nargs="+", default=["256x256x256"], help="NxKxM, e.g. 1024x1024x1024")
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--warmup", type=int, default=0)
    p.add_argument("--verify", default="sampled", choices=["full", "sampled", "off"])
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--program", action="store_true", help="JTAG load + rescan before the rest of the bring-up.")
    p.add_argument("--no-bringup", action="store_true", help="Skip driver/DDR/tools (already done for this load).")
    p.add_argument("--results", help="JSONL file (default results/<build_id>/runs.jsonl).")
    a = p.parse_args()

    build_dir = os.path.abspath(a.build_dir)
    cfg, info = load_build_info(build_dir, a.config)
    with board_lock():
        run(a, build_dir, cfg, info)


def run(a, build_dir, cfg, info):
    if not a.no_bringup:
        Board(build_dir).bring_up(program=a.program, log=lambda s: print(f"[bring-up] {s}", flush=True))

    runner = os.path.join(build_dir, "sw", "gemm_runner")
    mm = cfg.memory_map
    results = a.results or os.path.join(ROOT, "results", info["build_id"], "runs.jsonl")
    os.makedirs(os.path.dirname(results), exist_ok=True)
    sha, dirty = git_rev()
    nf = cfg.numeric()
    meta = {"schema_version": 2, "build_id": info["build_id"], "config_hash": info["config_hash"],
            "hls_hash": info["hls_hash"], "git_sha": sha, "git_dirty": dirty, "host": platform.node(),
            "kernel_release": platform.release(), "kernel": info["config"]["kernel"], "clocks": info["config"]["clocks"],
            "memory_map": info["config"]["memory_map"], **nf.columns(), "peak_gops": cfg.peak_gops()}
    failures = 0
    for size in a.sizes:
        n, k, m = (int(x) for x in size.lower().split("x"))
        cmd = [runner, "--n", str(n), "--k", str(k), "--m", str(m), "--reps", str(a.reps), "--warmup", str(a.warmup),
               "--seed", str(a.seed), "--verify", a.verify, "--map", f"a={mm.a},b={mm.b},c={mm.c}",
               "--expect-build-id", info["config_hash"][:8], "--dtype", nf.spec, "--json"] + \
              (["--float-exact"] if nf.kind == "float" else []) + \
              (["--transposed-a"] if cfg.kernel.transposed_a else []) + \
              ["--multiples", ",".join(map(str, cfg.size_multiples()))]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        for line in proc.stdout.splitlines():
            if line.startswith("{"):
                row = json.loads(line)
                if row.get("dtype", nf.spec) != nf.spec:
                    print(f"[{size}] runner reports dtype {row.get('dtype')}, build is {nf.spec}", flush=True)
                    failures += 1
                row.update(meta)
                row["pct_of_peak"] = round(100 * row.get("gops", row.get("gflops", 0)) / cfg.peak_gops(), 2)
                row["timestamp"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
                with open(results, "a") as f:
                    f.write(json.dumps(row) + "\n")
            else:
                print(line, flush=True)
        if proc.returncode != 0:
            failures += 1
            print(f"[{size}] gemm_runner exit {proc.returncode}: {proc.stderr.strip()[-400:]}", flush=True)
    print(f"results appended to {os.path.relpath(results, ROOT)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
