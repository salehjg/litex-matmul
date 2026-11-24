#
# reports.py - flat summaries of the tool reports for result rows (plan section 4.7). HLS: hls.HlsReport (csynth.xml).
# Vivado: the text reports LiteX's build script writes (timing summary, placed utilization, hierarchical utilization).
#

import os
import re


class VivadoReport:
    def __init__(self, gateware_dir, build_name="dupcb"):
        self.dir  = gateware_dir
        self.name = build_name

    def _read(self, suffix):
        path = os.path.join(self.dir, f"{self.name}_{suffix}")
        try:
            with open(path, errors="replace") as f:
                return f.read()
        except OSError:
            return None

    def timing(self):
        """WNS/TNS/WHS/THS (ns) from the routed timing summary, None if missing."""
        text = self._read("timing.rpt")
        if not text:
            return {}
        m = re.search(r"WNS\(ns\)\s+TNS\(ns\).*?\n\s*-+.*?\n\s*(\S+)\s+(\S+)\s+\S+\s+\S+\s+(\S+)\s+(\S+)", text, re.S)
        if not m:
            return {}
        wns, tns, whs, ths = (float(x) for x in m.groups())
        return {"wns_ns": wns, "tns_ns": tns, "whs_ns": whs, "ths_ns": ths, "timing_met": wns >= 0 and whs >= 0}

    def utilization(self):
        """Used LUT/FF/BRAM tiles/DSP of the whole placed design."""
        text = self._read("utilization_place.rpt")
        if not text:
            return {}
        out = {}
        for key, label in (("lut", "Slice LUTs"), ("ff", "Slice Registers"), ("bram_tiles", "Block RAM Tile"),
                           ("dsp", "DSPs")):
            m = re.search(r"^\|\s*" + re.escape(label) + r"\s*\|\s*([\d.]+)\s*\|", text, re.M)
            if m:
                out[key] = float(m.group(1))
        return out

    # Hierarchical report column -> summary key (columns are matched by header name, not position).
    KERNEL_COLUMNS = {"Total LUTs": "kernel_lut", "Logic LUTs": "kernel_logic_lut", "LUTRAMs": "kernel_lutram",
                      "SRLs": "kernel_srl", "FFs": "kernel_ff", "RAMB36": "kernel_ramb36", "RAMB18": "kernel_ramb18",
                      "DSP Blocks": "kernel_dsp"}

    def kernel_utilization(self, instance="gemm_kernel_ip"):
        """LUTs/LUTRAMs/SRLs/FFs/RAMB36/RAMB18/DSPs of the kernel instance from the hierarchical report, if present.
        (Before 2026-11-24 the columns were taken by position and shifted by one after Total LUTs.)"""
        text = self._read("utilization_hierarchical_place.rpt")
        if not text:
            return {}
        header = None
        for line in text.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not cells:
                continue
            if cells[0] == "Instance":
                header = cells
            elif header and cells[0] == instance:
                out = {}
                for name, value in zip(header, cells):
                    if name in self.KERNEL_COLUMNS:
                        try:
                            out[self.KERNEL_COLUMNS[name]] = float(value)
                        except ValueError:
                            pass
                return out
        return {}

    def summary(self):
        s = {}
        s.update(self.timing())
        s.update(self.utilization())
        s.update(self.kernel_utilization())
        return s
