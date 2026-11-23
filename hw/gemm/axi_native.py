#
# axi_native.py - AXI4 write master -> LiteDRAM native write port, for the kernel's write-only bundle (C).
#
# Replaces LiteDRAMAXI2NativeW there: its write-response FIFO only ever holds constant data (AWID is constant 0 with
# a single ID, resp is always OKAY), Vivado then optimizes the LUTRAM's read side away but leaves the cell with
# driverless read-address pins (ERROR Route 35-13). Here the B responses come from a counter instead.
#
# Assumes INCR bursts, full-width beats (AWSIZE = data width) and a single AXI ID: what the HLS m_axi adapter emits
# (kernels/gemm/src/Top.cpp). Bursts are handled in order; one native command per beat.
#

from migen import *

from litex.gen import *
from litex.soc.interconnect import stream


class AxiWriteToNative(LiteXModule):
    def __init__(self, axi, port, fifo_depth=32, base_address=0):
        assert axi.data_width == port.data_width
        shift = log2_int(port.data_width // 8)
        aw, w, b = axi.aw, axi.w, axi.b

        # Current burst: word address and beats left.
        addr  = Signal(port.address_width)
        left  = Signal(9)   # AXI4 len + 1 <= 256
        busy  = Signal()
        bid   = Signal(len(aw.id))
        self.comb += aw.ready.eq(~busy)
        self.sync += If(aw.valid & aw.ready,
            busy.eq(1),
            addr.eq((aw.addr - base_address)[shift:]),
            left.eq(aw.len + 1),
            bid.eq(aw.id),
        )

        # Data FIFO in front of wdata (commands and data leave together, like LiteDRAMDMAWriter).
        fifo = stream.SyncFIFO([("data", port.data_width), ("we", port.data_width // 8)], fifo_depth)
        self.submodules += fifo
        beat = Signal()
        self.comb += [
            port.cmd.we.eq(1),
            port.cmd.addr.eq(addr),
            port.cmd.last.eq(left == 1),
            port.cmd.valid.eq(busy & w.valid & fifo.sink.ready),
            w.ready.eq(busy & port.cmd.ready & fifo.sink.ready),
            fifo.sink.valid.eq(busy & w.valid & port.cmd.ready),
            fifo.sink.data.eq(w.data),
            fifo.sink.we.eq(w.strb),
            beat.eq(w.valid & w.ready),
            port.wdata.valid.eq(fifo.source.valid),
            port.wdata.data.eq(fifo.source.data),
            port.wdata.we.eq(fifo.source.we),
            fifo.source.ready.eq(port.wdata.ready),
        ]
        burst_done = Signal()
        self.comb += burst_done.eq(beat & (left == 1))
        self.sync += If(beat,
            addr.eq(addr + 1),
            left.eq(left - 1),
            If(left == 1, busy.eq(0)),
        )

        # Write responses: one per finished burst (OKAY), counted, not queued.
        pending = Signal(16)
        self.comb += [
            b.valid.eq(pending != 0),
            b.resp.eq(0),
            b.id.eq(bid),
        ]
        self.sync += pending.eq(pending + burst_done - (b.valid & b.ready))
