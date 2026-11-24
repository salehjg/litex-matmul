#!/usr/bin/env python3
"""Print the gemm_runner base command line for a build dir (gemmflow.runner.runner_cmd)."""
import json, os, shlex, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "flow"))
from gemmflow.config import BuildCfg  # noqa: E402
from gemmflow.runner import runner_cmd  # noqa: E402
b = os.path.abspath(sys.argv[1])
info = json.load(open(os.path.join(b, "config.resolved.json")))
print(shlex.join(runner_cmd(BuildCfg.from_dict(info["config"]), b, info["config_hash"])))
