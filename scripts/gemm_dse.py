#!/usr/bin/env python3
#
# gemm_dse.py - design-space exploration and benchmarking (plan section 9).
#   .venv/bin/python scripts/gemm_dse.py configs/dse/<campaign>.json [--stages hls,build,run] [--resume] [--dry-run]
#
# A campaign has a base build config, a build grid (dotted config keys -> value lists, full cross product), a runtime
# grid (sizes, repetitions, warmup, verify) and stages:
#   hls   : Vitis HLS of every valid point (parallel, sized by free RAM and cores) -> estimates, no Vivado
#   build : LiteX + Vivado + host tools per point, one at a time (15 GB RAM)
#   run   : per built point: JTAG load + bring-up, then gemm_runner for every size
# Results in results/<campaign>/: campaign.json, builds.jsonl/.csv (one row per point and stage), runs.jsonl/.csv (one
# row per point, size and repetition). Invalid points are recorded with status "invalid" and the reason.
# --resume skips stages/sizes already recorded as ok for a point.
#

import argparse
import concurrent.futures
import copy
import datetime
import itertools
import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "flow"))

from gemmflow.board import Board                       # noqa: E402
from gemmflow.config import BuildCfg, ConfigError      # noqa: E402
from gemmflow.manifest import manifest                 # noqa: E402
from gemmflow.results import ResultStore               # noqa: E402
from gemmflow.steps import HlsStep, SocStep, HostStep  # noqa: E402
from gemmflow.tools import ToolError                   # noqa: E402


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def set_dotted(d, key, value):
    parts = key.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def expand(campaign):
    with open(os.path.join(ROOT, campaign["base"])) as f:
        base = json.load(f)
    grid = campaign.get("build_grid", {})
    keys = list(grid)
    points = []
    for values in itertools.product(*[grid[k] for k in keys]) if keys else [()]:
        d = copy.deepcopy(base)
        for k, v in zip(keys, values):
            set_dotted(d, k, v)
        try:
            d["name"] = BuildCfg.from_dict(d).auto_name()  # name from the actual parameters
        except (ConfigError, KeyError, TypeError):
            d["name"] = base.get("name", "gemm") + "_" + "_".join(f"{k.split('.')[-1]}{v}" for k, v in zip(keys, values))
        points.append(d)
    return points


def sizes_of(rt):
    if rt.get("triples"):
        return [tuple(t) for t in rt["triples"]]
    return list(itertools.product(rt.get("n", [256]), rt.get("k", [256]), rt.get("m", [256])))


