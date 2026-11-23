#!/usr/bin/env python3
#
# test_axi_native.py - simulation of AxiWriteToNative (hw/gemm/axi_native.py): AXI4 write bursts of several lengths,
# with random gaps, into LiteDRAM's DRAMMemory model; checks the memory and the number of B responses.
#   .venv/bin/python hw/gemm/tests/test_axi_native.py
#

import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "deps", "litedram"))

from migen import *                                          # noqa: E402
from litex.soc.interconnect.axi import AXIInterface          # noqa: E402
from litedram.common import LiteDRAMNativeWritePort          # noqa: E402
from test.common import DRAMMemory                           # noqa: E402

from axi_native import AxiWriteToNative                      # noqa: E402

DW, AW = 512, 16


class DUT(Module):
    def __init__(self):
        self.axi  = AXIInterface(data_width=DW, address_width=32, id_width=1)
        self.port = LiteDRAMNativeWritePort(AW, DW)
        self.submodules.conv = AxiWriteToNative(self.axi, self.port)


def main():
    dut = DUT()
    mem = DRAMMemory(DW, 1 << 10)
    prng = random.Random(3)
    bursts = [(0, 1), (4, 16), (40, 64), (200, 3), (300, 256)]  # (start word, beats)
    expected = {}
    got_b = []

    def master():
        aw, w, b = dut.axi.aw, dut.axi.w, dut.axi.b
        yield b.ready.eq(1)
        for start, beats in bursts:
            yield aw.valid.eq(1); yield aw.addr.eq(start * DW // 8); yield aw.len.eq(beats - 1)
            yield aw.size.eq(6); yield aw.burst.eq(1); yield aw.id.eq(0)
            yield
            while not (yield aw.ready):
                yield
            yield aw.valid.eq(0)
            for i in range(beats):
                while prng.randrange(100) < 25:
                    yield w.valid.eq(0)
                    yield
                data = prng.getrandbits(DW)
                expected[start + i] = data
                yield w.valid.eq(1); yield w.data.eq(data); yield w.strb.eq(2**(DW // 8) - 1)
                yield w.last.eq(int(i == beats - 1))
                yield
                while not (yield w.ready):
                    yield
            yield w.valid.eq(0)
        for _ in range(2000):
            yield

    @passive
    def b_monitor():
        b = dut.axi.b
        while True:
            if (yield b.valid) and (yield b.ready):
                got_b.append((yield b.resp))
            yield

    run_simulation(dut, [master(), b_monitor(), mem.write_handler(dut.port)])
    bad = [a for a, d in expected.items() if mem.mem[a] != d]
    ok = not bad and len(got_b) == len(bursts) and all(r == 0 for r in got_b)
    print(f"words written {len(expected)}, bad {len(bad)}, B responses {len(got_b)}/{len(bursts)}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
