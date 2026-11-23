#
# wl7ddrphy.py - DDR3 PHY with host-adjustable CK phase, for the HR banks of the DU-PCB-001-003.
#
# Adapted from openTPU's tools/litedram/wl7ddrphy.py (Apache-2.0; my-fpga-ws/submodules/openTPU,
# docs/litedram.md sections 7-8), itself derived from LiteDRAM's A7DDRPHY (BSD-2-Clause:
# Copyright (c) 2015-2020 Florent Kermarrec, (c) 2015 Sebastien Bourdeauducq, (c) 2021 Antmicro).
# Changes here: one write clock group per channel (openTPU dropped its second group after it failed
# on the card), no rd_reg option, write_latency_calibration off in the settings (the host does it).
#
# Why: HR banks have no ODELAY, so there is no write leveling. The stock A7DDRPHY can only move DQS,
# which shifts it against CK *and* against its own DQ. On these cards that leaves no write timing
# that works for every byte lane under traffic (measured by openTPU, and by us: CH1 BIST ~21% bad).
#
# What this PHY does instead: each channel gets its own MMCM (DDRClocks). CK and the commands run on
# two outputs with fine phase shift (1/56 VCO period per step, 17.9 ps at 125 MHz sys), which the
# host moves. DQ, DQS (DQ + 90 deg) and the read capture stay on static outputs. So the host moves
# CK against the DQ/DQS pair, which is what write leveling does, and each lane still picks its own
# write latency (bitslip). scripts/ddr_init.sh scans the phase with a BIST at every step and picks
# the centre of the range where every lane passes.
#
# Clock domains (renamed per channel with ClockDomainsRenamer(DDRClocks.domains)):
#   sys_ck, sys4x_ck      CLKDIV / CLK of CK and the commands   (fine phase shift, host-controlled)
#   sys_w                 CLKDIV of every DQ/DQS OSERDES and ISERDES (static, in phase with sys)
#   sys4x_w, sys4x_w_dqs  CLK of DQ (and the read ISERDES) / of DQS at +90 deg (static)
# The PHY logic itself runs in sys. Commands cross sys -> sys_ck through registers on sys's falling
# edge; the host keeps sys_ck within half a tCK of sys (ddr_init.py wraps the phase), so they keep
# about 3 ns of setup and hold.

from functools import reduce
from operator import or_
import math

from migen import *

from litex.gen import LiteXModule
from litex.soc.interconnect.csr import CSR, CSRStatus, CSRStorage, AutoCSR

from litedram.common import *
from litedram.phy.dfi import *

# Per-channel clocks -------------------------------------------------------------------------------