def hls_jobs(spec):
    if spec != "auto":
        return int(spec)
    try:
        with open("/proc/meminfo") as f:
            avail_gb = int([l for l in f if l.startswith("MemAvailable")][0].split()[1]) / 2**20
    except (OSError, IndexError):
        avail_gb = 4
    return max(1, min((os.cpu_count() or 4) // 4, int(avail_gb // 3)))


def main():
    p = argparse.ArgumentParser(description="GEMM design-space exploration and benchmarking.")
    p.add_argument("campaign")
    p.add_argument("--stages", help="Override the campaign's stages (comma list of hls, build, run).")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    with open(a.campaign) as f:
        camp = json.load(f)
    name = camp["campaign"]
    stages = (a.stages.split(",") if a.stages else camp.get("stages", ["hls"]))
    out = os.path.join(ROOT, "results", name)
    builds = ResultStore(os.path.join(out, "builds.jsonl"))
    runs = ResultStore(os.path.join(out, "runs.jsonl"))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "campaign.json"), "w") as f:
        json.dump({"campaign": camp, "stages": stages, "manifest": manifest()}, f, indent=2)

    # Expand + validate ------------------------------------------------------------------------------------------
    cfgs = []
    for d in expand(camp):
        try:
            cfg = BuildCfg.from_dict(d)
            warnings = cfg.validate()
            cfgs.append((cfg, warnings))
        except ConfigError as e:
            print(f"invalid point {d['name']}: {e}")
            if not a.dry_run:
                builds.append({"campaign": name, "status": "invalid", "stage": "config", "name": d["name"],
                               "reason": str(e), "timestamp": now(), "config": d})
    print(f"campaign {name}: {len(cfgs)} valid point(s), stages {','.join(stages)}")
    done = {(r.get("build_id"), r.get("stage")) for r in builds.rows() if r.get("status") == "ok"} if a.resume else set()
    if a.dry_run:
        for cfg, w in cfgs:
            print(f"  {cfg.build_id()}  peak {cfg.peak_gflops():.1f} GFLOP/s" + (f"  warnings: {w}" if w else ""))
        return

    def record(cfg, stage, status, extra):
        builds.append({"campaign": name, "status": status, "stage": stage, "build_id": cfg.build_id(),
                       "config_hash": cfg.config_hash(), "hls_hash": cfg.hls_hash(), "timestamp": now(),
                       "git_sha": manifest()["git_sha"], "git_dirty": manifest()["git_dirty"],
                       "config": cfg.to_dict(), **extra})

    # HLS (parallel) ---------------------------------------------------------------------------------------------
    if "hls" in stages:
        todo = [(c, w) for c, w in cfgs if (c.build_id(), "hls") not in done]
        jobs = hls_jobs(camp.get("jobs", {}).get("hls", "auto"))
        print(f"hls: {len(todo)} point(s), {jobs} in parallel")

        def hls_one(cfg):
            step = HlsStep(cfg, os.path.join(ROOT, "build", "gemm", "soc", cfg.build_id()))
            try:
                seconds, skipped = step.run()
                return cfg, "ok", {"seconds": round(seconds, 1), "skipped": skipped, "hls": step.summary()}
            except (ToolError, OSError) as e:
                return cfg, "failed", {"reason": str(e)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            for cfg, status, extra in pool.map(hls_one, [c for c, _ in todo]):
                record(cfg, "hls", status, extra)
                h = extra.get("hls", {})
                print(f"  {cfg.build_id()}: {status}  est {h.get('estimated_clock_ns')} ns  DSP {h.get('dsp')}  "
                      f"BRAM18 {h.get('bram_18k')}  LUT {h.get('lut')}")
        builds.write_csv()

    # Builds (sequential) ----------------------------------------------------------------------------------------
    built = []
    for cfg, _ in cfgs:
        bdir = os.path.join(ROOT, "build", "gemm", "soc", cfg.build_id())
        if "build" in stages and (cfg.build_id(), "build") not in done:
            print(f"build {cfg.build_id()}:", flush=True)
            try:
                summary = {}
                for Step in (HlsStep, SocStep, HostStep):
                    st = Step(cfg, bdir)
                    seconds, skipped = st.run()
                    summary[Step.name] = {"seconds": round(seconds, 1), "skipped": skipped, **st.summary()}
                    print(f"  {Step.name}: {'up to date' if skipped else f'{seconds:.0f} s'}", flush=True)
                soc = summary["soc"]
                status = "ok" if soc.get("timing_met") else "timing_failed"
                record(cfg, "build", status, summary)
                print(f"  WNS {soc.get('wns_ns')} WHS {soc.get('whs_ns')} -> {status}")
            except (ToolError, OSError) as e:
                record(cfg, "build", "failed", {"reason": str(e)})
                print(f"  failed: {e}")
            builds.write_csv()
        if os.path.exists(os.path.join(bdir, "sw", "gemm_runner")):
            built.append((cfg, bdir))

    # Runs -----------------------------------------------------------------------------------------------------
    if "run" in stages:
        rt = camp.get("runtime_grid", {})
        have = {(r["build_id"], r["n"], r["k"], r["m"], r["rep"]) for r in runs.rows()} if a.resume else set()
        timing_ok = {r["build_id"] for r in builds.rows() if r.get("stage") == "build" and r.get("status") == "ok"}
        for cfg, bdir in built:
            if cfg.build_id() not in timing_ok:
                print(f"run {cfg.build_id()}: skipped (no build with timing met)")
                continue
            print(f"run {cfg.build_id()}: bring-up", flush=True)
            try:
                Board(bdir).bring_up(program=True, log=lambda s: print(f"  [bring-up] {s}", flush=True))
            except (ToolError, OSError) as e:
                print(f"  bring-up failed: {e}  (stopping: the card may need a rescan or a warm reboot; --resume)")
                runs.write_csv()
                sys.exit(1)
            mm = cfg.memory_map
            for n, k, m in sizes_of(rt):
                if all((cfg.build_id(), n, k, m, r) in have for r in range(rt.get("repetitions", 1))):
                    continue
                cmd = [os.path.join(bdir, "sw", "gemm_runner"), "--n", str(n), "--k", str(k), "--m", str(m),
                       "--reps", str(rt.get("repetitions", 1)), "--warmup", str(rt.get("warmup", 0)),
                       "--verify", rt.get("verify", "sampled"), "--map", f"a={mm.a},b={mm.b},c={mm.c}",
                       "--expect-build-id", cfg.config_hash()[:8], "--json"]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
                for line in proc.stdout.splitlines():
                    if line.startswith("{"):
                        row = json.loads(line)
                        status = "ok" if row["verify_bad"] == 0 else "verify_failed"
                        runs.append({"campaign": name, "status": status, "config_hash": cfg.config_hash(),
                                     "hls_hash": cfg.hls_hash(), "timestamp": now(), "git_sha": manifest()["git_sha"],
                                     "git_dirty": manifest()["git_dirty"], "peak_gflops": cfg.peak_gflops(),
                                     "pct_of_peak": round(100 * row["gflops"] / cfg.peak_gflops(), 2),
                                     "config": cfg.to_dict(), **row})
                        print(f"  {n}x{k}x{m} rep {row['rep']}: {row['gflops']:.2f} GFLOP/s, verify {status}")
                if proc.returncode not in (0, 1):
                    print(f"  {n}x{k}x{m}: gemm_runner error {proc.returncode}: {proc.stderr.strip()[-300:]}")
            runs.write_csv()
    print(f"results in {os.path.relpath(out, ROOT)}/")


if __name__ == "__main__":
    main()
