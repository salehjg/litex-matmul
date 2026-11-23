#!/usr/bin/env python3
#
# LiteX SoC for the DU-PCB-001-003 card, used as a PCIe accelerator.
#
#   PCIe Gen2 x8 -> BAR0: CSRs (LEDs, DDR, freq meters, BIOS console) + 1 DMA channel.
#   VexRiscv CPU + BIOS: CH0 is its main RAM (CPU sees the first 1 GB).
#   DDR3 CH0 + CH1: WL7DDRPHY (host-adjustable CK phase), both calibrated by the host after every
#             load (scripts/ddr_init.sh). CH1 is for your accelerator logic.
#   Clocks:   clk200 #0 -> sys (125 MHz) -> per-channel DDR MMCMs, clk200 #1 -> "accel" (200 MHz),
#             clk50 -> "clk50" domain. Freq meters on both let the host check every crystal.
#   AC24 keep-alive driven high. 3 LEDs run a chaser (host can take them over).
#
# Normally run through scripts/build.sh and scripts/load.sh.

import os
import sys

from migen import *
from litex.gen import *
from litex.build.io import DifferentialInput

from litex.build.parser import LiteXArgumentParser
from litex.soc.cores.clock import S7MMCM, S7PLL, S7IDELAYCTRL
from litex.soc.cores.led import LedChaser
from litex.soc.cores.freqmeter import FreqMeter
from litex.soc.integration.soc_core import SoCCore
from litex.soc.integration.builder import Builder

from litedram.modules import MT41K256M8          # Same geometry/timings as the SK hynix H5TC2G83 on the card.
from litedram.core import LiteDRAMCore
from litedram.frontend.bist import LiteDRAMBISTGenerator, LiteDRAMBISTChecker
from litedram.init import get_sdram_phy_py_header

from litepcie.phy.s7pciephy import S7PCIEPHY
from litepcie.software import generate_litepcie_software

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dupcb_platform  # noqa: E402  (boards/dupcb/dupcb_platform.py)
from wl7ddrphy import DDRClocks, WL7DDRPHY  # noqa: E402
from s7pciephy_rx_be import S7PCIEPHYRXBE  # noqa: E402  (S7PCIEPHY with correct 128-bit RX byte enables)

# CRG ----------------------------------------------------------------------------------------------

class _CRG(LiteXModule):
    def __init__(self, platform, sys_clk_freq, accel_clk_freq):
        self.rst          = Signal()
        self.cd_sys       = ClockDomain()
        self.cd_idelay    = ClockDomain()
        self.cd_accel     = ClockDomain()
        self.cd_clk50     = ClockDomain(reset_less=True)

        rst_n = platform.request("cpu_reset_n")

        # 200 MHz inputs go through a BUFG first: LiteX clocks the MMCM/PLL reset logic from clkin,
        # and on local routing that logic fails hold timing.
        def clk200(n):
            se, buffered = Signal(), Signal()
            pads = platform.request("clk200", n)
            self.specials += DifferentialInput(pads.p, pads.n, se)
            self.specials += Instance("BUFG", i_I=se, o_O=buffered)
            return buffered

        # Main MMCM on 200 MHz #0: sys + IDELAY reference. Each DDR3 channel's PHY clocks come from
        # its own MMCM, cascaded from sys (wl7ddrphy.DDRClocks).
        self.mmcm = mmcm = S7MMCM(speedgrade=-2)
        self.comb += mmcm.reset.eq(~rst_n | self.rst)
        mmcm.register_clkin(clk200(0), 200e6)
        mmcm.create_clkout(self.cd_sys,    sys_clk_freq)
        mmcm.create_clkout(self.cd_idelay, 200e6)
        platform.add_false_path_constraints(self.cd_sys.clk, mmcm.clkin)
        self.idelayctrl = S7IDELAYCTRL(self.cd_idelay)

        # PLL on 200 MHz #1: clock for accelerator logic.
        self.pll = pll = S7PLL(speedgrade=-2)
        self.comb += pll.reset.eq(~rst_n | self.rst)
        pll.register_clkin(clk200(1), 200e6)
        pll.create_clkout(self.cd_accel, accel_clk_freq)
        platform.add_false_path_constraints(self.cd_sys.clk, pll.clkin)

        # 50 MHz crystal, used as is.
        self.comb += self.cd_clk50.clk.eq(platform.request("clk50"))

        platform.add_false_path_constraints(self.cd_sys.clk, self.cd_accel.clk, self.cd_clk50.clk)