class DDRClocks(LiteXModule):
    """One channel's PHY clocks from one MMCM, cascaded from sys (DIVCLK 1, x8 VCO, feedback through
    a BUFG) so its outputs keep a fixed phase to sys. CSRs: reset/locked, and the CK phase:
    each write to ck_shift moves CK one fine step later (1) or earlier (0) once ck_busy is 0;
    ck_steps counts the steps since the MMCM came out of reset."""
    def __init__(self, n, sys_clk_freq):
        self.name_of = f"ddr{n}_mmcm"
        self.domains = {
            "sys_ck":      f"ddr{n}_ck",
            "sys4x_ck":    f"ddr{n}_ck4x",
            "sys_w":       f"ddr{n}_w",
            "sys4x_w":     f"ddr{n}_w4x",
            "sys4x_w_dqs": f"ddr{n}_w4x_dqs",
        }
        # (output, divide, static phase, fine phase shift) per PHY clock.
        outs = {
            "sys_ck":      (0, 8,  0.0, True),
            "sys4x_ck":    (1, 2,  0.0, True),
            "sys_w":       (2, 8,  0.0, False),
            "sys4x_w":     (3, 2,  0.0, False),
            "sys4x_w_dqs": (4, 2, 90.0, False),
        }
        self.vco_freq = 8*sys_clk_freq
        self.steps_per_tck = round(56*self.vco_freq/(4*sys_clk_freq))   # 112

        self.reset    = CSRStorage(1, description="1: hold this channel's MMCM in reset.")
        self.locked   = CSRStatus(1)
        self.ck_shift = CSRStorage(1, description="Write: move CK one step; 1 = later, 0 = earlier.")
        self.ck_busy  = CSRStatus(1)
        self.ck_steps = CSRStatus(32, description="CK phase in fine steps since reset (signed).")

        # # #

        params = {}
        for name, (i, div, phase, fine) in outs.items():
            cd = ClockDomain(self.domains[name], reset_less=True)
            setattr(self, "cd_" + cd.name, cd)
            o = Signal()
            params[f"p_CLKOUT{i}_DIVIDE" + ("_F" if i == 0 else "")] = float(div) if i == 0 else div
            params[f"p_CLKOUT{i}_PHASE"]       = phase
            params[f"p_CLKOUT{i}_DUTY_CYCLE"]  = 0.5
            params[f"p_CLKOUT{i}_USE_FINE_PS"] = "TRUE" if fine else "FALSE"
            params[f"o_CLKOUT{i}"]             = o
            self.specials += Instance("BUFG", i_I=o, o_O=cd.clk)

        locked, rst = Signal(), Signal()
        psen, psincdec, psdone = Signal(), Signal(), Signal()
        fb, fb_buf = Signal(), Signal()
        self.comb += rst.eq(ResetSignal("sys") | self.reset.storage)
        self.specials += Instance("MMCME2_ADV", name=self.name_of,
            p_BANDWIDTH="OPTIMIZED", p_COMPENSATION="ZHOLD", p_STARTUP_WAIT="FALSE",
            p_CLKIN1_PERIOD=1e9/sys_clk_freq, p_REF_JITTER1=0.01, p_DIVCLK_DIVIDE=1,
            p_CLKFBOUT_MULT_F=8.0, p_CLKFBOUT_PHASE=0.0, p_CLKFBOUT_USE_FINE_PS="FALSE",
            i_CLKIN1=ClockSignal("sys"), i_CLKIN2=0, i_CLKINSEL=1,
            i_CLKFBIN=fb_buf, o_CLKFBOUT=fb, i_RST=rst, i_PWRDWN=0, o_LOCKED=locked,
            i_PSCLK=ClockSignal("sys"), i_PSEN=psen, i_PSINCDEC=psincdec, o_PSDONE=psdone,
            i_DCLK=0, i_DEN=0, i_DWE=0, i_DADDR=0, i_DI=0,
            **params)
        self.specials += Instance("BUFG", i_I=fb, o_O=fb_buf)

        # Phase-shift control: one step per ck_shift write, counted while the MMCM is running.
        busy  = Signal()
        steps = Signal((32, True))
        self.sync += [
            psen.eq(0),
            If(self.ck_shift.re & ~busy,
                psen.eq(1), psincdec.eq(self.ck_shift.storage[0]), busy.eq(1),
                If(self.ck_shift.storage[0], steps.eq(steps + 1)).Else(steps.eq(steps - 1)),
            ).Elif(psdone, busy.eq(0)),
            If(~locked | self.reset.storage, busy.eq(0), steps.eq(0)),
        ]
        self.comb += [
            self.locked.status.eq(locked),
            self.ck_busy.status.eq(busy),
            self.ck_steps.status.eq(steps),
        ]

    def constraints(self, ns=1.0):
        """Tcl (before placement): commands cross between sys (falling edge) and the shifted
        sys_ck, which the host keeps within half a tCK of sys: `ns` of uncertainty both ways."""
        pin = lambda p: f"[get_pins {self.name_of}/{p}]"
        sys_, ck = (f"[get_clocks -of_objects {pin(p)}]" for p in ("CLKIN1", "CLKOUT0"))
        return [f"set_clock_uncertainty -{k} {ns:.3f} -from {a} -to {b}"
                for a, b in ((sys_, ck), (ck, sys_)) for k in ("setup", "hold")]

