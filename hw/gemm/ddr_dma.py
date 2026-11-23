#
# ddr_dma.py - host <-> DDR3 through the LitePCIe DMA streams, one DDR channel at a time.
#
#   host -> DDR (H2C): pcie_dma0.source (128 b) -> Converter -> writer of the selected channel
#   DDR -> host (C2H): reader of the selected channel -> Converter -> pcie_dma0.sink
#
# The DDR side is LiteDRAM's LiteDRAMDMAWriter / LiteDRAMDMAReader in CSR mode (base and length in bytes, enable, done,
# offset). Per channel the CSRs are ddr_dma_ch<n>_writer_* and ddr_dma_ch<n>_reader_*.
#
# Host protocol (sw/gemm_host, docs/notes1/gemm-hls-progress.md phase 2):
#   H2C: sel = ch; writer base/length; writer enable = 0 then 1; stream the data with the PCIe DMA reader;
#        wait for ch<n>_drained; writer enable = 0. Data arriving while the selected writer is not running (before
#        enable, after length bytes) is dropped: the host sends whole 8 KB DMA buffers and loop-mode repeats.
#   C2H: sel = ch; enable the PCIe DMA writer; reader base/length (multiple of 8 KB, at least 2 buffers so that
#        pcie_dma0_writer_table_loop_status is unambiguous); reader enable = 0 then 1; wait until the PCIe DMA writer
#        has written length/8 KB buffers (loop_status index = n-1, count 0); reader enable = 0.
# Lengths and bases must be multiples of the DDR word (64 B).
#

from migen import *

from litex.gen import *
from litex.soc.interconnect import stream
from litex.soc.interconnect.csr import CSRStorage, CSRStatus, CSRField

from litedram.frontend.dma import LiteDRAMDMAReader, LiteDRAMDMAWriter


class _Channel(LiteXModule):
    def __init__(self, write_port, read_port, fifo_depth=32):
        self.writer = LiteDRAMDMAWriter(write_port, fifo_depth=fifo_depth, with_csr=True)
        self.reader = LiteDRAMDMAReader(read_port,  fifo_depth=fifo_depth, with_csr=True)
        self.writer_running = self.writer.fsm.ongoing("RUN")
        self.drained        = self.writer._done.status & ~self.writer.fifo.source.valid


class _StreamWatch(LiteXModule):
    """Debug: counts stream protocol violations on one endpoint (valid/ready/data signals)."""
    def __init__(self, valid, ready, data, clear, width=32):
        self.data_change = Signal(width)  # data changed while valid was held and not accepted
        self.valid_drop  = Signal(width)  # valid dropped before the beat was accepted
        self.beats       = Signal(width)
        stalled = Signal()
        data_d  = Signal(len(data))
        self.sync += [
            stalled.eq(valid & ~ready),
            data_d.eq(data),
            If(clear,
                self.data_change.eq(0), self.valid_drop.eq(0), self.beats.eq(0),
            ).Else(
                If(stalled & valid & (data != data_d), self.data_change.eq(self.data_change + 1)),
                If(stalled & ~valid, self.valid_drop.eq(self.valid_drop + 1)),
                If(valid & ready, self.beats.eq(self.beats + 1)),
            ),
        ]


