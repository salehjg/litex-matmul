#
# DMAProbe - debug counters around the LitePCIe DMA reader and TLP controller (LitePCIe 2026.08).
# See misc/dma_debug/README.md.
#
# Everything runs in "sys" (the S7 PHY does the CDC). Counters are 32-bit, free-running, cleared only by a write to
# dma_probe_clear, so they can be read *after* a DMA test ends (no polling while DMA runs). A snapshot of the reader
# state is taken at every falling edge of the reader enable, i.e. the state the reader was in when software stopped
# it.
#

from migen import *

from litex.gen import *
from litex.soc.interconnect.csr import CSRStorage, CSRStatus


def _hs(ep):
    return ep.valid & ep.ready


class DMAProbe(LiteXModule):
    def __init__(self, endpoint, dma, width=32):
        rd   = dma.reader
        wr   = dma.writer
        ctl  = endpoint.crossbar.controller
        cmp  = endpoint.depacketizer.cmp_source  # Completions from the host, after the depacketizer.
        tlp  = endpoint.depacketizer.tlp_cmp     # Same stream, with the raw completion status/fields.
        mpr  = endpoint.max_pending_requests
        beat_bytes = endpoint.phy.data_width//8

        self.clear = CSRStorage(description="Write any value: clear all counters, min/max and the snapshot.")
        clear = self.clear.re

        # Counters ---------------------------------------------------------------------------------
        def counter(name, inc, amount=1, description=""):
            value = Signal(width)
            self.sync += If(clear, value.eq(0)).Elif(inc, value.eq(value + amount))
            csr = CSRStatus(width, name=name, description=description)
            self.comb += csr.status.eq(value)
            setattr(self, name, csr)

        # Host completions (depacketizer output).
        cmp_first = _hs(cmp) & cmp.first
        counter("cmp_tlps",   cmp_first,                 description="Completion TLPs from the host.")
        counter("cmp_beats",  _hs(cmp),                  description="Completion beats from the host.")
        counter("cmp_bytes",  cmp_first, cmp.len << 2,   description="Completion payload bytes (len field).")
        counter("cmp_ends",   cmp_first & cmp.end,       description="Completions that end a request (len == byte_count).")
        counter("cmp_errs",   cmp_first & cmp.err,       description="Completions with status != SC.")
        counter("cmp_len16",  cmp_first & (cmp.len == 16),  description="64 B completions.")
        counter("cmp_len32",  cmp_first & (cmp.len == 32),  description="128 B completions.")
        counter("cmp_len64",  cmp_first & (cmp.len == 64),  description="256 B completions.")
        counter("cmp_len128", cmp_first & (cmp.len == 128), description="512 B completions.")
        counter("cmp_len_other", cmp_first & ~((cmp.len == 16) | (cmp.len == 32) | (cmp.len == 64) | (cmp.len == 128)),
            description="Completions of any other length.")

        # TLP controller (read requests out to the host, completions in).
        req_out = ctl.master_out.sink
        cmp_in  = ctl.master_out.source
        counter("ctl_rd_reqs",    _hs(req_out) & req_out.first & ~req_out.we, description="Read requests sent to the host.")
        counter("ctl_cmp_tlps",   _hs(cmp_in) & cmp_in.first,                  description="Completion TLPs into the controller.")
        counter("ctl_cmp_beats",  _hs(cmp_in),                                 description="Completion beats into the controller.")
        counter("ctl_cmp_badtag", _hs(cmp_in) & cmp_in.first & (cmp_in.tag >= mpr),
            description="Completions with a tag outside the controller's range (dropped).")

        # DMA reader.
        split = rd.splitter.source
        split_words = Signal(24)
        self.comb += split_words.eq((split.length + (beat_bytes - 1)) >> log2_int(beat_bytes))
        counter("rd_desc",          _hs(rd.table.source),     description="Descriptors taken from the reader table.")
        counter("rd_retire",        rd.table.retire,          description="Reader descriptors retired.")
        counter("rd_split_reqs",    _hs(split),               description="Split read requests issued by the reader.")
        counter("rd_split_words",   _hs(split), split_words,  description="Beats queued into pending_words (sum).")
        counter("rd_port_reqs",     _hs(rd.port.source),      description="Requests accepted by the reader port.")
        counter("rd_port_cmp_beats", _hs(rd.port.sink),       description="Completion beats into the reader.")
        counter("rd_port_cmp_done", _hs(rd.port.sink) & rd.port.sink.last & (rd.port.sink.end | rd.port.sink.err),
            description="Completions that retire a request at the reader (last & (end | err)).")
        counter("rd_port_cmp_errs", _hs(rd.port.sink) & rd.port.sink.first & rd.port.sink.err,
            description="Error completions reaching the reader.")
        counter("rd_fifo_in",       _hs(rd.data_fifo.sink),   description="Beats into the reader data FIFO.")
        counter("rd_fifo_out",      _hs(rd.data_fifo.source), description="Beats out of the reader data FIFO (pending_words dequeue).")

        # DMA writer.
        counter("wr_beats", _hs(wr.sink), description="Beats into the DMA writer.")

        # Shadow of the reader's pending_words (signed, reset like the original when the reader is disabled) ----
        pending     = Signal((width, True))
        pending_min = Signal((width, True))
        pending_max = Signal((width, True))
        queue   = Mux(_hs(split), split_words, 0)
        dequeue = _hs(rd.data_fifo.source)
        self.sync += [
            If(~rd.enable,
                pending.eq(0)
            ).Else(
                pending.eq(pending + queue - dequeue)
            ),
            If(clear,
                pending_min.eq(0),
                pending_max.eq(0),
            ).Elif(rd.enable,
                If(pending < pending_min, pending_min.eq(pending)),
                If(pending > pending_max, pending_max.eq(pending)),
            ),
        ]
        self.pending_live = CSRStatus(width, description="Shadow pending_words now (signed).")
        self.pending_min  = CSRStatus(width, description="Minimum shadow pending_words since clear (signed).")
        self.pending_max  = CSRStatus(width, description="Maximum shadow pending_words since clear (signed).")
        self.comb += [
            self.pending_live.status.eq(pending),
            self.pending_min.status.eq(pending_min),
            self.pending_max.status.eq(pending_max),
        ]

        # Live state + snapshot at reader disable ---------------------------------------------------
        levels = Signal(32)
        flags  = Signal(32)
        self.comb += [
            levels[0:12].eq(rd.data_fifo.level),
            levels[12:16].eq(rd.request_metadata.level),
            levels[16:20].eq(ctl.tag_queue.level),
            levels[20:24].eq(ctl.req_queue.level),
            flags[0].eq(rd.enable),
            flags[1].eq(rd._enable.storage[1]),  # Reader FSM in IDLE (written by the reader itself).
            flags[2].eq(split.valid),
            flags[3].eq(rd.table.source.valid),
            flags[4].eq(rd.port.source.valid),
            flags[5].eq(rd.port.source.ready),
            flags[6].eq(rd.port.sink.valid),
            flags[7].eq(rd.port.sink.ready),
            flags[8].eq(rd.request_metadata.sink.ready),
            flags[9].eq(rd.request_metadata.source.valid),
            flags[10].eq(rd.data_fifo.sink.ready),
            flags[11].eq(ctl.tag_queue.source.valid),
            flags[12].eq(req_out.valid),
            flags[13].eq(req_out.ready),
        ]
        levels_desc = "[11:0] reader data FIFO level, [15:12] request_metadata level, [19:16] tag_queue level, [23:20] req_queue level."
        flags_desc  = ("bit0 reader enable, 1 reader IDLE, 2 splitter valid, 3 table valid, 4/5 port.source valid/ready, "
                       "6/7 port.sink valid/ready, 8 metadata sink ready, 9 metadata source valid, 10 data FIFO sink ready, "
                       "11 tag available, 12/13 request to host valid/ready.")
        self.levels_live = CSRStatus(32, description="Live: " + levels_desc)
        self.flags_live  = CSRStatus(32, description="Live: " + flags_desc)
        self.comb += [self.levels_live.status.eq(levels), self.flags_live.status.eq(flags)]

        enable_d = Signal()
        snap_count   = Signal(width)
        snap_pending = Signal(width)
        snap_levels  = Signal(32)
        snap_flags   = Signal(32)
        self.sync += [
            enable_d.eq(rd.enable),
            If(clear,
                snap_count.eq(0), snap_pending.eq(0), snap_levels.eq(0), snap_flags.eq(0),
            ).Elif(enable_d & ~rd.enable,  # Registered state still holds the pre-disable values this cycle.
                snap_count.eq(snap_count + 1),
                snap_pending.eq(pending),
                snap_levels.eq(levels),
                snap_flags.eq(flags),
            ),
        ]
        self.snap_count   = CSRStatus(width, description="Number of reader disables since clear (snapshots taken).")
        self.snap_pending = CSRStatus(width, description="Snapshot: shadow pending_words at the last reader disable (signed).")
        self.snap_levels  = CSRStatus(32,    description="Snapshot: " + levels_desc)
        self.snap_flags   = CSRStatus(32,    description="Snapshot: " + flags_desc)
        self.comb += [
            self.snap_count.status.eq(snap_count),
            self.snap_pending.status.eq(snap_pending),
            self.snap_levels.status.eq(snap_levels),
            self.snap_flags.status.eq(snap_flags),
        ]

        # First error completion after clear --------------------------------------------------------
        err_seen = Signal()
        err_info = Signal(32)
        err_bc   = Signal(32)
        self.sync += [
            If(clear,
                err_seen.eq(0), err_info.eq(0), err_bc.eq(0),
            ).Elif(_hs(tlp) & tlp.first & (tlp.status != 0) & ~err_seen,
                err_seen.eq(1),
                err_info[0:3].eq(tlp.status),
                err_info[8:16].eq(tlp.tag),
                err_info[16:26].eq(tlp.length),
                err_bc[0:12].eq(tlp.byte_count),
                err_bc[16:23].eq(tlp.lower_address),
            ),
        ]
        self.err_info = CSRStatus(32, description="First error completion: [2:0] status, [15:8] tag, [25:16] length (DW).")
        self.err_bc   = CSRStatus(32, description="First error completion: [11:0] byte count, [22:16] lower address.")
        self.comb += [self.err_info.status.eq(err_info), self.err_bc.status.eq(err_bc)]
