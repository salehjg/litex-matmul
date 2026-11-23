#!/usr/bin/env python3
#
# gemm_build.py - build everything for one JSON config (plan section 7): HLS -> LiteX/Vivado -> host tools.
#   .venv/bin/python scripts/gemm_build.py configs/gemm/default.json [--stages hls,soc,host] [--force] [--dry-run]
# Output: build/gemm/soc/<build_id>/ (config.json, manifest.json, reports/summary.json, logs/, stamps/, gateware/, sw/)
# and build/gemm/hls/<hls_hash[:12]>/. Unchanged steps are skipped (stamps); --force reruns the selected stages.
#

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "flow"))

from gemmflow.config import BuildCfg, ConfigError   # noqa: E402
from gemmflow.manifest import manifest              # noqa: E402
from gemmflow.steps import HlsStep, SocStep, HostStep  # noqa: E402
from gemmflow.tools import ToolError                # noqa: E402

STAGES = {"hls": HlsStep, "soc": SocStep, "host": HostStep}


def main():
    p = argparse.ArgumentParser(description="Build HLS kernel, SoC bitstream and host tools for one config.")
    p.add_argument("config")
    p.add_argument("--stages", default="hls,soc,host", help="Comma list of hls, soc, host (in this order).")
    p.add_argument("--force", action="store_true", help="Rerun the selected stages even if up to date.")
    p.add_argument("--dry-run", action="store_true", help="Show what would run.")
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

    os.makedirs(build_dir, exist_ok=True)
    summary = {"build_id": cfg.build_id(), "config_hash": cfg.config_hash(), "hls_hash": cfg.hls_hash(),
               "config": cfg.to_dict(), "manifest": manifest(), "steps": {}}
    for s in stages:
        step = STAGES[s](cfg, build_dir)
        if a.dry_run:
            print(f"  {s:5s}: {'up to date' if step.up_to_date() and not a.force else 'would run'}")
            continue
        print(f"  {s:5s}: ", end="", flush=True)
        try:
            seconds, skipped = step.run(force=a.force)
        except ToolError as e:
            print("FAILED")
            sys.exit(f"{s}: {e}")
        print("up to date" if skipped else f"done in {seconds:.0f} s", flush=True)
        summary["steps"][s] = {"seconds": round(seconds, 1), "skipped": skipped, **step.summary()}
    if not a.dry_run:
        os.makedirs(os.path.join(build_dir, "reports"), exist_ok=True)
        with open(os.path.join(build_dir, "manifest.json"), "w") as f:
            json.dump(summary["manifest"], f, indent=2)
        with open(os.path.join(build_dir, "reports", "summary.json"), "w") as f:
            json.dump(summary, f, indent=2)
        soc = summary["steps"].get("soc", {})
        if "wns_ns" in soc:
            print(f"timing: WNS {soc['wns_ns']} ns, WHS {soc['whs_ns']} ns ({'met' if soc['timing_met'] else 'FAILED'});"
                  f" LUT {soc.get('lut')}, FF {soc.get('ff')}, BRAM {soc.get('bram_tiles')}, DSP {soc.get('dsp')}")


if __name__ == "__main__":
    main()
