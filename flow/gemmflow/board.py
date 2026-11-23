#
# board.py - bring a GemmSoC build up on the card (plan section 8), in the order that works:
#   program (JTAG) -> PCIe rescan -> driver build + load (its probe resets the SoC) -> DDR calibration -> host tools.
# The DDR calibration must come after the driver load: the driver's probe writes CSR_CTRL_RESET, which clears it.
#

import os
import re
import subprocess

from .config import ROOT
from .tools import Vivado, ToolError


class Board:
    def __init__(self, build_dir, log_dir=None):
        self.build_dir = os.path.abspath(build_dir)
        self.log_dir   = log_dir or os.path.join(self.build_dir, "logs")
        os.makedirs(self.log_dir, exist_ok=True)

    def _sh(self, args, log_name, env_extra=None, timeout=600):
        env = dict(os.environ, BUILD_DIR=self.build_dir, **(env_extra or {}))
        log = os.path.join(self.log_dir, log_name)
        with open(log, "w") as f:
            p = subprocess.run(args, cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
        with open(log) as f:
            out = f.read()
        if p.returncode != 0:
            raise ToolError(f"{' '.join(args)} failed ({p.returncode})", log)
        return out

    @property
    def bitstream(self):
        return os.path.join(self.build_dir, "gateware", "dupcb.bit")

    def program(self):
        log = os.path.join(self.log_dir, "program.log")
        Vivado().run_tcl(os.path.join(ROOT, "tcl", "vivado", "program.tcl"), cwd=self.log_dir, log=log,
                         tclargs=[self.bitstream], timeout=600)
        with open(log) as f:
            if "PROGRAM_DONE_PIN=1" not in f.read():
                raise ToolError("FPGA DONE pin not high after programming", log)

    def rescan(self):
        out = self._sh(["scripts/pcie_rescan.sh"], "pcie_rescan.log", timeout=120)
        m = re.search(r"Link: (.*)", out)
        if "BAR0 size: 128 KB" not in out:
            raise ToolError("card not back on the bus with the LiteX BAR0 after rescan",
                            os.path.join(self.log_dir, "pcie_rescan.log"))
        return m.group(1) if m else ""

    def driver(self):
        self._sh(["scripts/driver.sh", "build"], "driver_build.log")
        self._sh(["scripts/driver.sh", "load"], "driver_load.log", timeout=60)

    def ddr_init(self):
        out = self._sh(["scripts/ddr_init.sh"], "ddr_init.log", timeout=600)
        m = re.search(r"Result: (.*)", out)
        if not m or "FAIL" in m.group(1):
            raise ToolError(f"DDR calibration failed: {m.group(1) if m else 'no result'}",
                            os.path.join(self.log_dir, "ddr_init.log"))
        return m.group(1)

    def host_tools(self):
        self._sh(["make", "-s", "-C", "sw/gemm_host", f"BUILD_DIR={self.build_dir}"], "host_tools.log")

    def bring_up(self, program=True, log=print):
        if program:
            log("program (JTAG)");     self.program()
            log("pcie rescan");        log("  link " + self.rescan())
        log("driver build + load");    self.driver()
        log("ddr calibration");        log("  " + self.ddr_init())
        log("host tools");             self.host_tools()
