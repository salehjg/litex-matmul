#!/usr/bin/env python3
"""sim.py - g++ simulation of kernels/gemm_mixed (its own testbench: random full-range codes, exact compare) for every
MAC / format combination and every C width conversion (gather, pass, split), with full and partial tiles.
Seconds to a minute per case; cases compile in parallel. Outputs in misc/gemm_mixed_sim/logs/ (git-ignored).
  .venv/bin/python misc/gemm_mixed_sim/sim.py [name-filter]"""
import json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "flow"))
from gemmflow.config import BuildCfg  # noqa: E402
from gemmflow.tools import read_config_sh  # noqa: E402
from gemmflow.hls import HLSLIB_INCLUDE  # noqa: E402

CASES = [  # name, (A type, A bits), (B type, B bits), C bits, PN, PM, tile_n, tile_m, transposed A
    ("i8xds3c32 PM32 split2",   ("int", 8),  ("pow2", 3), 32, 8, 32, 64, 256, True),
    ("i8xds3c32 PM16 pass",     ("int", 8),  ("pow2", 3), 32, 8, 16, 64, 128, True),
    ("i8xds3c32 PM8 gather2",   ("int", 8),  ("pow2", 3), 32, 8, 8, 64, 128, True),
    ("i8xds3c32 PM64 split4",   ("int", 8),  ("pow2", 3), 32, 4, 64, 64, 1024, True),
    ("i8xds2c32 PM32",          ("int", 8),  ("pow2", 2), 32, 8, 32, 64, 256, True),
    ("i8xds4c32 PM32",          ("int", 8),  ("pow2", 4), 32, 8, 32, 64, 256, True),
    ("u8xds3c32 PM32",          ("uint", 8), ("pow2", 3), 32, 8, 32, 64, 256, True),
    ("i4xds3c16 PM32 pass",     ("int", 4),  ("pow2", 3), 16, 8, 32, 128, 512, True),
    ("i16xds4c32 PM32",         ("int", 16), ("pow2", 4), 32, 8, 32, 64, 256, True),
    ("i8xi8c32 PM32 multiply",  ("int", 8),  ("int", 8),  32, 8, 32, 64, 256, True),
    ("i8xds3c16 PM64 wrap",     ("int", 8),  ("pow2", 3), 16, 4, 64, 64, 1024, True),
    ("i8xds3c32 PM32 notA",     ("int", 8),  ("pow2", 3), 32, 8, 32, 64, 256, False),
]
K = 48

base = json.load(open(os.path.join(ROOT, "configs", "gemm", "default.json")))
settings = read_config_sh().get("VIVADO_SETTINGS", "")
vitis_inc = os.path.join(os.path.dirname(os.path.dirname(settings)), "Vitis", "include")
flt = sys.argv[1] if len(sys.argv) > 1 else ""


def run(case):
    name, (at, ab), (bt, bb), cb, pn, pm, tn, tm, ta = case
    d = json.loads(json.dumps(base))
    d["kernel"].update(data_type=at, data_bits=ab, b_type=bt, b_bits=bb, accum_bits=cb, parallelism_n=pn,
                       parallelism_m=pm, memory_tile_size_n=tn, memory_tile_size_m=tm, transposed_a=ta)
    cfg = BuildCfg.from_dict(d)
    cfg.validate()
    out = os.path.join(ROOT, "misc", "gemm_mixed_sim", "logs", name.replace(" ", "_"))
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, "Config.h"), "w").write(cfg.render_config_h())
    kd = cfg.kernel_dir()
    flags = ["-std=c++17", "-O2", "-DMM_DYNAMIC_SIZES", "-DHLSLIB_STREAM_TIMEOUT=16", "-DHLSLIB_LEGACY_SDX=0",
             f"-I{out}", f"-I{os.path.join(kd, 'include')}", f"-I{HLSLIB_INCLUDE}", "-isystem", vitis_inc,
             "-pthread", "-w", "-DAP_INT_MAX_W=4096"] + (["-DMM_TRANSPOSED_A"] if ta else [])
    srcs = [os.path.join(kd, "tb", "test_gemm.cpp")] + [os.path.join(kd, "src", s) for s in ("Top.cpp", "Memory.cpp", "Compute.cpp")]
    exe = os.path.join(out, "test_gemm")
    p = subprocess.run(["g++", *flags, "-o", exe, *srcs], capture_output=True, text=True)
    if p.returncode:
        open(os.path.join(out, "compile.log"), "w").write(p.stdout + p.stderr)
        return f"{name:26s} COMPILE FAILED: " + (p.stdout + p.stderr).strip().splitlines()[0][:160]
    mn, mk, mm = cfg.size_multiples()
    k = -(-K // mk) * mk
    lines = []
    for n, m in ((2 * tn, 2 * tm), (tn + mn, tm + mm)):  # whole tiles, then partial tiles in N and M
        try:
            p = subprocess.run([exe, str(n), str(k), str(m)], capture_output=True, text=True, timeout=300)
            res = (p.stdout + p.stderr).strip().splitlines()
            lines.append(f"{n}x{k}x{m} exit {p.returncode}: {res[-1][:110] if res else ''}")
        except subprocess.TimeoutExpired:
            lines.append(f"{n}x{k}x{m} hung (300 s)")
    return f"{name:26s} " + " || ".join(lines)


cases = [c for c in CASES if flt in c[0]]
with ThreadPoolExecutor(6) as ex:
    for r in ex.map(run, cases):
        print(r, flush=True)
