#!/usr/bin/env python3
#
# gemm_dse.py - design-space exploration and benchmarking (plan section 9).
#   .venv/bin/python scripts/gemm_dse.py configs/dse/<campaign>.json [--stages sim,hls,build,run] [--resume] [--dry-run]
#
# A campaign has a base build config, a build grid (dotted config keys -> value lists, full cross product; a key starting
# with "@" is a linked axis whose values are objects of dotted keys set together, e.g. "@format": [{"kernel.data_type":
# "int", "kernel.data_bits": 8}, ...] or a PE count with its matching tile), a runtime
# grid (sizes, repetitions, warmup, verify) and stages:
#   sim   : g++ simulation of the kernel per point (parallel, seconds; float32 and native ints, others "skipped")
#   hls   : Vitis HLS of every valid point (parallel, sized by free RAM and cores) -> estimates, no Vivado
#   build : LiteX + Vivado + host tools per point, one at a time (15 GB RAM)
#   run   : per built point: JTAG load + bring-up, then gemm_runner for every size
# Results in results/<campaign>/: campaign.json (the latest invocation), invocations.jsonl (every invocation: time,
# argv, stages, git state), builds.jsonl/.csv (one row per point and stage), runs.jsonl/.csv (one row per point, size
# and repetition; build_id = full build id, hw_build_id = the id read from the bitstream). Invalid points are
# recorded with status "invalid" and the reason.
# Element formats (docs/notes2): sweep kernel.data_type / data_bits / frac_bits like any key. Every row carries the
# format columns (dtype, dtype_kind, dtype_bits, dtype_signed, dtype_frac_bits, a/b/c/acc_dtype, quant_*, pow2_*),
# runs also gops / peak_gops / pct_of_peak (gflops / peak_gflops for float).
# --resume skips stages/sizes already recorded as ok for a point (and the bring-up of a point with nothing left).
# Each build also gets <build>/reports/summary.json (+ manifest.json when a step ran), as with gemm_build.py.
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

from gemmflow.board import Board, board_lock           # noqa: E402
from gemmflow.config import DEVICE, BuildCfg, ConfigError  # noqa: E402
from gemmflow.manifest import manifest                 # noqa: E402
from gemmflow.results import ResultStore               # noqa: E402
from gemmflow.runner import runner_cmd                 # noqa: E402
from gemmflow.steps import HlsStep, SimStep, SocStep, HostStep, write_build_reports  # noqa: E402
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
            if k.startswith("@"):  # linked axis: each value sets several dotted keys together
                for kk, vv in v.items():
                    set_dotted(d, kk, vv)
            else:
                set_dotted(d, k, v)
        try:
            d["name"] = BuildCfg.from_dict(d).auto_name()  # name from the actual parameters
        except (ConfigError, KeyError, TypeError, ValueError):
            label = lambda k, v: ("".join(f"{kk.split('.')[-1]}{vv}" for kk, vv in v.items()) if k.startswith("@")
                                  else f"{k.split('.')[-1]}{v}")
            d["name"] = base.get("name", "gemm") + "_" + "_".join(label(k, v) for k, v in zip(keys, values))
        points.append(d)
    return points


def sizes_of(rt):
    if rt.get("triples"):
        return [tuple(t) for t in rt["triples"]]
    return list(itertools.product(rt.get("n", [256]), rt.get("k", [256]), rt.get("m", [256])))


SOC_BRAM18 = 150  # GemmSoC without the kernel: 73.5 BRAM36 tiles (docs/notes1 measurements)


