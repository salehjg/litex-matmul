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
from litex.soc.interconnect.csr import CSRStorage, CSRStatus

from litedram.frontend.dma import LiteDRAMDMAReader, LiteDRAMDMAWriter


class _Channel(LiteXModule):
    def __init__(self, write_port, read_port, fifo_depth=32):
        self.writer = LiteDRAMDMAWriter(write_port, fifo_depth=fifo_depth, with_csr=True)
        self.reader = LiteDRAMDMAReader(read_port,  fifo_depth=fifo_depth, with_csr=True)
        self.writer_running = self.writer.fsm.ongoing("RUN")
        self.drained        = self.writer._done.status & ~self.writer.fifo.source.valid


class PCIeDDRBridge(LiteXModule):
    def __init__(self, dma, channels, counter_width=32):
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
        self.comb += down.source.connect(dma.sink, omit={"last"})

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
