#!/usr/bin/env python3
#
# dma_probe_soc.py - the DU-PCB base SoC (boards/dupcb/target.py, unchanged) plus DMAProbe counters around the
# LitePCIe DMA reader. Debug case for the phase 1 DMA stall, see misc/dma_debug/README.md.
#
#   source scripts/env.sh
#   python3 misc/dma_debug/dma_probe_soc.py --build                          # bitstream + driver into build/dma_probe/
#   python3 misc/dma_debug/dma_probe_soc.py --build --no-compile-gateware    # fast check: Verilog/XDC/driver only
#   python3 misc/dma_debug/dma_probe_soc.py --build --stock-phy              # LitePCIe's S7PCIEPHY instead of
#                                                                            # S7PCIEPHYRXBE (reproduces the DMA
#                                                                            # stall), into build/dma_probe_stock/
#   python3 misc/dma_debug/dma_probe_soc.py --load                           # JTAG load, then scripts/pcie_rescan.sh
#

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "boards", "dupcb"))
sys.path.insert(0, HERE)

import target as base                                   # noqa: E402  boards/dupcb/target.py
import dupcb_platform                                   # noqa: E402
from dma_probe import DMAProbe                          # noqa: E402

from litex.build.parser import LiteXArgumentParser      # noqa: E402
from litex.soc.integration.builder import Builder       # noqa: E402


class DMAProbeSoC(base.BaseSoC):
    def __init__(self, **kwargs):
        base.BaseSoC.__init__(self, with_pcie=True, **kwargs)

    def finalize(self, *args, **kwargs):
        # LitePCIe creates the TLP controller in the crossbar's do_finalize, once all masters are registered (all of
        # them are, in add_pcie). Finalize the endpoint first so the controller exists, then add the probe before the
        # SoC collects its CSRs.
        if not self.finalized and not hasattr(self, "dma_probe"):
            self.pcie_endpoint.finalize()
            self.dma_probe = DMAProbe(self.pcie_endpoint, self.pcie_dma0)
        base.BaseSoC.finalize(self, *args, **kwargs)


def main():
    parser = LiteXArgumentParser(platform=dupcb_platform.Platform, description="DU-PCB SoC + LitePCIe DMA probes.")
    parser.add_target_argument("--sys-clk-freq", default=125e6, type=float, help="System clock frequency.")
    parser.add_target_argument("--stock-phy", action="store_true",
        help="Use LitePCIe's S7PCIEPHY instead of S7PCIEPHYRXBE (reproduces the DMA stall); output in build/dma_probe_stock.")
    args = parser.parse_args()
    if args.output_dir is None:
        args.output_dir = os.path.join(ROOT, "build", "dma_probe_stock" if args.stock_phy else "dma_probe")

    if args.stock_phy:
        # BaseSoC (target.py) builds the PHY by the module-level name S7PCIEPHYRXBE.
        base.S7PCIEPHYRXBE = base.S7PCIEPHY

    soc = DMAProbeSoC(sys_clk_freq=args.sys_clk_freq, **parser.soc_argdict)

    builder = Builder(soc, **parser.builder_argdict)
    builder.csr_csv = os.path.join(builder.output_dir, "csr.csv")
    # Same flow and extra outputs as boards/dupcb/target.py: DDR init data for ddr_init.py, LitePCIe driver.
    if args.build:
        builder.build(**parser.toolchain_argdict)
        phy = getattr(soc, "ddrphy1", None) or getattr(soc, "ddrphy", None)
        with open(os.path.join(builder.output_dir, "sdram_init.py"), "w") as f:
            f.write(base.get_sdram_phy_py_header(phy.settings, soc.ddr_module.timing_settings))
        base.generate_litepcie_software(soc, os.path.join(builder.output_dir, "driver"))

    if args.load:  # JTAG, volatile. Vivado writes its logs into the build directory. Then: scripts/pcie_rescan.sh
        os.chdir(builder.output_dir)
        soc.platform.create_programmer().load_bitstream(builder.get_bitstream_filename(mode="sram"))


if __name__ == "__main__":
    main()
