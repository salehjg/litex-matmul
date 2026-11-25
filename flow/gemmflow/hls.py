#
# hls.py - one Vitis HLS run of the GEMM kernel: render Config.h + params.tcl, run tcl/hls/run_hls.tcl, parse
# csynth.xml. Output in build/gemm/hls/<hls_hash[:12]>/ (keyed only by what the IP depends on, so Vivado-only changes
# reuse it). Plan sections 4.5 and 7.
#

import fcntl
import json
import os
import time
import xml.etree.ElementTree as ET

from .config import ROOT, DEVICE
from .tools import VitisHls

HLSLIB_INCLUDE = os.path.join(ROOT, "submodules", "gemm_hls", "hlslib", "include")
RUN_HLS_TCL    = os.path.join(ROOT, "tcl", "hls", "run_hls.tcl")
TOP            = "MatrixMultiplicationKernel"
IP             = {"vendor": "litex-matmul", "library": "hls", "name": "gemm_kernel", "version": "1.0"}


def _tcl_list(items):
    return "[list " + " ".join("{" + str(i) + "}" for i in items) + "]"


class HlsRun:
    def __init__(self, cfg, root=os.path.join(ROOT, "build", "gemm", "hls")):
        self.cfg     = cfg
        self.hash    = cfg.hls_hash()
        self.dir     = os.path.join(root, self.hash[:12])
        self.project = "prj"
        self.sol_dir = os.path.join(self.dir, self.project, "sol")
        self.stamp   = os.path.join(self.dir, "stamp.json")
        self.log     = os.path.join(self.dir, "hls.log")

    # Paths of the results -------------------------------------------------------------------------------------
    @property
    def csynth_xml(self):
        return os.path.join(self.sol_dir, "syn", "report", "csynth.xml")

    @property
    def ip_dir(self):
        return os.path.join(self.sol_dir, "impl", "ip")

    def up_to_date(self):
        try:
            with open(self.stamp) as f:
                return json.load(f).get("hls_hash") == self.hash and os.path.isdir(self.ip_dir)
        except (OSError, ValueError):
            return False

    # Steps ----------------------------------------------------------------------------------------------------
    def cflags(self):
        k = self.cfg.kernel
        flags = ["-O3", "-DMM_SYNTHESIS", "-DHLSLIB_SYNTHESIS", "-DMM_DYNAMIC_SIZES",
                 "-DHLSLIB_STREAM_TIMEOUT=16", "-DHLSLIB_LEGACY_SDX=0",
                 f"-I{self.dir}", f"-I{os.path.join(self.cfg.kernel_dir(), 'include')}", f"-I{HLSLIB_INCLUDE}"]
        if self.cfg.is_mixed():
            flags.append("-DAP_INT_MAX_W=4096")  # kernels/gemm_mixed: wide C packs
        if k.transposed_a:
            flags.append("-DMM_TRANSPOSED_A")
        if k.data_type == "half":
            flags.append("-DMM_HALF_PRECISION")
        return " ".join(flags)

    def op_bindings(self):
        """Vitis config_op pairs (op, impl) from kernel.mult_resource / add_resource (notes2 plan 4.1); empty when
        both are null, so earlier HLS runs see the same commands."""
        k, ops = self.cfg.kernel, self.cfg.numeric().hls_ops
        return [(ops[name], impl) for name, impl in (("mult", k.mult_resource), ("add", k.add_resource)) if impl]

    def prepare(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "Config.h"), "w") as f:
            f.write(self.cfg.render_config_h())
        period = 1000.0 / self.cfg.clocks.accel_mhz
        sources = [os.path.join(self.cfg.kernel_dir(), "src", s) for s in ("Top.cpp", "Memory.cpp", "Compute.cpp")]
        params = {
            "project":           self.project,
            "top":               TOP,
            "part":              DEVICE["part"],
            "clock_period_ns":   f"{period:.4f}",
            "clock_uncertainty": f"{self.cfg.clocks.hls_uncertainty_pct:g}%",
            "cflags":            "{" + self.cflags() + "}",
            "op_bindings":       _tcl_list(f"{op} {impl}" for op, impl in self.op_bindings()),
            "sources":           _tcl_list(sources),
            "testbench":         "{" + os.path.join(self.cfg.kernel_dir(), "tb", "test_gemm.cpp") + "}",
            "run_csim":          int(self.cfg.hls.run_csim),
            "csim_argv":         "{256 256 256}",
            "export_flow":       self.cfg.hls.export_flow,
            "ip_vendor":         IP["vendor"],
            "ip_library":        IP["library"],
            "ip_name":           IP["name"],
            "ip_version":        IP["version"],
        }
        with open(os.path.join(self.dir, "params.tcl"), "w") as f:
            f.write("# Written by flow/gemmflow/hls.py, read by tcl/hls/run_hls.tcl\n")
            for k, v in params.items():
                f.write(f"set ::P({k}) {v}\n")
        with open(os.path.join(self.dir, "config.resolved.json"), "w") as f:
            json.dump(self.cfg.to_dict(), f, indent=2)

    def run(self, force=False, timeout=4 * 3600):
        """Returns (seconds, skipped). One run per HLS dir at a time: builds that differ only outside the HLS hash
        (e.g. the Vivado preset) share the dir, and two Vitis runs in it break each other (dtype_clock, 2026-11-24).
        The second caller waits on <dir>.lock and then finds the dir up to date."""
        if not force and self.up_to_date():
            return 0.0, True
        os.makedirs(os.path.dirname(self.dir), exist_ok=True)
        with open(self.dir + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not force and self.up_to_date():
                return 0.0, True
            return self._run_locked(timeout)

    def _run_locked(self, timeout):
        self.prepare()
        seconds = VitisHls().run_tcl(RUN_HLS_TCL, cwd=self.dir, log=self.log, timeout=timeout)
        summary = HlsReport(self.csynth_xml).summary()
        with open(self.stamp, "w") as f:
            json.dump({"hls_hash": self.hash, "seconds": round(seconds, 1), "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "summary": summary}, f, indent=2)
        return seconds, False

    def report(self):
        return HlsReport(self.csynth_xml)


class HlsReport:
    """Parse Vitis HLS csynth.xml: estimated clock, latency, resources, RTL ports."""
    def __init__(self, path):
        self.path = path
        self.root = ET.parse(path).getroot()

    def _text(self, xpath, cast=str, default=None):
        node = self.root.find(xpath)
        if node is None or node.text is None:
            return default
        try:
            return cast(node.text.strip())
        except ValueError:
            return default

    def summary(self):
        r = "AreaEstimates/Resources/"
        return {
            "target_clock_ns":    self._text("UserAssignments/TargetClockPeriod", float),
            "estimated_clock_ns": self._text("PerformanceEstimates/SummaryOfTimingAnalysis/EstimatedClockPeriod", float),
            "latency_best":       self._text("PerformanceEstimates/SummaryOfOverallLatency/Best-caseLatency"),
            "latency_worst":      self._text("PerformanceEstimates/SummaryOfOverallLatency/Worst-caseLatency"),
            "bram_18k":           self._text(r + "BRAM_18K", int),
            "dsp":                self._text(r + "DSP", int),
            "ff":                 self._text(r + "FF", int),
            "lut":                self._text(r + "LUT", int),
            "uram":               self._text(r + "URAM", int),
            "axi_data_bits":      sorted(set(self.axi_data_bits().values())),
        }

    def axi_data_bits(self):
        """{bundle: data width in bits} of the m_axi masters (RDATA or WDATA), e.g. {"gmem0": 512, ...}."""
        out = {}
        for p in self.rtl_ports():
            m = p["name"].split("_")
            if len(m) >= 4 and m[0] == "m" and m[1] == "axi" and m[3] in ("RDATA", "WDATA"):
                out[m[2]] = p["bits"]
        return out

    def rtl_ports(self):
        """List of dicts: name, dir (in/out), bits, protocol, object (C argument or bundle)."""
        ports = []
        for p in self.root.iter("RtlPorts"):
            get = lambda tag: (p.findtext(tag) or "").strip()
            ports.append({"name": get("name"), "dir": get("Dir"), "bits": int(get("Bits") or 0),
                          "protocol": get("IOProtocol"), "object": get("Object"), "type": get("Type")})
        return ports
