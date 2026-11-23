#
# axi_perf.py - performance counters on one AXI4 master (plan section 4.3), in the kernel's clock domain.
#
# Counters are cleared by `clear` (kernel start) and count only while `run` is high (kernel running), so after the
# kernel is done they are stable and can be read from another clock domain through MultiReg.
# Read latency: AR handshake -> first R beat of that burst, measured with a FIFO of AR timestamps. Valid because the
# HLS master uses a single ID, so R data comes back in AR order.
#

from migen import *
from migen.genlib.fifo import SyncFIFO

from litex.gen import *


class AxiPerfMonitor(LiteXModule):
    def __init__(self, axi, run, clear, width=64, ts_depth=64):
        self.counters = c = {}

        def counter(name, inc, amount=1):
            sig = Signal(width, name=f"perf_{name}")
            self.sync += If(clear, sig.eq(0)).Elif(run & inc, sig.eq(sig + amount))
            c[name] = sig
            return sig

        ar, r, aw, w, b = axi.ar, axi.r, axi.aw, axi.w, axi.b
        ar_hs = ar.valid & ar.ready
        r_hs  = r.valid & r.ready
        aw_hs = aw.valid & aw.ready
        w_hs  = w.valid & w.ready
        b_hs  = b.valid & b.ready

        # Outstanding transactions (not cleared by `clear`: they reflect the bus, not the measurement window).
        rd_out = Signal(16)
        wr_out = Signal(16)
        self.sync += [
            rd_out.eq(rd_out + ar_hs - (r_hs & r.last)),
            wr_out.eq(wr_out + aw_hs - b_hs),
        ]

        counter("ar_count",       ar_hs)
        counter("aw_count",       aw_hs)
        counter("r_beats",        r_hs)
        counter("w_beats",        w_hs)
        counter("b_count",        b_hs)
        counter("ar_stall",       ar.valid & ~ar.ready)                 # address not accepted
        counter("aw_stall",       aw.valid & ~aw.ready)
        counter("r_wait",         r.ready & ~r.valid & (rd_out != 0))   # kernel waits for read data
        counter("r_backpressure", r.valid & ~r.ready)                   # kernel not taking read data
        counter("w_stall",        w.valid & ~w.ready)                   # write data not accepted
        counter("b_wait",         ~b.valid & (wr_out != 0))             # write responses outstanding
        counter("rd_outstanding_sum", 1, rd_out)                        # Little's law: avg latency ~ sum / ar_count

        # Read latency (AR -> first R beat) ---------------------------------------------------------------------
        now = Signal(32)
        self.sync += now.eq(now + 1)
        ts = SyncFIFO(32, ts_depth)
        self.submodules += ts
        r_first = Signal(reset=1)  # Next R beat is the first of a burst.
        self.sync += If(r_hs, r_first.eq(r.last))
        self.comb += [
            ts.din.eq(now),
            ts.we.eq(ar_hs),
            ts.re.eq(r_hs & r_first),
        ]
        lat = Signal(32)
        self.comb += lat.eq(now - ts.dout)
        lat_sum = Signal(width, name="perf_rd_lat_sum")
        lat_min = Signal(32, name="perf_rd_lat_min", reset=2**32 - 1)
        lat_max = Signal(32, name="perf_rd_lat_max")
        sample  = r_hs & r_first & ts.readable
        self.sync += [
            If(clear,
                lat_sum.eq(0), lat_min.eq(2**32 - 1), lat_max.eq(0),
            ).Elif(run & sample,
                lat_sum.eq(lat_sum + lat),
                If(lat < lat_min, lat_min.eq(lat)),
                If(lat > lat_max, lat_max.eq(lat)),
            ),
        ]
        c["rd_lat_sum"] = lat_sum
        c["rd_lat_min"] = lat_min
        c["rd_lat_max"] = lat_max
        counter("ts_overflow", ar_hs & ~ts.writable)  # More reads in flight than ts_depth: latencies invalid.
