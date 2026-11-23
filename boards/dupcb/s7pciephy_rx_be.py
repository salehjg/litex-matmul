#
# s7pciephy_rx_be.py - S7PCIEPHY with correct RX byte enables on the 128-bit interface.
#
# LitePCIe 2026.08's S7PCIEPHY feeds the RX byte enables from the core's m_axis_rx_tkeep and uses rx_is_eof only for
# "last". On the 7-series 128-bit interface the end position of a TLP is in rx_is_eof[3:0], not in tkeep, so the last
# beat claims all 4 DWs valid. Since LitePCIe commit f4dbb63 the header extracter emits a "tail" beat whenever DW3 of
# the last beat is valid, so every TLP whose last beat is not full gets one extra beat carrying a garbage DW: on this
# host each 256 B completion arrives as 17 beats instead of 16. The DMA reader then dequeues more beats than it
# queued (pending_words wraps after ~128 requests and the reader stops for good), and on equal PHY/DMA widths the
# garbage DW reaches the DMA stream. Measured with misc/dma_debug (docs/notes1/gemm-hls-progress.md, phase 1).
#
# Fix: derive the byte enables of an EOF beat from rx_is_eof (DWs up to the end position) and, when the next TLP
# starts in the same beat (straddle, rx_is_sof at DW2), keep its DWs valid too. Other beats keep tkeep unchanged.
#
# 7-series 128-bit m_axis_rx_tuser: [14:10] is_sof = {valid, byte position}, [21:17] is_eof = {valid, byte position
# of the last byte}; the DW index is bits [3:2] of the position.
#

from migen import *

from litepcie.phy.s7pciephy import S7PCIEPHY


def rx_be_from_tuser(be, tkeep, tuser):
    """Statements driving the 16-bit RX byte enables `be` of one 128-bit beat from tuser (and tkeep off-EOF)."""
    is_sof = tuser[10:15]
    is_eof = tuser[17:22]
    sof_dw = is_sof[2:4]
    eof_dw = is_eof[2:4]
    straddle = is_sof[4] & (sof_dw > eof_dw)  # Next TLP starts after the end of this one, in the same beat.
    stmts = []
    for dw in range(4):
        valid = Mux(is_eof[4],
            (eof_dw >= dw) | (straddle & (sof_dw <= dw)),
            tkeep[4*dw])
        stmts.append(be[4*dw:4*(dw + 1)].eq(Replicate(valid, 4)))
    return stmts


class S7PCIEPHYRXBE(S7PCIEPHY):
    def __init__(self, platform, pads, data_width=64, **kwargs):
        S7PCIEPHY.__init__(self, platform, pads, data_width=data_width, **kwargs)
        if self.pcie_data_width != 128:
            return  # 64-bit uses tlast/tkeep correctly.

        # The core Instance is created in do_finalize() from pcie_phy_params: take tkeep away from rx_datapath.sink.be
        # and drive the byte enables here instead (combinational, "pcie" domain like the AXI stream itself).
        tkeep = Signal(16)
        tuser = self.pcie_phy_params["o_m_axis_rx_tuser"]
        self.pcie_phy_params["o_m_axis_rx_tkeep"] = tkeep
        self.comb += rx_be_from_tuser(self.rx_datapath.sink.be, tkeep, tuser)
