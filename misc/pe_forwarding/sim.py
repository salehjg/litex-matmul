#!/usr/bin/env python3
"""sim.py - g++ simulation of the kernel (upstream testbench, exact int32 compare) for tiles that break or meet the
two tile rules the upstream kernel does not check (flow/gemmflow/config.py): tile_m/PM >= PN (A forwarding; int4
PE 48x64 tile 768x2048 gave wrong C on the card, then hung) and tile_m/PM, tile_n/PN powers of two (WriteC counters). Small arrays with the same ratios, seconds per case. Outputs in misc/pe_forwarding/logs/ (git-ignored).
  misc/pe_forwarding/sim.py"""
import json, os, subprocess, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "flow"))
from gemmflow.config import BuildCfg, KERNEL_DIR  # noqa: E402
from gemmflow.tools import read_config_sh  # noqa: E402
from gemmflow.hls import HLSLIB_INCLUDE  # noqa: E402

CASES = [  # name, PN, PM, tile_n, tile_m, transposed A
    # rule 1: tile_m/PM >= PN (A forwarding along the PE chain)
    ("PN8 PM8 t32x32  (tile_m/PM 4 < PN 8)",          8, 8, 32, 32, False),
    ("PN8 PM8 t32x64  (tile_m/PM 8 = PN 8)",          8, 8, 32, 64, False),
    ("PN8 PM8 t32x32 tA (4 < 8)",                     8, 8, 32, 32, True),
    ("PN8 PM8 t32x64 tA (8 = 8)",                     8, 8, 32, 64, True),
    ("PN12 PM8 t48x64 (tile_m/PM 8 < PN 12)",        12, 8, 48, 64, False),
    ("PN12 PM8 t48x128 (16 >= 12, powers of 2)",     12, 8, 48, 128, False),
    # rule 2: tile_m/PM and tile_n/PN powers of two (WriteC counters ap_uint<ConstLog2(x)>, floor)
    ("PN12 PM8 t48x96 (tile_m/PM 12 = PN, not 2^k)", 12, 8, 48, 96, False),
    ("PN8 PM8 t32x96  (tile_m/PM 12, not 2^k)",       8, 8, 32, 96, False),
    ("PN8 PM8 t96x128 (tile_n/PN 12, not 2^k)",       8, 8, 96, 128, False),
    ("PN8 PM8 t64x128 (8, 16)",                       8, 8, 64, 128, False),
]
base = json.load(open(os.path.join(ROOT, "configs", "gemm", "default.json")))
settings = read_config_sh().get("VIVADO_SETTINGS", "")
vitis_inc = os.path.join(os.path.dirname(os.path.dirname(settings)), "Vitis", "include")
for name, pn, pm, tn, tm, ta in CASES:
    d = json.loads(json.dumps(base))
    d["kernel"].update(data_type="int", data_bits=32, parallelism_n=pn, parallelism_m=pm, memory_tile_size_n=tn,
                       memory_tile_size_m=tm, transposed_a=ta)
    cfg = BuildCfg.from_dict(d)  # not validated: the first case of each pair is what the new rule rejects
    out = os.path.join(ROOT, "misc", "pe_forwarding", "logs", name.split(" (")[0].replace(" ", "_"))
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, "Config.h"), "w").write(cfg.render_config_h())
    flags = ["-std=c++17", "-O2", "-DMM_DYNAMIC_SIZES", "-DHLSLIB_STREAM_TIMEOUT=16", "-DHLSLIB_LEGACY_SDX=0",
             f"-I{out}", f"-I{os.path.join(KERNEL_DIR, 'include')}", f"-I{HLSLIB_INCLUDE}", "-isystem", vitis_inc,
             "-pthread", "-w"] + (["-DMM_TRANSPOSED_A"] if ta else [])
    srcs = [os.path.join(KERNEL_DIR, "tb", "test_gemm.cpp")] + \
           [os.path.join(KERNEL_DIR, "src", s) for s in ("Top.cpp", "Memory.cpp", "Compute.cpp")]
    exe = os.path.join(out, "test_gemm")
    subprocess.run(["g++", *flags, "-o", exe, *srcs], check=True)
    n, k, m = 2 * tn, 32, 2 * tm
    try:
        p = subprocess.run([exe, str(n), str(k), str(m)], capture_output=True, text=True, timeout=120)
        res = (p.stdout + p.stderr).strip().splitlines()
        verdict = f"exit {p.returncode}: " + " | ".join(res[-2:])[:150]
    except subprocess.TimeoutExpired:
        verdict = "hung (120 s timeout)"
    print(f"{name:42s} {n}x{k}x{m}: {verdict}")