class PCIeDDRBridge(LiteXModule):
    def __init__(self, dma, channels, counter_width=32, with_debug=False):
        """dma: LitePCIeDMA (user-side source/sink). channels: list of (write_port, read_port) native ports, same
        data width, in the DMA's clock domain."""
        dw_pcie = len(dma.source.data)
        dw_dram = channels[0][0].data_width
        nchan   = len(channels)

        self.sel = CSRStorage(max(1, log2_int(nchan, need_pow2=False)),
            description="DDR channel used by both directions (0 = CH0, 1 = CH1).")
        self.drained = CSRStatus(nchan, description="Per channel: H2C writer done and its data FIFO empty.")
        self.clear   = CSRStorage(description="Write any value: clear the byte counters.")
        self.h2c_beats   = CSRStatus(counter_width, description=f"{dw_dram//8}-byte words written to DDR since clear.")
        self.h2c_dropped = CSRStatus(counter_width, description=f"{dw_pcie//8}-byte PCIe beats dropped since clear "
                                                                "(selected writer not running).")
        self.c2h_beats   = CSRStatus(counter_width, description=f"{dw_dram//8}-byte words read from DDR since clear.")

        # # #

        chans = []
        for n, (wport, rport) in enumerate(channels):
            assert wport.data_width == dw_dram and rport.data_width == dw_dram
            ch = _Channel(wport, rport)
            setattr(self, f"ch{n}", ch)
            chans.append(ch)
        self.comb += self.drained.status.eq(Cat(*[ch.drained for ch in chans]))
        sel = self.sel.storage

        # H2C: PCIe -> DDR ------------------------------------------------------------------------------
        # Drop *before* the converter while the selected writer is not running, and hold the converter in reset
        # then: a transfer stopped mid-stream (the host disables the PCIe DMA reader) must not leave a partial DDR
        # word behind for the next transfer, and stale data must never stall in front of a writer that starts later.
        h2c_running = Signal()
        self.comb += Case(sel, {n: h2c_running.eq(ch.writer_running) for n, ch in enumerate(chans)})
        self.up = up = ResetInserter()(stream.Converter(dw_pcie, dw_dram))
        self.comb += [
            up.reset.eq(~h2c_running),
            up.sink.valid.eq(dma.source.valid & h2c_running),
            up.sink.data.eq(dma.source.data),
            dma.source.ready.eq(Mux(h2c_running, up.sink.ready, 1)),  # Not running: accept and drop.
        ]
        cases = {}
        for n, ch in enumerate(chans):
            cases[n] = [
                ch.writer.sink.valid.eq(up.source.valid),
                ch.writer.sink.data.eq(up.source.data),
                up.source.ready.eq(ch.writer.sink.ready),
            ]
        self.comb += Case(sel, cases)

        # C2H: DDR -> PCIe ------------------------------------------------------------------------------
        # Converter in reset while the selected reader is disabled (an aborted transfer leaves nothing behind).
        c2h_enabled = Signal()
        self.comb += Case(sel, {n: c2h_enabled.eq(ch.reader._enable.storage) for n, ch in enumerate(chans)})
        self.down = down = ResetInserter()(stream.Converter(dw_dram, dw_pcie))
        self.comb += down.reset.eq(~c2h_enabled)
        cases = {}
        for n, ch in enumerate(chans):
            cases[n] = [
                down.sink.valid.eq(ch.reader.source.valid),
                down.sink.data.eq(ch.reader.source.data),
                ch.reader.source.ready.eq(down.sink.ready),
            ]
        self.comb += Case(sel, cases)
        if not with_debug:
            self.comb += down.source.connect(dma.sink, omit={"last"})
        else:
            self.add_debug(dma, chans, channels, down, sel, counter_width)

        # Counters -----------------------------------------------------------------------------------------
        clear = self.clear.re
        h2c, dropped, c2h = Signal(counter_width), Signal(counter_width), Signal(counter_width)
        self.sync += [
            If(clear, h2c.eq(0)).Elif(up.source.valid & up.source.ready, h2c.eq(h2c + 1)),
            If(clear, dropped.eq(0)).Elif(dma.source.valid & dma.source.ready & ~h2c_running, dropped.eq(dropped + 1)),
            If(clear, c2h.eq(0)).Elif(down.sink.valid & down.sink.ready, c2h.eq(c2h + 1)),
        ]
        self.comb += [
            self.h2c_beats.status.eq(h2c),
            self.h2c_dropped.status.eq(dropped),
            self.c2h_beats.status.eq(c2h),
        ]

    # Debug (with_debug=True only) -----------------------------------------------------------------------------
    def add_debug(self, dma, chans, channels, down, sel, counter_width):
        """C2H test pattern (full-rate counter into the PCIe DMA writer, bypassing DDR) and protocol watchers."""
        dw_pcie = len(dma.sink.data)
        self.dbg_clear       = CSRStorage(description="Write any value: clear the debug counters.")
        self.dbg_pattern     = CSRStorage(fields=[
            CSRField("enable", size=1, description="1: C2H comes from the pattern generator, not from DDR."),
        ])
        self.dbg_pattern_len = CSRStorage(32, description="Pattern length in bytes (multiple of 16); counting starts "
                                                          "when dbg_pattern.enable goes 0 -> 1.")
        clear = self.dbg_clear.re

        # Pattern generator: beat k = k replicated in every 32-bit lane.
        pat_en   = self.dbg_pattern.fields.enable
        pat_en_d = Signal()
        count    = Signal(32)
        left     = Signal(32)
        self.sync += [
            pat_en_d.eq(pat_en),
            If(pat_en & ~pat_en_d,
                count.eq(0),
                left.eq(self.dbg_pattern_len.storage[4:]),
            ).Elif(dma.sink.valid & dma.sink.ready & pat_en,
                count.eq(count + 1),
                left.eq(left - 1),
            ),
        ]
        pat_valid = pat_en & pat_en_d & (left != 0)
        self.comb += [
            If(pat_en,
                dma.sink.valid.eq(pat_valid),
                dma.sink.data.eq(Replicate(count, dw_pcie // 32)),
                down.source.ready.eq(0),
            ).Else(
                down.source.connect(dma.sink, omit={"last"}),
            ),
        ]

        # Watchers: selected reader output, converter output (= PCIe writer input), DRAM read data overflow.
        rd_valid, rd_ready, rd_data = Signal(), Signal(), Signal(len(chans[0].reader.source.data))
        rdata_overflow = Signal()
        self.comb += Case(sel, {n: [
            rd_valid.eq(ch.reader.source.valid), rd_ready.eq(ch.reader.source.ready), rd_data.eq(ch.reader.source.data),
            rdata_overflow.eq(channels[n][1].rdata.valid & ~channels[n][1].rdata.ready),
        ] for n, ch in enumerate(chans)})
        self.w_reader = _StreamWatch(rd_valid, rd_ready, rd_data, clear, counter_width)
        self.w_down   = _StreamWatch(down.source.valid, down.source.ready, down.source.data, clear, counter_width)
        self.w_sink   = _StreamWatch(dma.sink.valid, dma.sink.ready, dma.sink.data, clear, counter_width)
        overflow = Signal(counter_width)
        self.sync += If(clear, overflow.eq(0)).Elif(rdata_overflow, overflow.eq(overflow + 1))

        csrs = {
            "dbg_rdata_overflow":   overflow,
            "dbg_reader_change":    self.w_reader.data_change, "dbg_reader_vdrop": self.w_reader.valid_drop,
            "dbg_reader_beats":     self.w_reader.beats,
            "dbg_down_change":      self.w_down.data_change,   "dbg_down_vdrop":   self.w_down.valid_drop,
            "dbg_down_beats":       self.w_down.beats,
            "dbg_sink_change":      self.w_sink.data_change,   "dbg_sink_vdrop":   self.w_sink.valid_drop,
            "dbg_sink_beats":       self.w_sink.beats,
        }
        for name, sig in csrs.items():
            csr = CSRStatus(counter_width, name=name)
            self.comb += csr.status.eq(sig)
            setattr(self, name, csr)
