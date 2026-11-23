#
# LiteX platform for the DU-PCB-001-003 (SCH-001-003) Kintex-7 480T PCIe card.
#
# Pins come from the board README (my-fpga-ws/boards/DU-PCB-001-003, sections 12.x) and its master
# XDC (constraints/dupcb001003.xdc). Verified on unit 0265: LEDs, clk50, clk200 #0, DDR3 CH0.
# Inherited from the YPCB-00338-1P1 and not yet verified on this card: DDR3 CH1, clk200 #1, PCIe.
#
# THE ONE RULE: pin AC24 ("keepalive") must stay high after configuration, or the card clears the
# FPGA ~250 ms later. The target drives it to 1 and this file pulls every unused pin up as well.

from litex.build.generic_platform import Pins, Subsignal, IOStandard, Misc
from litex.build.xilinx import Xilinx7SeriesPlatform, VivadoProgrammer


# DDR3 channel helper ------------------------------------------------------------------------------
# Both channels: 64 data bits (8 x8 chips) used; the 9th (ECC) chip is not connected here.
# I/O like the MIG preset that calibrated on this card: SSTL15, 50 ohm split termination, LVCMOS15 reset.

def _ddram(n, a, ba, ras_n, cas_n, we_n, cs_n, cke, odt, reset_n, clk_p, clk_n, dq, dqs_p, dqs_n):
    term = Misc("IN_TERM=UNTUNED_SPLIT_50")
    return ("ddram", n,
        Subsignal("a",       Pins(a),       IOStandard("SSTL15")),
        Subsignal("ba",      Pins(ba),      IOStandard("SSTL15")),
        Subsignal("ras_n",   Pins(ras_n),   IOStandard("SSTL15")),
        Subsignal("cas_n",   Pins(cas_n),   IOStandard("SSTL15")),
        Subsignal("we_n",    Pins(we_n),    IOStandard("SSTL15")),
        Subsignal("cs_n",    Pins(cs_n),    IOStandard("SSTL15")),
        Subsignal("cke",     Pins(cke),     IOStandard("SSTL15")),
        Subsignal("odt",     Pins(odt),     IOStandard("SSTL15")),
        Subsignal("reset_n", Pins(reset_n), IOStandard("LVCMOS15")),
        Subsignal("clk_p",   Pins(clk_p),   IOStandard("DIFF_SSTL15")),
        Subsignal("clk_n",   Pins(clk_n),   IOStandard("DIFF_SSTL15")),
        Subsignal("dq",      Pins(dq),      IOStandard("SSTL15"),      term),
        Subsignal("dqs_p",   Pins(dqs_p),   IOStandard("DIFF_SSTL15"), term),
        Subsignal("dqs_n",   Pins(dqs_n),   IOStandard("DIFF_SSTL15"), term),
        Misc("SLEW=FAST"),
    )

# IOs ----------------------------------------------------------------------------------------------

