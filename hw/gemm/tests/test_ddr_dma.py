#!/usr/bin/env python3
#
# test_ddr_dma.py - simulation of PCIeDDRBridge (hw/gemm/ddr_dma.py) with LiteDRAM's DRAMMemory model on the native
# ports and a fake LitePCIe DMA on the 128-bit side. Random gaps on the H2C stream, random back-pressure on C2H,
# extra beats after `length` (drop path), and a transfer stopped mid-stream followed by a full one.
#   .venv/bin/python hw/gemm/tests/test_ddr_dma.py
#

import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "deps", "litedram"))

from migen import *                                                    # noqa: E402
from litex.soc.interconnect import stream                              # noqa: E402
from litedram.common import LiteDRAMNativeWritePort, LiteDRAMNativeReadPort  # noqa: E402
from test.common import DRAMMemory                                     # noqa: E402  (deps/litedram/test/common.py)

from ddr_dma import PCIeDDRBridge                                      # noqa: E402

DW_PCIE, DW_DRAM, AW = 128, 512, 16
RATIO = DW_DRAM // DW_PCIE


class FakeDMA:
    def __init__(self):
        self.source = stream.Endpoint([("data", DW_PCIE)])  # host -> card
        self.sink   = stream.Endpoint([("data", DW_PCIE)])  # card -> host


class DUT(Module):
    def __init__(self):
        self.dma = FakeDMA()
        self.ports = [(LiteDRAMNativeWritePort(AW, DW_DRAM), LiteDRAMNativeReadPort(AW, DW_DRAM)) for _ in range(2)]
        self.submodules.bridge = PCIeDDRBridge(self.dma, self.ports)


def beats_of(words):
    """512-bit DDR words -> 128-bit PCIe beats, LSB chunk first (what the up-converter expects)."""
    out = []
    for w in words:
        for i in range(RATIO):
            out.append((w >> (DW_PCIE * i)) & ((1 << DW_PCIE) - 1))
    return out


def run(seed=1):
    dut = DUT()
    mems = [DRAMMemory(DW_DRAM, 1 << 12) for _ in range(2)]
    prng = random.Random(seed)
    b = dut.bridge
    results = {}

    def csr(sig, value):
        yield sig.eq(value)
        yield

    def send(beats, gap_pct=30):
        src = dut.dma.source
        for x in beats:
            while prng.randrange(100) < gap_pct:
                yield src.valid.eq(0)
                yield
            yield src.valid.eq(1)
            yield src.data.eq(x)
            yield
            while not (yield src.ready):
                yield
        yield src.valid.eq(0)

    def h2c(ch, base_word, words, extra_beats=0, stop_after=None):
        w = getattr(b, f"ch{ch}").writer
        yield from csr(b.sel.storage, ch)
        yield from csr(w._enable.storage, 0)
        yield from csr(w._base.storage, base_word * DW_DRAM // 8)
        yield from csr(w._length.storage, len(words) * DW_DRAM // 8)
        yield from csr(w._enable.storage, 1)
        beats = beats_of(words) + [prng.getrandbits(DW_PCIE) for _ in range(extra_beats)]
        if stop_after is not None:
            beats = beats[:stop_after]
        yield from send(beats)
        if stop_after is None:
            for _ in range(10000):
                if ((yield b.drained.status) >> ch) & 1:
                    break
                yield
            else:
                raise AssertionError(f"ch{ch}: not drained")
        yield from csr(w._enable.storage, 0)

    def c2h(ch, base_word, nwords, ready_pct=60):
        r = getattr(b, f"ch{ch}").reader
        sink = dut.dma.sink
        yield from csr(b.sel.storage, ch)
        yield from csr(r._enable.storage, 0)
        yield from csr(r._base.storage, base_word * DW_DRAM // 8)
        yield from csr(r._length.storage, nwords * DW_DRAM // 8)
        yield from csr(r._enable.storage, 1)
        got = []
        for _ in range(200000):
            ready = prng.randrange(100) < ready_pct
            yield sink.ready.eq(int(ready))
            yield
            if ready and (yield sink.valid):
                got.append((yield sink.data))
            if len(got) == nwords * RATIO:
                break
        yield sink.ready.eq(0)
        yield from csr(r._enable.storage, 0)
        return got

    def main():
        n = 40
        data = {ch: [prng.getrandbits(DW_DRAM) for _ in range(n)] for ch in (0, 1)}
        # 1. Aborted H2C (stops in the middle of a DDR word), then the real transfers with extra beats.
        yield from h2c(0, 0, data[0], stop_after=RATIO * 5 + 2)
        for ch in (0, 1):
            yield from h2c(ch, 8, data[ch], extra_beats=13)
        # 2. Read back with back-pressure, twice.
        for ch in (0, 1):
            for k in range(2):
                results[(ch, k)] = yield from c2h(ch, 8, n)
        results["expected"] = {ch: beats_of(data[ch]) for ch in (0, 1)}
        results["mem"] = {ch: mems[ch].mem[8:8 + n] for ch in (0, 1)}
        results["data"] = data

    gens = [main()]
    for (wp, rp), mem in zip(dut.ports, mems):
        gens += [mem.write_handler(wp), mem.read_handler(rp)]
    run_simulation(dut, gens)

    ok = True
    for ch in (0, 1):
        mem_ok = results["mem"][ch] == results["data"][ch]
        print(f"ch{ch}: DDR content after H2C {'OK' if mem_ok else 'WRONG'}")
        if not mem_ok:
            bad = [i for i, (a, e) in enumerate(zip(results["mem"][ch], results["data"][ch])) if a != e]
            print(f"   bad DDR words: {bad[:10]}...")
        ok &= mem_ok
        for k in range(2):
            got, exp = results[(ch, k)], results["expected"][ch]
            c_ok = got == exp
            print(f"ch{ch}: C2H read {k} {'OK' if c_ok else 'WRONG'} ({len(got)} beats)")
            if not c_ok:
                bad = [i for i, (a, e) in enumerate(zip(got, exp)) if a != e]
                print(f"   first bad beats: {bad[:10]}")
            ok &= c_ok
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
