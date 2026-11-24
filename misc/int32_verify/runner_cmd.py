#!/usr/bin/env python3
"""Print the gemm_runner base command line (runner, map, transpose, build id, dtype, multiples) for a build dir."""
import json, os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "flow"))
from gemmflow.config import BuildCfg  # noqa: E402
b = os.path.abspath(sys.argv[1])
info = json.load(open(os.path.join(b, "config.resolved.json")))
cfg = BuildCfg.from_dict(info["config"])
mm = cfg.memory_map
print(" ".join([os.path.join(b, "sw", "gemm_runner"), "--map", f"a={mm.a},b={mm.b},c={mm.c}",
                "--expect-build-id", info["config_hash"][:8], "--dtype", cfg.numeric().spec,
                "--multiples", ",".join(map(str, cfg.size_multiples()))] + (["--transposed-a"] if cfg.kernel.transposed_a else [])))