# SoC ----------------------------------------------------------------------------------------------

class BaseSoC(SoCCore):
    def __init__(self, sys_clk_freq=125e6, accel_clk_freq=200e6, with_ch1=True, with_pcie=True, **kwargs):
        platform = dupcb_platform.Platform()
        sys_clk_freq = int(sys_clk_freq)

        # The card has no UART pins: the BIOS console goes over PCIe (crossover) by default.
        if kwargs.get("uart_name", "serial") == "serial":
            kwargs["uart_name"] = "crossover"

        self.crg = _CRG(platform, sys_clk_freq, accel_clk_freq)
        SoCCore.__init__(self, platform, sys_clk_freq, ident="LiteX SoC on DU-PCB-001-003", **kwargs)

        # Keep-alive: AC24 high, always (otherwise the card clears the FPGA). -----------------------
        self.comb += platform.request("keepalive").eq(1)

        # LEDs -------------------------------------------------------------------------------------
        self.leds = LedChaser(platform.request_all("user_led"), sys_clk_freq)

        # Clock checks: read clk50_freq / accel_freq over PCIe, value is in Hz. --------------------
        self.clk50_freq = FreqMeter(period=sys_clk_freq, clk=ClockSignal("clk50"))
        self.accel_freq = FreqMeter(period=sys_clk_freq, clk=ClockSignal("accel"))

        # DDR3 -------------------------------------------------------------------------------------
        # Both channels: WL7DDRPHY (boards/dupcb/wl7ddrphy.py), A7DDRPHY with a host-adjustable CK
        # phase. The stock PHY has no write timing that works for all lanes on these HR banks.
        # The host calibrates both channels after every load (scripts/ddr_init.sh): CK phase scan
        # with BIST, write latency and read leveling per lane. The BIOS still tries CH0 at boot,
        # without the phase scan, and will usually report a memtest failure; ignore it.
        self.ddr_module = module = MT41K256M8(sys_clk_freq, "1:4")
        if not self.integrated_main_ram_size:
            self.ddrphy = self.add_ddr_phy(platform, 0, sys_clk_freq)
            self.add_sdram("sdram",
                phy           = self.ddrphy,
                module        = module,
                size          = 0x4000_0000,      # CPU window: 1 GB of the 2 GB channel.
                l2_cache_size = 8192,
                with_bist     = True)             # sdram_generator/checker: traffic test.

        # DDR3 CH1: second controller for accelerator logic. ---------------------------------------
        if with_ch1:
            self.ddrphy1 = self.add_ddr_phy(platform, 1, sys_clk_freq)
            self.sdram1 = LiteDRAMCore(
                phy             = self.ddrphy1,
                geom_settings   = module.geom_settings,
                timing_settings = module.timing_settings,
                clk_freq        = sys_clk_freq)
            # BIST (pattern generator + checker), used by ddr_init.py to test the whole channel.
            self.sdram1_generator = LiteDRAMBISTGenerator(self.sdram1.crossbar.get_port())
            self.sdram1_checker   = LiteDRAMBISTChecker(self.sdram1.crossbar.get_port())
            # Your accelerator: take a native port (2 GB, 512-bit at sys_clk) like this:
            #   self.my_accel = MyAccel(self.sdram1.crossbar.get_port(clock_domain="accel"))

        # PCIe Gen2 x8 -----------------------------------------------------------------------------
        if with_pcie:
            # S7PCIEPHYRXBE, not LitePCIe's S7PCIEPHY: the stock PHY takes the 128-bit RX byte enables from tkeep,
            # and with LitePCIe 2026.08 that adds a garbage beat to completions and stalls the DMA reader
            # (s7pciephy_rx_be.py; docs/notes1/gemm-hls-progress.md, phase 1).
            self.pcie_phy = S7PCIEPHYRXBE(platform, platform.request("pcie_x8"),
                data_width = 128,
                bar0_size  = 0x20000)
            self.pcie_phy.update_config({"PCIe_Blk_Locn": "X0Y0"})
            self.add_pcie(phy=self.pcie_phy, ndmas=1)
            # Lane 0 is on X0Y23 and lane 7 on X0Y16 (README 12.4): pin the GTs explicitly.
            self.pcie_phy.add_gt_loc_constraints(
                locs    = [f"GTXE2_CHANNEL_X0Y{n}" for n in range(23, 15, -1)],
                gt_type = "gtx")

    # Channel n's PHY clocks (own MMCM, in the channel's banks' clock regions as in openTPU) + PHY.
    MMCM_LOCS = {0: "MMCME2_ADV_X0Y2", 1: "MMCME2_ADV_X0Y6"}   # CH0 banks 11-13, CH1 banks 16-18.

    def add_ddr_phy(self, platform, n, sys_clk_freq):
        esc = lambda c: c.replace("{", "{{").replace("}", "}}")   # LiteX runs .format() on these.
        clocks = DDRClocks(n, sys_clk_freq)
        setattr(self, "ddrclk" if n == 0 else f"ddrclk{n}", clocks)
        phy = ClockDomainsRenamer(clocks.domains)(
            WL7DDRPHY(platform.request("ddram", n), sys_clk_freq=sys_clk_freq))
        platform.toolchain.pre_placement_commands += [esc(c) for c in clocks.constraints()]
        platform.add_platform_command(f"set_property LOC {self.MMCM_LOCS[n]} [get_cells {clocks.name_of}]")
        if not getattr(self, "_wl7_constraints", False):
            for c in WL7DDRPHY.constraints():
                platform.add_platform_command(esc(c))
            self._wl7_constraints = True
        return phy