def over_budget(h):
    """Reason string if the HLS estimate already exceeds the device (BRAM18 incl. the SoC, or DSP), else None. HLS
    LUT estimates run ~45 % above Vivado's for this kernel, so LUTs are not gated."""
    bram, dsp = h.get("bram_18k") or 0, h.get("dsp") or 0
    why = []
    if bram + SOC_BRAM18 > 2 * DEVICE["bram36"]:
        why.append(f"BRAM18 {bram} + {SOC_BRAM18} (SoC) > {2 * DEVICE['bram36']}")
    if dsp > DEVICE["dsp"]:
        why.append(f"DSP {dsp} > {DEVICE['dsp']}")
    return "; ".join(why) or None


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
    if not a.dry_run:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "campaign.json"), "w") as f:
            json.dump({"campaign": camp, "stages": stages, "manifest": manifest()}, f, indent=2)
        with open(os.path.join(out, "invocations.jsonl"), "a") as f:
            f.write(json.dumps({"timestamp": now(), "argv": sys.argv[1:], "stages": stages, "manifest": manifest()}) + "\n")

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
                k = d.get("kernel", {})
                builds.append({"campaign": name, "status": "invalid", "stage": "config", "name": d["name"],
                               "reason": str(e), "timestamp": now(), "data_type": k.get("data_type"),
                               "data_bits": k.get("data_bits"), "frac_bits": k.get("frac_bits"), "config": d})
    print(f"campaign {name}: {len(cfgs)} valid point(s), stages {','.join(stages)}")
    done = {(r.get("build_id"), r.get("stage")) for r in builds.rows() if r.get("status") == "ok"} if a.resume else set()
    if a.dry_run:
        for cfg, w in cfgs:
            print(f"  {cfg.build_id()}  {cfg.numeric().spec:<10s} peak {cfg.peak_gops():.1f} GOP/s"
                  + (f"  warnings: {w}" if w else ""))
        return

    def record(cfg, stage, status, extra):
        builds.append({"campaign": name, "status": status, "stage": stage, "build_id": cfg.build_id(),
                       "config_hash": cfg.config_hash(), "hls_hash": cfg.hls_hash(), "timestamp": now(),
                       "git_sha": manifest()["git_sha"], "git_dirty": manifest()["git_dirty"],
                       **cfg.numeric().columns(), "peak_gops": cfg.peak_gops(), "config": cfg.to_dict(), **extra})

    # Simulation (parallel, optional) ----------------------------------------------------------------------------
    if "sim" in stages:
        todo = [c for c, _ in cfgs if (c.build_id(), "sim") not in done]
        print(f"sim: {len(todo)} point(s)")

        def sim_one(cfg):
            st = SimStep(cfg)
            ok, why = st.supported()
            if not ok:
                return cfg, "skipped", {"reason": why}
            try:
                seconds, skipped = st.run()
                return cfg, "ok", {"seconds": round(seconds, 1), "skipped": skipped, "sim": st.summary()}
            except (ToolError, OSError, subprocess.TimeoutExpired) as e:
                return cfg, "failed", {"reason": str(e)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, (os.cpu_count() or 2) // 2))) as pool:
            for cfg, status, extra in pool.map(sim_one, todo):
                record(cfg, "sim", status, extra)
                print(f"  {cfg.build_id()}: {status}  {extra.get('reason', '')}")
        builds.write_csv()

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
                      f"BRAM18 {h.get('bram_18k')}  LUT {h.get('lut')}  AXI {h.get('axi_data_bits')}"
                      + (f"  {extra['reason'][:160]}" if "reason" in extra else ""))
        builds.write_csv()

    # Builds (sequential) ----------------------------------------------------------------------------------------
    built = []
    for cfg, _ in cfgs:
        bdir = os.path.join(ROOT, "build", "gemm", "soc", cfg.build_id())
        if "build" in stages and (cfg.build_id(), "build") not in done:
            print(f"build {cfg.build_id()}:", flush=True)
            try:
                summary, over = {}, None
                for Step in (HlsStep, SocStep, HostStep):
                    st = Step(cfg, bdir)
                    seconds, skipped = st.run()
                    summary[Step.name] = {"seconds": round(seconds, 1), "skipped": skipped, **st.summary()}
                    print(f"  {Step.name}: {'up to date' if skipped else f'{seconds:.0f} s'}", flush=True)
                    if Step is HlsStep and (over := over_budget(summary["hls"])):
                        break  # does not fit: no Vivado run
                if over:
                    record(cfg, "build", "over_budget", {"reason": over, **summary})
                    print(f"  over budget (HLS estimate): {over}")
                    builds.write_csv()
                    continue
                write_build_reports(cfg, bdir, summary)
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
        # Keyed on config_hash: rows written before 2026-11-23 22:40 carry the 8-hex hardware id as build_id.
        have = {(r["config_hash"], r["n"], r["k"], r["m"], r["rep"]) for r in runs.rows()
                if r.get("status") == "ok"} if a.resume else set()
        reps = rt.get("repetitions", 1)
        timing_ok = {r["build_id"] for r in builds.rows() if r.get("stage") == "build" and r.get("status") == "ok"}
        for cfg, bdir in built:
            if cfg.build_id() not in timing_ok:
                print(f"run {cfg.build_id()}: skipped (no build with timing met)")
                continue
            todo = [(n, k, m) for n, k, m in sizes_of(rt)
                    if not all((cfg.config_hash(), n, k, m, r) in have for r in range(reps))]
            if not todo:
                print(f"run {cfg.build_id()}: all sizes already recorded")
                continue
            print(f"run {cfg.build_id()}: bring-up", flush=True)
            with board_lock(log=lambda s: print(f"  {s}", flush=True)):  # the card, for this build's bring-up and sizes
                tune = bool(camp.get("ddr_load_tune"))  # campaign key: DDR read taps tuned under load (ddr_tune)
                try:
                    tuned = Board(bdir).bring_up(program=True, log=lambda s: print(f"  [bring-up] {s}", flush=True),
                                                 tune_runner=runner_cmd(cfg, bdir) if tune else None)
                except (ToolError, OSError) as e:
                    print(f"  bring-up failed: {e}  (stopping: the card may need a rescan or a warm reboot; --resume)")
                    runs.write_csv()
                    sys.exit(1)
                ddr = {"ddr_load_tuned": tune, **({"ddr_tune_bad_before": tuned["start_bad"],
                                                    "ddr_tune_bad_after": tuned["end_bad"]} if tuned else {})}
                for n, k, m in todo:
                    cmd = runner_cmd(cfg, bdir) + ["--n", str(n), "--k", str(k), "--m", str(m), "--reps", str(reps),
                                                   "--warmup", str(rt.get("warmup", 0)),
                                                   "--verify", rt.get("verify", "sampled"), "--json"]
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
                    for line in proc.stdout.splitlines():
                        if line.startswith("{"):
                            row = json.loads(line)
                            nf = cfg.numeric()
                            gops = row.get("gops", row.get("gflops"))
                            status = "ok" if row["verify_bad"] == 0 else "verify_failed"
                            if row.get("dtype", nf.spec) != nf.spec:
                                status = "dtype_mismatch"
                            extra = {"peak_gflops": cfg.peak_gops()} if nf.kind == "float" else {}
                            runs.append({"campaign": name, "status": status, "config_hash": cfg.config_hash(),
                                         "hls_hash": cfg.hls_hash(), "timestamp": now(),
                                         "git_sha": manifest()["git_sha"],
                                         "git_dirty": manifest()["git_dirty"], "peak_gops": cfg.peak_gops(), **extra,
                                         "pct_of_peak": round(100 * gops / cfg.peak_gops(), 2),
                                         "config": cfg.to_dict(), **row, **nf.columns(), **ddr,
                                         "build_id": cfg.build_id(), "hw_build_id": row.get("build_id")})
                            unit = "GFLOP/s" if nf.kind == "float" else "GOP/s"
                            print(f"  {n}x{k}x{m} {nf.spec} rep {row['rep']}: {gops:.2f} {unit}, verify {status}")
                    if proc.returncode not in (0, 1):
                        print(f"  {n}x{k}x{m}: gemm_runner error {proc.returncode}: {proc.stderr.strip()[-300:]}")
            runs.write_csv()
    print(f"results in {os.path.relpath(out, ROOT)}/")


if __name__ == "__main__":
    main()
