#!/usr/bin/env python3
#
# gemm_soc.py - GemmSoC: the DU-PCB base SoC (boards/dupcb/target.py) without the CPU, with the host <-> DDR bridge on
# both DDR3 channels (hw/gemm/ddr_dma.py). The HLS GEMM kernel is added in phase 4 (docs/notes1/gemm-hls-plan.md).
#
#   source scripts/env.sh
#   python3 hw/gemm/gemm_soc.py --build                          # bitstream + driver into build/gemm_soc/
#   python3 hw/gemm/gemm_soc.py --build --no-compile-gateware    # fast check
#   python3 hw/gemm/gemm_soc.py --load                           # JTAG, then scripts/pcie_rescan.sh
#

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "boards", "dupcb"))
sys.path.insert(0, HERE)

import target as base                                   # noqa: E402  boards/dupcb/target.py
import dupcb_platform                                   # noqa: E402
from ddr_dma import PCIeDDRBridge                       # noqa: E402

from litex.build.parser import LiteXArgumentParser      # noqa: E402
from litex.soc.integration.builder import Builder       # noqa: E402


class GemmSoC(base.BaseSoC):
    def __init__(self, ddr_dma_debug=False, **kwargs):
        # No CPU: both DDR channels belong to the host DMA and (later) the kernel. CH0 stays add_sdram (its Wishbone
        # port is idle without a CPU), so the DDR CSR names, and with them ddr_init.py, are unchanged. DDR init comes
        # from the host only (scripts/ddr_init.sh).
        kwargs["cpu_type"] = None
        kwargs.setdefault("uart_name", "crossover")
        base.BaseSoC.__init__(self, with_ch1=True, with_pcie=True, **kwargs)

        channels = []
        for sdram in [self.sdram, self.sdram1]:
            channels.append((sdram.crossbar.get_port(mode="write"), sdram.crossbar.get_port(mode="read")))
        self.ddr_dma = PCIeDDRBridge(self.pcie_dma0, channels, with_debug=ddr_dma_debug)


def main():
    parser = LiteXArgumentParser(platform=dupcb_platform.Platform, description="GemmSoC on the DU-PCB-001-003.")
    parser.add_target_argument("--sys-clk-freq", default=125e6, type=float, help="System clock frequency.")
    parser.add_target_argument("--ddr-dma-debug", action="store_true",
        help="DDR bridge debug: C2H test pattern + protocol watchers (build/gemm_soc_dbg).")
    args = parser.parse_args()
    if args.output_dir is None:
        args.output_dir = os.path.join(ROOT, "build", "gemm_soc_dbg" if args.ddr_dma_debug else "gemm_soc")

    soc = GemmSoC(ddr_dma_debug=args.ddr_dma_debug, sys_clk_freq=args.sys_clk_freq, **parser.soc_argdict)

    builder = Builder(soc, **parser.builder_argdict)
    builder.csr_csv = os.path.join(builder.output_dir, "csr.csv")
    if args.build:
        builder.build(**parser.toolchain_argdict)
        phy = getattr(soc, "ddrphy1", None) or getattr(soc, "ddrphy", None)
        with open(os.path.join(builder.output_dir, "sdram_init.py"), "w") as f:
            f.write(base.get_sdram_phy_py_header(phy.settings, soc.ddr_module.timing_settings))
        base.generate_litepcie_software(soc, os.path.join(builder.output_dir, "driver"))

    if args.load:  # JTAG, volatile. Then: scripts/pcie_rescan.sh
        os.chdir(builder.output_dir)
        soc.platform.create_programmer().load_bitstream(builder.get_bitstream_filename(mode="sram"))


if __name__ == "__main__":
    main()
