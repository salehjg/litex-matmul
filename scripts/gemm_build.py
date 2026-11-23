#!/usr/bin/env python3
#
# gemm_build.py - build everything for one JSON config (plan section 7): HLS -> LiteX/Vivado -> host tools.
#   .venv/bin/python scripts/gemm_build.py configs/gemm/default.json [--stages hls,soc,host] [--force] [--dry-run]
#   --report-only: run nothing, (re)write reports/summary.json from the outputs already there.
# Output: build/gemm/soc/<build_id>/ (config.json, manifest.json, reports/summary.json, logs/, stamps/, gateware/, sw/)
# and build/gemm/hls/<hls_hash[:12]>/. Unchanged steps are skipped (stamps); --force reruns the selected stages.
#

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "flow"))

from gemmflow.config import BuildCfg, ConfigError   # noqa: E402
from gemmflow.steps import HlsStep, SocStep, HostStep, write_build_reports  # noqa: E402
from gemmflow.tools import ToolError                # noqa: E402

STAGES = {"hls": HlsStep, "soc": SocStep, "host": HostStep}


def main():
    p = argparse.ArgumentParser(description="Build HLS kernel, SoC bitstream and host tools for one config.")
    p.add_argument("config")
    p.add_argument("--stages", default="hls,soc,host", help="Comma list of hls, soc, host (in this order).")
    p.add_argument("--force", action="store_true", help="Rerun the selected stages even if up to date.")
    p.add_argument("--dry-run", action="store_true", help="Show what would run.")
    p.add_argument("--report-only", action="store_true",
                   help="Run nothing; write reports/summary.json from the existing outputs.")
    a = p.parse_args()

    try:
        cfg = BuildCfg.load(a.config)
    except ConfigError as e:
        sys.exit(f"config error: {e}")
    for w in cfg.validate():
        print(f"warning: {w}")
    build_dir = os.path.join(ROOT, "build", "gemm", "soc", cfg.build_id())
    stages = [s.strip() for s in a.stages.split(",") if s.strip()]
    for s in stages:
        if s not in STAGES:
            sys.exit(f"unknown stage {s!r} (hls, soc, host)")
    print(f"build {cfg.build_id()}  hls {cfg.hls_hash()[:12]}  peak {cfg.peak_gflops():.1f} GFLOP/s  -> "
          f"{os.path.relpath(build_dir, ROOT)}")

    if a.report_only and not os.path.isdir(build_dir):
        sys.exit(f"no build at {os.path.relpath(build_dir, ROOT)}")
    os.makedirs(build_dir, exist_ok=True)
    steps = {}
    for s in stages:
        step = STAGES[s](cfg, build_dir)
        if a.dry_run:
            print(f"  {s:5s}: {'up to date' if step.up_to_date() and not a.force else 'would run'}")
            continue
        if a.report_only:
            try:
                steps[s] = {"seconds": 0.0, "skipped": True, "up_to_date": step.up_to_date(), **step.summary()}
            except (OSError, SyntaxError) as e:  # missing outputs (xml ParseError is a SyntaxError)
                steps[s] = {"seconds": 0.0, "skipped": True, "up_to_date": False, "error": str(e)}
            print(f"  {s:5s}: {'up to date' if steps[s]['up_to_date'] else 'NOT up to date'} (report only)")
            continue
        print(f"  {s:5s}: ", end="", flush=True)
        try:
            seconds, skipped = step.run(force=a.force)
        except ToolError as e:
            print("FAILED")
            sys.exit(f"{s}: {e}")
        print("up to date" if skipped else f"done in {seconds:.0f} s", flush=True)
        steps[s] = {"seconds": round(seconds, 1), "skipped": skipped, **step.summary()}
    if not a.dry_run:
        write_build_reports(cfg, build_dir, steps)
        soc = steps.get("soc", {})
        if "wns_ns" in soc:
            print(f"timing: WNS {soc['wns_ns']} ns, WHS {soc['whs_ns']} ns ({'met' if soc['timing_met'] else 'FAILED'});"
                  f" LUT {soc.get('lut')}, FF {soc.get('ff')}, BRAM {soc.get('bram_tiles')}, DSP {soc.get('dsp')}")


if __name__ == "__main__":
    main()
