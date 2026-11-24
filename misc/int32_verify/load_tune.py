#!/usr/bin/env python3
"""load_tune.py - tune the DDR3 read taps under GEMM load on a brought-up card (gemmflow.ddr_tune; the flow does the
same with gemm_run.py --ddr-load-tune or the campaign key "ddr_load_tune").
  load_tune.py <build_dir> [--channels 0,1] [--span 4] [--passes 2] [--size 1024]"""
import argparse, json, os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "flow"))
from gemmflow.config import BuildCfg  # noqa: E402
from gemmflow.ddr_tune import tune  # noqa: E402
from gemmflow.runner import runner_cmd  # noqa: E402
ap = argparse.ArgumentParser()
ap.add_argument("build_dir"); ap.add_argument("--channels", default="0,1"); ap.add_argument("--span", type=int, default=4)
ap.add_argument("--passes", type=int, default=2); ap.add_argument("--size", type=int, default=1024)
a = ap.parse_args()
b = os.path.abspath(a.build_dir)
info = json.load(open(os.path.join(b, "config.resolved.json")))
res = tune(b, runner_cmd(BuildCfg.from_dict(info["config"]), b, info["config_hash"]),
           channels=tuple(int(c) for c in a.channels.split(",")), span=a.span, passes=a.passes, size=a.size)
print(json.dumps(res["taps"]))
