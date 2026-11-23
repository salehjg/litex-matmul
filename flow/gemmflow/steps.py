#
# steps.py - the build steps (plan sections 4.7 and 7). Each step hashes its inputs, keeps a stamp file next to its
# outputs and is skipped when the stamp matches and the outputs exist (--force overrides).
#   HlsStep  : Vitis HLS of the kernel        -> build/gemm/hls/<hls_hash[:12]>/   (shared by all configs with that hash)
#   SocStep  : LiteX + Vivado of GemmSoC       -> build/gemm/soc/<build_id>/
#   HostStep : liblitepcie + sw/gemm_host tools -> build/gemm/soc/<build_id>/sw/
# write_build_reports() writes <build>/reports/summary.json (and manifest.json) for gemm_build.py and gemm_dse.py.
#

import hashlib
import json
import os
import subprocess
import sys
import time

from .config import ROOT, tree_hash
from .hls import HlsRun
from .manifest import manifest
from .reports import VivadoReport
from .tools import Tool, ToolError

SOC_SOURCES = [os.path.join(ROOT, d) for d in ("hw/gemm", "boards/dupcb", "tcl/vivado")]
HOST_SOURCES = [os.path.join(ROOT, "sw/gemm_host")]


def _hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


class Step:
    name = "step"

    def __init__(self, cfg, build_dir):
        self.cfg = cfg
        self.build_dir = build_dir

    # To implement ---------------------------------------------------------------------------------------------
    def inputs(self):
        raise NotImplementedError

    def outputs(self):
        raise NotImplementedError

    def _run(self):
        raise NotImplementedError

    def summary(self):
        return {}

    # Common -------------------------------------------------------------------------------------------------------
    @property
    def stamp_path(self):
        return os.path.join(self.build_dir, "stamps", f"{self.name}.json")

    def up_to_date(self):
        try:
            with open(self.stamp_path) as f:
                stamp = json.load(f)
        except (OSError, ValueError):
            return False
        return stamp.get("inputs") == _hash(self.inputs()) and all(os.path.exists(p) for p in self.outputs())

    def run(self, force=False):
        """Returns (seconds, skipped)."""
        if not force and self.up_to_date():
            return 0.0, True
        t0 = time.time()
        self._run()
        seconds = time.time() - t0
        os.makedirs(os.path.dirname(self.stamp_path), exist_ok=True)
        with open(self.stamp_path, "w") as f:
            json.dump({"inputs": _hash(self.inputs()), "seconds": round(seconds, 1),
                       "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "summary": self.summary()}, f, indent=2)
        return seconds, False


class HlsStep(Step):
    name = "hls"

    def __init__(self, cfg, build_dir):
        super().__init__(cfg, build_dir)
        self.hls = HlsRun(cfg)

    def inputs(self):
        return {"hls_hash": self.hls.hash}

    def outputs(self):
        return [os.path.join(self.hls.ip_dir, "component.xml")]

    def up_to_date(self):  # The HLS dir is shared between builds: its own stamp counts.
        return self.hls.up_to_date()

    def run(self, force=False):
        seconds, skipped = self.hls.run(force=force)
        return seconds, skipped

    def summary(self):
        return self.hls.report().summary()


class SocStep(Step):
    name = "soc"

    def inputs(self):
        return {"config_hash": self.cfg.config_hash(), "hls_hash": self.cfg.hls_hash(),
                "sources": [tree_hash(d) for d in SOC_SOURCES]}

    def outputs(self):
        return [os.path.join(self.build_dir, "gateware", "dupcb.bit"), os.path.join(self.build_dir, "csr.csv")]

    def _run(self):
        os.makedirs(os.path.join(self.build_dir, "logs"), exist_ok=True)
        cfg_path = os.path.join(self.build_dir, "config.json")
        with open(cfg_path, "w") as f:
            json.dump(self.cfg.to_dict(), f, indent=2)
        v = self.cfg.resolved_vivado()
        args = [sys.executable, os.path.join(ROOT, "hw", "gemm", "gemm_soc.py"), "--gemm-config", cfg_path,
                "--output-dir", self.build_dir, "--build",
                "--sys-clk-freq", str(self.cfg.soc.sys_clk_mhz * 1e6),
                "--vivado-max-threads", str(v["max_threads"]),
                "--vivado-synth-directive", v["synth_directive"],
                "--vivado-opt-directive", v["opt_directive"],
                "--vivado-place-directive", v["place_directive"],
                "--vivado-route-directive", v["route_directive"],
                "--vivado-post-route-phys-opt-directive", v["post_route_phys_opt"]]
        if v["post_place_phys_opt"]:
            args += ["--vivado-post-place-phys-opt-directive", v["post_place_phys_opt"]]
        Tool().run(args, cwd=self.build_dir, log=os.path.join(self.build_dir, "logs", "soc.log"))
        timing = VivadoReport(os.path.join(self.build_dir, "gateware")).timing()
        if not timing:
            raise ToolError("Vivado finished without a timing report", os.path.join(self.build_dir, "logs", "soc.log"))

    def summary(self):
        return VivadoReport(os.path.join(self.build_dir, "gateware")).summary()


class HostStep(Step):
    name = "host"

    def inputs(self):
        with open(os.path.join(self.build_dir, "csr.csv"), "rb") as f:
            csr = hashlib.sha256(f.read()).hexdigest()
        return {"csr": csr, "sources": [tree_hash(d) for d in HOST_SOURCES]}

    def outputs(self):
        return [os.path.join(self.build_dir, "sw", t) for t in ("ddr_dma_test", "gemm_runner")]

    def _run(self):
        log = os.path.join(self.build_dir, "logs", "host.log")
        with open(log, "w") as f:
            for args in (["make", "-s", "-C", os.path.join(self.build_dir, "driver", "user")],
                         ["make", "-s", "-C", os.path.join(ROOT, "sw", "gemm_host"), f"BUILD_DIR={self.build_dir}"]):
                p = subprocess.run(args, stdout=f, stderr=subprocess.STDOUT)
                if p.returncode:
                    raise ToolError(f"{' '.join(args)} failed", log)


def write_build_reports(cfg, build_dir, steps):
    """Write <build>/reports/summary.json (config, hashes, per-step summaries) and, if a step actually ran,
    <build>/manifest.json (the tree state its outputs were made from; an all-skipped rerun keeps the old one).
    `steps` maps a step name to its summary dict ("seconds", "skipped", ...). Returns the summary."""
    m = manifest()
    summary = {"build_id": cfg.build_id(), "config_hash": cfg.config_hash(), "hls_hash": cfg.hls_hash(),
               "config": cfg.to_dict(), "manifest": m, "steps": steps}
    os.makedirs(os.path.join(build_dir, "reports"), exist_ok=True)
    if any(not st.get("skipped", True) for st in steps.values()):
        with open(os.path.join(build_dir, "manifest.json"), "w") as f:
            json.dump(m, f, indent=2)
    with open(os.path.join(build_dir, "reports", "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    return summary
