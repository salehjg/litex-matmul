#
# runner.py - the gemm_runner command line for a build (shared by gemm_dse.py, gemm_run.py and ddr_tune).
#

import os


def runner_cmd(cfg, build_dir, config_hash=None):
    """Base gemm_runner arguments: the build's memory map, build id check, element format, size multiples, A layout
    (mixed formats: --b-dtype / --c-dtype too); float32 is checked bit for bit (--float-exact). Add --n/--k/--m, --verify, --seed, --reps, --json as needed."""
    mm = cfg.memory_map
    nf = cfg.numeric()
    return ([os.path.join(build_dir, "sw", "gemm_runner"), "--map", f"a={mm.a},b={mm.b},c={mm.c}",
             "--expect-build-id", (config_hash or cfg.config_hash())[:8], *nf.runner_args(),
             "--multiples", ",".join(map(str, cfg.size_multiples()))] +
            (["--float-exact"] if nf.kind == "float" else []) +
            (["--transposed-a"] if cfg.kernel.transposed_a else []))
