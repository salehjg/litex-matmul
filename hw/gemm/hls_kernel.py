#
# hls_kernel.py - the HLS GEMM IP in the SoC (plan section 4.2): Instance generated from the HLS RTL port list
# (csynth.xml, so names and widths are never hand-written), CSR control from sys, the three m_axi masters into
# LiteDRAM native ports through LiteDRAMAXI2Native, and AxiPerfMonitor counters.
#
# Clocking: the kernel and its AXI side run in `cd` (default "accel"); the CSRs are in sys. Start is a pulse
# (PulseSynchronizer); arguments and soft reset are quasi-static (written before start) and cross with MultiReg over the
# existing false path; status and counters cross back with MultiReg and are read only after done, when they are stable.
#
# Host protocol (sw/gemm_host): write a/b/c (byte addresses inside the DDR channel of that bundle), size_n/k/m;
# write gemm_start; poll gemm_status.done; read gemm_cycles and the perf counters.
#

from migen import *
from migen.genlib.cdc import MultiReg, PulseSynchronizer

from litex.gen import *
from litex.soc.interconnect.axi import AXIInterface
from litex.soc.interconnect.csr import CSR, CSRStorage, CSRStatus, CSRField

from litedram.frontend.axi import LiteDRAMAXI2Native

from axi_perf import AxiPerfMonitor

ARGS    = ["a", "b", "c", "size_n", "size_k", "size_m"]
BUNDLES = {"gmem0": "a", "gmem1": "b", "gmem2": "c"}  # m_axi bundle -> matrix (kernels/gemm/src/Top.cpp)


def _axi_field(axi, tail):
    """HLS m_axi signal suffix (e.g. AWADDR, RDATA, WID) -> LiteX AXIInterface signal, or None if AXI4 has none."""
    for prefix, ch in (("AW", axi.aw), ("AR", axi.ar), ("W", axi.w), ("R", axi.r), ("B", axi.b)):
        if tail.startswith(prefix):
            field = tail[len(prefix):].lower()
            break
    else:
        raise ValueError(f"unknown m_axi signal {tail}")
    if prefix == "W" and field == "id":  # AXI3 WID, not in AXI4.
        return None
    try:
        return getattr(ch, field)
    except AttributeError:
        return None  # user signals (width 0 here)


