#!/usr/bin/env python3
"""lane_delay.py - set one DDR byte lane's read settings (no DRAM re-init), to probe read-capture margin.
  lane_delay.py <build_dir> <channel> <lane> <wbitslip> <rbitslip> <delay> [offsets, e.g. 0,2,0,0,0,0,0,0]
Uses boards/dupcb/ddr_init.py's Channel.set_lane through the LitePCIe driver (/dev/litepcie0)."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "boards", "dupcb"))
sys.dont_write_bytecode = True  # no boards/dupcb/__pycache__/ddr_init.*.pyc: that tree is hashed into SoC stamps
import ddr_init  # noqa: E402

b, ch, lane, wbs, rbs, delay = sys.argv[1], *map(int, sys.argv[2:7])
offs = [int(x) for x in sys.argv[7].split(",")] if len(sys.argv) > 7 else [0] * 8
bus = ddr_init.Driver("/dev/litepcie0", os.path.join(b, "csr.csv"))
bus.open()
try:
    c = ddr_init.Channel(bus, ch, os.path.join(b, "sdram_init.py"))
    c.set_lane(lane, wbs, rbs, delay, offs)
finally:
    bus.close()
print(f"CH{ch} lane {lane}: write bitslip {wbs}, read bitslip {rbs}, delay {delay}, offsets {offs}")
