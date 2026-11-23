#!/usr/bin/env python3
#
# test_rx_be.py - simulation check of boards/dupcb/s7pciephy_rx_be.py:rx_be_from_tuser (7-series 128-bit RX byte
# enables from rx_is_sof / rx_is_eof). Run: .venv/bin/python misc/dma_debug/test_rx_be.py
#
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "boards", "dupcb"))

from migen import *                                   # noqa: E402
from s7pciephy_rx_be import rx_be_from_tuser          # noqa: E402


def tuser_of(sof=None, eof=None):
    """sof/eof: DW index (0..3) or None. Byte position = 4*dw for SOF, 4*dw + 3 for EOF (last byte of the DW)."""
    v = 0
    if sof is not None: v |= (0b10000 | (4*sof))     << 10
    if eof is not None: v |= (0b10000 | (4*eof + 3)) << 17
    return v


CASES = [
    # (description,                                    sof,  eof,  tkeep,  expected be)
    ("mid-packet beat: tkeep passes",                  None, None, 0xffff, 0xffff),
    ("SOF at DW0, no EOF: tkeep passes",               0,    None, 0xffff, 0xffff),
    ("256 B completion last beat (67 DW): EOF at DW2", None, 2,    0xffff, 0x0fff),
    ("EOF at DW0",                                     None, 0,    0xffff, 0x000f),
    ("EOF at DW3 (full last beat)",                    None, 3,    0xffff, 0xffff),
    ("single-beat TLP SOF DW0 EOF DW3",                0,    3,    0xffff, 0xffff),
    ("straddle: EOF DW1 + next SOF DW2",               2,    1,    0xffff, 0xffff),
    ("straddle: EOF DW0 + next SOF DW2",               2,    0,    0xffff, 0xff0f),
]


class DUT(Module):
    def __init__(self):
        self.be    = Signal(16)
        self.tkeep = Signal(16)
        self.tuser = Signal(32)
        self.comb += rx_be_from_tuser(self.be, self.tkeep, self.tuser)


def main():
    dut = DUT()
    failures = []

    def gen():
        for desc, sof, eof, tkeep, expected in CASES:
            yield dut.tuser.eq(tuser_of(sof, eof))
            yield dut.tkeep.eq(tkeep)
            yield
            got = (yield dut.be)
            ok = got == expected
            print(f"{'ok  ' if ok else 'FAIL'} {desc:<48} be={got:#06x} expected={expected:#06x}")
            if not ok:
                failures.append(desc)

    run_simulation(dut, gen())
    print("PASS" if not failures else f"{len(failures)} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