# PHY ----------------------------------------------------------------------------------------------

class WL7DDRPHY(Module, AutoCSR):
    """A7DDRPHY (DDR3, 1:4, no ODELAY) on the DDRClocks domains. CSRs and settings are A7DDRPHY's
    (phytype A7DDRPHY), plus dly_sel_bits: which DQ bits of the lanes in dly_sel the read/write
    bitslip strobes act on (reset 0xFF). Some bits' ISERDES frame the word one CLK off the rest
    of their lane (openTPU: always a few on CH1, different pins per build); the host gives those
    bits their own read bitslip."""
    def __init__(self, pads, sys_clk_freq, iodelay_clk_freq=200e6, cl=None, cwl=None):
        memtype, nphases = "DDR3", 4
        pads        = PHYPadsCombiner(pads)
        tck         = 2/(2*nphases*sys_clk_freq)
        addressbits = len(pads.a)
        bankbits    = len(pads.ba)
        nranks      = 1 if not hasattr(pads, "cs_n") else len(pads.cs_n)
        databits    = len(pads.dq)
        strobes     = len(pads.dqs_p)
        assert databits == 8*strobes

        # Parameters (A7DDRPHY's) ------------------------------------------------------------------
        half_sys8x_taps = math.floor(tck/(4*{200e6: 78e-12, 300e6: 52e-12}[iodelay_clk_freq]))
        cl  = get_default_cl(memtype, tck)  if cl  is None else cl
        cwl = get_default_cwl(memtype, tck) if cwl is None else cwl
        cl_sys_latency  = get_sys_latency(nphases, cl)
        cwl_sys_latency = get_sys_latency(nphases, cwl)
        rdphase = get_sys_phase(nphases, cl_sys_latency, cl)
        wrphase = get_sys_phase(nphases, cwl_sys_latency, cwl)
        # No ODELAY: read data one memory-clock phase into the ISERDESE2 word (as A7DDRPHY).
        phase_cycles, rdphase = divmod(rdphase + 1, nphases)
        cl_sys_latency -= phase_cycles

        # Registers (A7DDRPHY's + dly_sel_bits) ----------------------------------------------------
        self._rst                 = CSRStorage()
        self._dly_sel             = CSRStorage(strobes)
        self._dly_sel_bits        = CSRStorage(8, reset=0xFF)
        self._half_sys8x_taps     = CSRStorage(5, reset=half_sys8x_taps)
        self._wlevel_en           = CSRStorage()
        self._wlevel_strobe       = CSR()
        self._rdly_dq_rst         = CSR()
        self._rdly_dq_inc         = CSR()
        self._rdly_dq_bitslip_rst = CSR()
        self._rdly_dq_bitslip     = CSR()
        self._wdly_dq_bitslip_rst = CSR()
        self._wdly_dq_bitslip     = CSR()
        self._rdphase             = CSRStorage(int(math.log2(nphases)), reset=rdphase)
        self._wrphase             = CSRStorage(int(math.log2(nphases)), reset=wrphase)

        rdly_dq_rst         = self._rdly_dq_rst.wr_stb
        rdly_dq_inc         = self._rdly_dq_inc.wr_stb
        rdly_dq_bitslip_rst = self._rdly_dq_bitslip_rst.wr_stb
        rdly_dq_bitslip     = self._rdly_dq_bitslip.wr_stb
        wdly_dq_bitslip_rst = self._wdly_dq_bitslip_rst.wr_stb
        wdly_dq_bitslip     = self._wdly_dq_bitslip.wr_stb
        bit_sel  = self._dly_sel_bits.storage
        lane_all = Signal()
        self.comb += lane_all.eq(bit_sel == 0xFF)

        self.settings = PhySettings(
            phytype                   = "A7DDRPHY",
            memtype                   = memtype,
            databits                  = databits,
            strobes                   = strobes,
            dfi_databits              = 2*databits,
            nranks                    = nranks,
            nphases                   = nphases,
            rdphase                   = self._rdphase.storage,
            wrphase                   = self._wrphase.storage,
            cl                        = cl,
            cwl                       = cwl,
            read_latency              = cl_sys_latency + 6,
            write_latency             = cwl_sys_latency - 1,
            cmd_latency               = 0,
            cmd_delay                 = None,
            write_leveling            = False,
            write_dq_dqs_training     = False,
            write_latency_calibration = False,  # Host does it (the 2026.08 BIOS can't compile it here).
            read_leveling             = True,
            delays                    = 32,
            bitslips                  = 8,
            with_dm                   = hasattr(pads, "dm"),
        )

        self.dfi = dfi = Interface(addressbits, bankbits, nranks, 2*databits, nphases)

        # # #

        def oserdes(d, clk, clkdiv, rst, o, t1=None, tq=None):
            kw = dict(i_TCE=1, i_T1=t1, o_TQ=tq) if t1 is not None else {}
            return Instance("OSERDESE2",
                p_SERDES_MODE="MASTER", p_DATA_WIDTH=2*nphases, p_TRISTATE_WIDTH=1,
                p_DATA_RATE_OQ="DDR", p_DATA_RATE_TQ="BUF",
                i_RST=rst, i_CLK=clk, i_CLKDIV=clkdiv,
                **{f"i_D{n + 1}": d[n] for n in range(8)},
                i_OCE=1, o_OQ=o, **kw)

        def nreg(x):
            """x through FDREs on sys's falling edge (clock inverted in the slice, no LUT in the
            clock path), on its way to the shifted sys_ck."""
            r = Signal(len(x))
            for i in range(len(x)):
                self.specials += Instance("FDRE", p_INIT=0, p_IS_C_INVERTED=1,
                    i_C=ClockSignal("sys"), i_CE=1, i_R=0, i_D=x[i], o_Q=r[i])
            return r

        def serdes_rst(x, clk):
            """x registered again in the serializers' CLKDIV domain, close to its SERDES, so all
            SERDES of a word leave reset on the same edge (constraints() bounds the route)."""
            r = Signal()
            self.specials += Instance("FDRE", name="wlrst", p_INIT=1, i_C=ClockSignal(clk),
                i_CE=1, i_R=0, i_D=x, o_Q=r, attr={("DONT_TOUCH", "TRUE")})
            return r

        sys_rst = Signal(reset=1, reset_less=True)
        self.sync += sys_rst.eq(ResetSignal("sys") | self._rst.storage)
        ck_rst_n = nreg(sys_rst)
        ck_rsts  = []

        def ck_rst():  # One reset register per 8 command/clock pads.
            if len(ck_rsts) % 8 == 0:
                ck_rsts.append(serdes_rst(ck_rst_n, "sys_ck"))
            else:
                ck_rsts.append(ck_rsts[-1])
            return ck_rsts[-1]

        # Clock ------------------------------------------------------------------------------------
        for i in range(len(pads.clk_p)):
            clk_o = Signal()
            self.specials += oserdes([(0b10101010 >> n) & 1 for n in range(8)],
                ClockSignal("sys4x_ck"), ClockSignal("sys_ck"), ck_rst(), clk_o)
            self.specials += Instance("OBUFDS", i_I=clk_o, o_O=pads.clk_p[i], o_OB=pads.clk_n[i])

        # Commands ---------------------------------------------------------------------------------
        pads_ba  = Signal(bankbits)
        commands = {"reset_n": "reset_n", "cs_n": "cs_n", "a": "address", pads_ba: "bank",
                    "ras_n": "ras_n", "cas_n": "cas_n", "we_n": "we_n", "cke": "cke", "odt": "odt"}
        for pad_name, dfi_name in commands.items():
            pad = pad_name if isinstance(pad_name, Signal) else getattr(pads, pad_name, None)
            if pad is None:
                assert pad_name in ("reset_n", "cs_n", "cke", "odt"), pad_name
                continue
            for i in range(len(pad)):
                # reset_n sits in another bank: its own reset register.
                own = dfi_name == "reset_n"
                self.specials += oserdes(
                    nreg(Cat(*[getattr(dfi.phases[n//2], dfi_name)[i] for n in range(8)])),
                    ClockSignal("sys4x_ck"), ClockSignal("sys_ck"),
                    serdes_rst(ck_rst_n, "sys_ck") if own else ck_rst(), pad[i])
        self.comb += pads.ba.eq(pads_ba)

        # DQS --------------------------------------------------------------------------------------
        dqs_oe        = Signal()
        dqs_preamble  = Signal()
        dqs_postamble = Signal()
        dqs_oe_delay  = TappedDelayLine(ntaps=2)
        dqs_pattern   = DQSPattern(wlevel_en=self._wlevel_en.storage,
            wlevel_strobe=self._wlevel_strobe.wr_stb, register=True)
        self.submodules += dqs_oe_delay, dqs_pattern
        self.comb += dqs_oe_delay.input.eq(dqs_preamble | dqs_oe | dqs_postamble)
        for i in range(strobes):
            dqs_o = Signal()
            dqs_t = Signal()
            dqs_bitslip = BitSlip(8, i=dqs_pattern.o,
                rst = (self._dly_sel.storage[i] & lane_all & wdly_dq_bitslip_rst) | self._rst.storage,
                slp = self._dly_sel.storage[i] & lane_all & wdly_dq_bitslip, cycles=1)
            self.submodules += dqs_bitslip
            self.specials += oserdes(dqs_bitslip.o, ClockSignal("sys4x_w_dqs"),
                ClockSignal("sys_w"), serdes_rst(sys_rst, "sys_w"), dqs_o,
                t1=~dqs_oe_delay.output, tq=dqs_t)
            self.specials += Instance("IOBUFDS", i_T=dqs_t, i_I=dqs_o,
                io_IO=pads.dqs_p[i], io_IOB=pads.dqs_n[i])

        # DM ---------------------------------------------------------------------------------------
        if hasattr(pads, "dm"):
            for i in range(databits//8):
                dm_i = Cat(*[dfi.phases[n//2].wrdata_mask[n%2*databits//8 + i] for n in range(8)])
                dm_o_bitslip = BitSlip(8, i=dm_i,
                    rst = (self._dly_sel.storage[i] & lane_all & wdly_dq_bitslip_rst) | self._rst.storage,
                    slp = self._dly_sel.storage[i] & lane_all & wdly_dq_bitslip, cycles=1)
                self.submodules += dm_o_bitslip
                self.specials += oserdes(dm_o_bitslip.o, ClockSignal("sys4x_w"),
                    ClockSignal("sys_w"), serdes_rst(sys_rst, "sys_w"), pads.dm[i])

        # DQ ---------------------------------------------------------------------------------------
        dq_oe       = Signal()
        dq_oe_delay = TappedDelayLine(ntaps=2)
        self.submodules += dq_oe_delay
        self.comb += dq_oe_delay.input.eq(dqs_preamble | dq_oe | dqs_postamble)
        for i in range(databits):
            lane = i//8
            dq_o         = Signal()
            dq_i         = Signal()
            dq_i_delayed = Signal()
            dq_t         = Signal()
            dq_o_bitslip = BitSlip(8,
                i   = Cat(*[dfi.phases[n//2].wrdata[n%2*databits + i] for n in range(8)]),
                rst = (self._dly_sel.storage[lane] & bit_sel[i%8] & wdly_dq_bitslip_rst) | self._rst.storage,
                slp = self._dly_sel.storage[lane] & bit_sel[i%8] & wdly_dq_bitslip, cycles=1)
            self.submodules += dq_o_bitslip
            dq_rst = serdes_rst(sys_rst, "sys_w")  # The pad's OSERDES and ISERDES.
            self.specials += oserdes(dq_o_bitslip.o, ClockSignal("sys4x_w"),
                ClockSignal("sys_w"), dq_rst, dq_o, t1=~dq_oe_delay.output, tq=dq_t)
            dq_q = Signal(8)
            dq_i_bitslip = BitSlip(8, i=dq_q,
                rst = (self._dly_sel.storage[lane] & bit_sel[i%8] & rdly_dq_bitslip_rst) | self._rst.storage,
                slp = self._dly_sel.storage[lane] & bit_sel[i%8] & rdly_dq_bitslip, cycles=1)
            self.submodules += dq_i_bitslip
            self.specials += Instance("ISERDESE2",
                p_SERDES_MODE="MASTER", p_INTERFACE_TYPE="NETWORKING", p_DATA_WIDTH=2*nphases,
                p_DATA_RATE="DDR", p_NUM_CE=1, p_IOBDELAY="IFD",
                i_RST=dq_rst, i_CLK=ClockSignal("sys4x_w"), i_CLKB=~ClockSignal("sys4x_w"),
                i_CLKDIV=ClockSignal("sys_w"), i_BITSLIP=0, i_CE1=1, i_DDLY=dq_i_delayed,
                **{f"o_Q{n + 1}": dq_q[8 - 1 - n] for n in range(8)})
            for n in range(8):
                self.comb += dfi.phases[n//2].rddata[n%2*databits + i].eq(dq_i_bitslip.o[n])
            self.specials += Instance("IDELAYE2",
                p_SIGNAL_PATTERN="DATA", p_DELAY_SRC="IDATAIN", p_CINVCTRL_SEL="FALSE",
                p_HIGH_PERFORMANCE_MODE="TRUE", p_REFCLK_FREQUENCY=iodelay_clk_freq/1e6,
                p_PIPE_SEL="FALSE", p_IDELAY_TYPE="VARIABLE", p_IDELAY_VALUE=0,
                i_C=ClockSignal("sys"),
                i_LD=(self._dly_sel.storage[lane] & rdly_dq_rst) | self._rst.storage,
                i_LDPIPEEN=0, i_CE=self._dly_sel.storage[lane] & rdly_dq_inc, i_INC=1,
                i_IDATAIN=dq_i, o_DATAOUT=dq_i_delayed)
            self.specials += Instance("IOBUF", i_I=dq_o, o_O=dq_i, i_T=dq_t, io_IO=pads.dq[i])

        # Read control path (A7DDRPHY's) -----------------------------------------------------------
        rddata_en = TappedDelayLine(
            signal = reduce(or_, [dfi.phases[i].rddata_en for i in range(nphases)]),
            ntaps  = self.settings.read_latency)
        self.submodules += rddata_en
        self.comb += [phase.rddata_valid.eq(rddata_en.output | self._wlevel_en.storage)
            for phase in dfi.phases]

        # Write control path (A7DDRPHY's) ----------------------------------------------------------
        wrtap = cwl_sys_latency - 1
        wrdata_en = TappedDelayLine(
            signal = reduce(or_, [dfi.phases[i].wrdata_en for i in range(nphases)]),
            ntaps  = wrtap + 2)
        self.submodules += wrdata_en
        self.comb += dq_oe.eq(wrdata_en.taps[wrtap])
        self.comb += If(self._wlevel_en.storage, dqs_oe.eq(1)).Else(dqs_oe.eq(dq_oe))
        self.comb += dqs_preamble.eq(wrdata_en.taps[wrtap - 1] & ~wrdata_en.taps[wrtap + 0])
        self.comb += dqs_postamble.eq(wrdata_en.taps[wrtap + 1] & ~wrdata_en.taps[wrtap + 0])

    @staticmethod
    def constraints(ns=3.0):
        """XDC: every SERDES reset register (serdes_rst) reaches its SERDES' RST within `ns`."""
        src = "[get_cells -hierarchical -filter {NAME =~ *wlrst*}]"
        dst = ("[get_pins -of_objects [get_cells -hierarchical -filter "
               "{REF_NAME == OSERDESE2 || REF_NAME == ISERDESE2}] -filter {REF_PIN_NAME == RST}]")
        return [f"set_max_delay {ns:.3f} -from {src} -to {dst}"]