# Build --------------------------------------------------------------------------------------------

def main():
    parser = LiteXArgumentParser(platform=dupcb_platform.Platform, description="LiteX SoC on DU-PCB-001-003.")
    parser.add_target_argument("--sys-clk-freq",   default=125e6, type=float, help="System clock frequency.")
    parser.add_target_argument("--accel-clk-freq", default=200e6, type=float, help="Accelerator clock frequency.")
    parser.add_target_argument("--no-ch1",         action="store_true",       help="Leave DDR3 channel 1 out.")
    parser.add_target_argument("--no-pcie",        action="store_true",       help="Leave PCIe out.")
    parser.add_target_argument("--driver",         action="store_true",       help="Generate the LitePCIe Linux driver.")
    args = parser.parse_args()

    soc = BaseSoC(
        sys_clk_freq   = args.sys_clk_freq,
        accel_clk_freq = args.accel_clk_freq,
        with_ch1       = not args.no_ch1,
        with_pcie      = not args.no_pcie,
        **parser.soc_argdict)

    builder_args = parser.builder_argdict
    builder = Builder(soc, **builder_args)
    if builder_args.get("csr_csv") is None:
        builder.csr_csv = os.path.join(builder.output_dir, "csr.csv")  # Host tools read register addresses here.

    if args.build:
        builder.build(**parser.toolchain_argdict)

        # Init data for ddr_init.py (host-side calibration). Both channels use the same PHY settings.
        phy = getattr(soc, "ddrphy1", None) or getattr(soc, "ddrphy", None)
        if phy is not None:
            with open(os.path.join(builder.output_dir, "sdram_init.py"), "w") as f:
                f.write(get_sdram_phy_py_header(phy.settings, soc.ddr_module.timing_settings))

    if args.driver and not args.no_pcie:
        generate_litepcie_software(soc, os.path.join(builder.output_dir, "driver"))

    if args.load:
        soc.platform.create_programmer().load_bitstream(builder.get_bitstream_filename(mode="sram"))

if __name__ == "__main__":
    main()
