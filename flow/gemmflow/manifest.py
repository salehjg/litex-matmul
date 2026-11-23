#
# manifest.py - provenance for builds and result rows: git state, submodule, tools, host, time (plan section 7).
#

import datetime
import os
import platform
import subprocess

from .config import ROOT
from .tools import read_config_sh


def _git(*args):
    p = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def manifest():
    status = _git("status", "--porcelain")
    settings = read_config_sh().get("VIVADO_SETTINGS", "")
    return {
        "time":          datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "git_sha":       _git("rev-parse", "HEAD"),
        "git_dirty":     bool(status),
        "gemm_hls_sha":  _git("-C", os.path.join(ROOT, "submodules", "gemm_hls"), "rev-parse", "HEAD"),
        "xilinx":        os.path.basename(os.path.dirname(os.path.dirname(settings))) if settings else None,  # 2026.1
        "host":          platform.node(),
        "kernel":        platform.release(),
        "python":        platform.python_version(),
    }
