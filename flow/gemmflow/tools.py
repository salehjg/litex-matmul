#
# tools.py - run external tools with the Xilinx environment from config.sh, log to a file, fail loudly.
# Plan section 4.7: Tool -> VitisHls, Vivado.
#

import os
import shlex
import subprocess
import time

from .config import ROOT


class ToolError(RuntimeError):
    def __init__(self, msg, log=None):
        super().__init__(msg + (f" (log: {log})" if log else ""))
        self.log = log


def read_config_sh(path=os.path.join(ROOT, "config.sh")):
    """KEY="value" lines of config.sh (written by setup.litex.du-pcb.py), as a dict."""
    values = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"')
    return values


class Tool:
    """A command run through bash with the Vivado settings sourced (like scripts/env.sh)."""
    name = "tool"

    def __init__(self):
        cfg = read_config_sh()
        self.vivado_settings = cfg["VIVADO_SETTINGS"]                         # .../2026.1/Vivado/settings64.sh
        self.xilinx_root = os.path.dirname(os.path.dirname(self.vivado_settings))  # .../2026.1

    def run(self, args, cwd, log, timeout=None):
        """Run args (list) in cwd; stdout+stderr to the log file. Returns elapsed seconds; raises ToolError."""
        os.makedirs(cwd, exist_ok=True)
        cmd = f"set +u; source {shlex.quote(self.vivado_settings)} > /dev/null; " + shlex.join(args)
        t0 = time.time()
        with open(log, "w") as f:
            f.write(f"# {self.name}: {shlex.join(args)}\n# cwd: {cwd}\n")
            f.flush()
            try:
                p = subprocess.run(["bash", "-c", cmd], cwd=cwd, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise ToolError(f"{self.name} timed out after {timeout} s", log)
        if p.returncode != 0:
            raise ToolError(f"{self.name} failed with exit code {p.returncode}", log)
        return time.time() - t0


class VitisHls(Tool):
    name = "vitis-run (hls)"

    def run_tcl(self, tcl, cwd, log, timeout=None):
        exe = os.path.join(self.xilinx_root, "Vitis", "bin", "vitis-run")
        return self.run([exe, "--mode", "hls", "--tcl", tcl], cwd, log, timeout)


class Vivado(Tool):
    name = "vivado"

    def run_tcl(self, tcl, cwd, log, tclargs=(), timeout=None):
        args = ["vivado", "-mode", "batch", "-nojournal", "-nolog", "-source", tcl]
        if tclargs:
            args += ["-tclargs", *map(str, tclargs)]
        return self.run(args, cwd, log, timeout)