_io = [
    # Clocks / reset.
    ("clk50",  0, Pins("AA28"), IOStandard("LVCMOS18")),                     # Y2, bank 14.
    ("clk200", 0,                                                             # Bank 12 (DDR CH0 side).
        Subsignal("p", Pins("AH27")),
        Subsignal("n", Pins("AH28")),
        IOStandard("DIFF_SSTL15"), Misc("DQS_BIAS=TRUE"),
    ),
    ("clk200", 1,                                                             # Bank 17 (DDR CH1 side).
        Subsignal("p", Pins("G25")),
        Subsignal("n", Pins("G26")),
        IOStandard("DIFF_SSTL15"), Misc("DQS_BIAS=TRUE"),
    ),
    ("cpu_reset_n", 0, Pins("R28"), IOStandard("LVCMOS18")),                  # SW2, active low.

    # Keep-alive: must be high after configuration (see top of file).
    ("keepalive", 0, Pins("AC24"), IOStandard("LVCMOS18")),

    # Bracket LEDs, active high: red, green, yellow.
    ("user_led", 0, Pins("P30"), IOStandard("LVCMOS18"), Misc("DRIVE=8"), Misc("SLEW=SLOW")),
    ("user_led", 1, Pins("M30"), IOStandard("LVCMOS18"), Misc("DRIVE=8"), Misc("SLEW=SLOW")),
    ("user_led", 2, Pins("N30"), IOStandard("LVCMOS18"), Misc("DRIVE=8"), Misc("SLEW=SLOW")),

    # LM73 temperature sensor (I2C address 0x4A) and PCIe SMBus. Defined, not used by the target.
    ("lm73", 0,
        Subsignal("scl",   Pins("N24")),
        Subsignal("sda",   Pins("N25")),
        Subsignal("alert", Pins("P25")),
        IOStandard("LVCMOS18"),
    ),
    ("pcie_smbus", 0,
        Subsignal("scl", Pins("R26")),
        Subsignal("sda", Pins("R27")),
        IOStandard("LVCMOS18"),
    ),

    # PCIe Gen2 x8 (GTX quads 115/116, lane 0 on GTXE2_CHANNEL_X0Y23).
    ("pcie_x8", 0,
        Subsignal("rst_n", Pins("Y26"), IOStandard("LVCMOS18"), Misc("PULLUP=TRUE")),
        Subsignal("clk_p", Pins("J8")),
        Subsignal("clk_n", Pins("J7")),
        Subsignal("rx_p",  Pins("H6 J4 K6 L4 M6 P6 R4 T6")),
        Subsignal("rx_n",  Pins("H5 J3 K5 L3 M5 P5 R3 T5")),
        Subsignal("tx_p",  Pins("F2 H2 K2 M2 N4 P2 T2 U4")),
        Subsignal("tx_n",  Pins("F1 H1 K1 M1 N3 P1 T1 U3")),
    ),

    # DDR3 channel 0: chips U5-U12 (+ECC U13), banks 11/12/13.
    _ddram(0,
        a       = "AK27 AN23 AL24 AK26 AH24 AH25 AL26 AJ24 AJ25 AM23 AL28 AL25 AM25 AK24 AM27",
        ba      = "AM26 AP24 AN28",
        ras_n   = "AJ29", cas_n = "AP26", we_n = "AN27", cs_n = "AK28",
        cke     = "AP27", odt   = "AK29", reset_n = "AD31",
        clk_p   = "AN25", clk_n = "AP25",
        dq      = " ".join([
            "AG17 AG16 AH17 AJ19 AH18 AH19 AJ16 AJ17",  # Byte 0, U5.
            "AL20 AN17 AL19 AM16 AL18 AL16 AM20 AN18",  # Byte 1, U6.
            "AL23 AN20 AK23 AP19 AN22 AN19 AM22 AP20",  # Byte 2, U7.
            "AJ21 AH22 AK21 AG21 AG22 AG20 AH23 AG23",  # Byte 3, U8.
            "AJ32 AK32 AK31 AL30 AL34 AL31 AK34 AL29",  # Byte 4, U9.
            "AJ34 AH32 AJ30 AH34 AF31 AG30 AG31 AF30",  # Byte 5, U10.
            "AE32 AC33 AF33 AC32 AD34 AC34 AE33 AE31",  # Byte 6, U11.
            "AE26 AF29 AE24 AF28 AF24 AG25 AF26 AF25",  # Byte 7, U12.
        ]),
        dqs_p   = "AK16 AM17 AP21 AH20 AK33 AG33 AE34 AE27",
        dqs_n   = "AK17 AM18 AP22 AJ20 AL33 AH33 AF34 AE28",
    ),

    # DDR3 channel 1: chips U1, U14-U20 (+ECC U21), banks 16/17/18.
    _ddram(1,
        a       = "E27 C27 B28 D27 C24 D24 C25 A24 A25 J24 F26 D26 H25 D25 B26",
        ba      = "F24 J25 E24",
        ras_n   = "E28", cas_n = "E26", we_n = "F25", cs_n = "F28",
        cke     = "A28", odt   = "B27", reset_n = "F18",
        clk_p   = "B25", clk_n = "A26",
        dq      = " ".join([
            "A29 B33 A31 C33 C32 A30 B30 A33",          # Byte 0, U1.
            "D31 F33 D30 D29 E33 E34 E31 F34",          # Byte 1, U14.
            "B23 A21 C23 B20 B22 A23 C20 B21",          # Byte 2, U15.
            "G31 G32 F29 F31 E29 G33 H33 H32",          # Byte 3, U16.
            "B18 C17 C19 B16 A18 A16 C18 B17",          # Byte 4, U17.
            "K27 L24 K24 L28 K26 M27 L25 M26",          # Byte 5, U18.
            "F16 E18 E16 H19 H17 H20 E17 H18",          # Byte 6, U19.
            "D20 F21 E23 G21 G20 D21 F20 F23",          # Byte 7, U20.
        ]),
        dqs_p   = "B31 D34 A19 H29 D16 K28 G17 G22",
        dqs_n   = "B32 C34 A20 H30 D17 K29 G18 G23",
    ),
]

# Platform -----------------------------------------------------------------------------------------

class Platform(Xilinx7SeriesPlatform):
    default_clk_name   = "clk200"
    default_clk_period = 1e9/200e6

    def __init__(self, toolchain="vivado"):
        Xilinx7SeriesPlatform.__init__(self, "xc7k480tiffg1156-2L", _io, name="dupcb", toolchain=toolchain)

        # Master XDC section 1: required on this card.
        self.add_platform_command("set_property BITSTREAM.CONFIG.UNUSEDPIN Pullup [current_design]")  # Keeps AC24 high too.
        self.add_platform_command("set_property CFGBVS GND [current_design]")
        self.add_platform_command("set_property CONFIG_VOLTAGE 1.8 [current_design]")
        # Master XDC section 2: BPI x16 flash boot settings (harmless for JTAG loading).
        self.add_platform_command("set_property CONFIG_MODE BPI16 [current_design]")
        self.add_platform_command("set_property BITSTREAM.CONFIG.BPI_SYNC_MODE Disable [current_design]")
        self.add_platform_command("set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]")

    def create_programmer(self):
        return VivadoProgrammer()

    def do_finalize(self, fragment):
        Xilinx7SeriesPlatform.do_finalize(self, fragment)
        self.add_period_constraint(self.lookup_request("clk50",     loose=True), 1e9/50e6)
        self.add_period_constraint(self.lookup_request("clk200", 0, loose=True), 1e9/200e6)
        self.add_period_constraint(self.lookup_request("clk200", 1, loose=True), 1e9/200e6)