class HlsGemmKernel(LiteXModule):
    def __init__(self, rtl_ports, dram_ports, ip_module="gemm_kernel_ip", cd="accel", build_id=0,
                 axi_w_buffer_depth=32, axi_r_buffer_depth=64):
        """rtl_ports: list of dicts (name, dir, bits) from gemmflow.hls.HlsReport.rtl_ports().
        dram_ports: {"a": port, "b": port, "c": port}, LiteDRAM native ports in clock domain `cd`."""
        # CSRs (sys) ---------------------------------------------------------------------------------------------
        self.start      = CSR()  # Write (any value): start the kernel; set the arguments first.
        self.soft_reset = CSRStorage(description="1: hold the kernel in reset.")
        self.status     = CSRStatus(fields=[
            CSRField("done", size=1, description="Kernel finished since the last start."),
            CSRField("idle", size=1, description="Kernel ap_idle."),
            CSRField("busy", size=1, description="Kernel running (start accepted, not done)."),
        ])
        self.build_id   = CSRStatus(32, description="First 32 bits of the build config hash (gemmflow).")
        self.cycles     = CSRStatus(64, description=f"{cd} cycles from start to done.")
        for arg in ARGS:
            setattr(self, arg, CSRStorage(32, name=arg, description=f"Kernel argument {arg}."))
        self.comb += self.build_id.status.eq(build_id)

        # CDC sys -> cd ------------------------------------------------------------------------------------------
        self.start_ps = PulseSynchronizer("sys", cd)
        self.comb += self.start_ps.i.eq(self.start.re)
        start = self.start_ps.o
        soft_reset = Signal()
        self.specials += MultiReg(self.soft_reset.storage, soft_reset, cd)
        args = {}
        for arg in ARGS:
            args[arg] = Signal(32, name=f"kernel_{arg}")
            self.specials += MultiReg(getattr(self, arg).storage, args[arg], cd)

        # Control (cd) --------------------------------------------------------------------------------------------
        ap_start, ap_done, ap_ready, ap_idle = Signal(), Signal(), Signal(), Signal()
        busy, done, cycles = Signal(), Signal(), Signal(64)
        sync = getattr(self.sync, cd)
        sync += [
            If(start, ap_start.eq(1)).Elif(ap_ready, ap_start.eq(0)),  # ap_ctrl_hs: hold start until ready.
            If(start, busy.eq(1), done.eq(0), cycles.eq(0)
            ).Elif(busy,
                cycles.eq(cycles + 1),
                If(ap_done, busy.eq(0), done.eq(1)),
            ),
        ]
        self.specials += [
            MultiReg(done,   self.status.fields.done, "sys"),
            MultiReg(ap_idle, self.status.fields.idle, "sys"),
            MultiReg(busy,   self.status.fields.busy, "sys"),
            MultiReg(cycles, self.cycles.status, "sys"),
        ]

        # AXI masters into LiteDRAM -----------------------------------------------------------------------------
        bundles = sorted({p["name"].split("_")[2] for p in rtl_ports if p["name"].startswith("m_axi_")})
        self.axi = {}
        for bundle in bundles:
            mat = BUNDLES[bundle]
            port = dram_ports[mat]
            axi = AXIInterface(data_width=port.data_width, address_width=32, id_width=1, clock_domain=cd)
            self.axi[bundle] = axi
            conv = LiteDRAMAXI2Native(axi, port, w_buffer_depth=axi_w_buffer_depth, r_buffer_depth=axi_r_buffer_depth)
            setattr(self, f"axi2native_{mat}", ClockDomainsRenamer(cd)(conv))
            mon = AxiPerfMonitor(axi, run=busy, clear=start)
            setattr(self, f"perf_{mat}", ClockDomainsRenamer(cd)(mon))
            for name, sig in mon.counters.items():
                csr = CSRStatus(len(sig), name=f"perf_{mat}_{name}")
                setattr(self, f"perf_{mat}_{name}", csr)
                self.specials += MultiReg(sig, csr.status, "sys")

        # Instance ----------------------------------------------------------------------------------------------
        params = {}
        for p in rtl_ports:
            name, d, bits = p["name"], p["dir"], p["bits"]
            if name.startswith("m_axi_") and name.endswith("USER"):
                continue  # In csynth.xml, but the packaged IP (component.xml) has no AXI user ports.
            key = ("i_" if d == "in" else "o_") + name
            if name == "ap_clk":
                params[key] = ClockSignal(cd)
            elif name == "ap_rst_n":
                params[key] = ~(ResetSignal(cd) | soft_reset)
            elif name == "ap_start":
                params[key] = ap_start
            elif name in ("ap_done", "ap_ready", "ap_idle"):
                params[key] = {"ap_done": ap_done, "ap_ready": ap_ready, "ap_idle": ap_idle}[name]
            elif name in args:
                params[key] = args[name]
            elif name.startswith("m_axi_"):
                bundle, tail = name.split("_")[2], name.split("_", 3)[3]
                field = _axi_field(self.axi[bundle], tail)
                sig = Signal(bits, name=f"kernel_{bundle}_{tail.lower()}")
                params[key] = sig
                if d == "in":
                    self.comb += sig.eq(field if field is not None else 0)
                elif field is not None:
                    self.comb += field.eq(sig)  # Truncates AXI3-style 2-bit LOCK to AXI4's 1 bit.
            else:
                raise ValueError(f"unexpected kernel port {name}")
        self.specials += Instance(ip_module, **params)
