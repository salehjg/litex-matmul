#!/usr/bin/env python3
#
# ddr_init.py - calibrate the DDR3 channels from the host over PCIe, then test them.
#
# Run after every bitstream load (scripts/ddr_init.sh). Talks to BAR0 directly (mmap), no server.
# Per channel (WL7DDRPHY, boards/dupcb/wl7ddrphy.py):
#   1. CK phase scan: the channel's MMCM moves CK against DQ/DQS in 1/56-VCO steps (17.9 ps).
#      At every --stride step over one tCK: DRAM init, calibration (below), then a 64 MB BIST.
#      A step passes if every lane calibrates and the BIST has no errors.
#   2. CK goes to the centre of the longest run of passing steps; calibrate again there.
#   3. Check 200 random addresses (errors per lane), then BIST: write once, read back twice.
# Calibration at one phase: for write bitslip 0/2/4/6 (whole tCK) x read bitslip x read delay,
# write/read random bursts at random addresses, all lanes at once, errors counted per DQ bit.
# Per lane keep the widest read window; a DQ bit may read 2 bitslips off its lane (its ISERDES
# frames the word one clock off) and then gets its own read bitslip.
# The single-burst check alone is not enough (it passes where streaming traffic fails), which is
# why every phase step is checked with the BIST. Method and card data: openTPU docs/litedram.md.

import argparse
import os
import random
import sys
import time

from litex.tools.remote.comm_pcie import CommPCIe

MODULES   = 8               # Byte lanes (x8 chips) used per channel.
DATABITS  = 64
NPHASES   = 4
DELAYS    = 32              # IDELAYE2 taps (78 ps each with a 200 MHz reference).
BITSLIPS  = 8
WBITSLIPS = (0, 2, 4, 6)    # Write latency in whole tCK; CK phase covers the rest.
STEPS     = 112             # CK fine-phase steps per tCK (56 per VCO period, VCO = 2 x DDR clock).
BANKS, ROWS, COLS = 8, 1 << 15, 1 << 10   # MT41K256M8 / H5TC2G83 geometry.

# BAR0 access --------------------------------------------------------------------------------------

class Bar(CommPCIe):
    """BAR0 mapped into this process. csr.csv addresses start at the CSR base; on the bus at 0."""
    def __init__(self, bar, csr_csv):
        CommPCIe.__init__(self, bar, csr_csv=csr_csv)
        self.offset = self.mems.csr.base

    def read(self, addr, length=None, burst="incr"):
        return CommPCIe.read(self, addr - self.offset, length, burst)

    def write(self, addr, data):
        CommPCIe.write(self, addr - self.offset, data)

# One channel --------------------------------------------------------------------------------------

class Channel:
    def __init__(self, bus, n, init_file):
        self.n     = n
        self.r     = bus.regs
        self.sys_clk_freq = bus.constants.config_clock_frequency
        sfx        = "" if n == 0 else str(n)
        self.dfii  = f"sdram{sfx}_dfii_"
        self.phyn  = f"ddrphy{sfx}_"
        self.clkn  = f"ddrclk{sfx}_"
        self.bistn = f"sdram{sfx}_"
        ns = {}
        with open(init_file) as f:
            src = f.read()
        exec(src, ns)
        self.c = ns  # dfii_* constants + init_sequence.
        # Entries that set the DFII control register (reset/CKE) instead of issuing a command.
        lines = [l for l in src.splitlines() if l.strip().startswith('("')]
        self.is_control = ["dfii_control" in l for l in lines]
        self.rdphase = self.reg_phy("rdphase").read()
        self.wrphase = self.reg_phy("wrphase").read()
        self.seed    = 1

    def reg(self, name):     return getattr(self.r, self.dfii + name)
    def reg_phy(self, name): return getattr(self.r, self.phyn + name)
    def reg_clk(self, name): return getattr(self.r, self.clkn + name)

    # DFII ---------------------------------------------------------------------------------------
    def command(self, phase, cmd, address=0, baddress=0):
        self.reg(f"pi{phase}_address").write(address)
        self.reg(f"pi{phase}_baddress").write(baddress)
        self.reg(f"pi{phase}_command").write(cmd)
        self.reg(f"pi{phase}_command_issue").write(1)

    def hardware_control(self):
        self.reg("control").write(self.c["dfii_control_sel"])

    def init_dram(self):
        """PHY reset (clears all delays/bitslips), then the JEDEC init sequence."""
        c = self.c
        self.reg("control").write(c["dfii_control_cke"] | c["dfii_control_odt"] | c["dfii_control_reset_n"])
        self.reg_phy("dly_sel_bits").write(0xFF)
        self.reg_phy("rst").write(1); time.sleep(0.001)
        self.reg_phy("rst").write(0); time.sleep(0.001)
        for (comment, a, ba, cmd, delay), control in zip(c["init_sequence"], self.is_control):
            if control:
                self.reg("control").write(cmd)
            else:
                self.command(0, cmd, a, ba)
            time.sleep(0.001 if delay else 0)

    def test_bits(self, count=1):
        """Write random bursts to random addresses and read them back; wrong reads per DQ bit."""
        c   = self.c
        err = [0]*DATABITS
        for _ in range(count):
            rng  = random.Random(self.seed); self.seed += 1
            bank = rng.randrange(BANKS)
            row  = rng.randrange(ROWS)
            col  = rng.randrange(COLS) & ~7          # Burst of 8 starts on an 8-column boundary.
            data = [rng.getrandbits(2*DATABITS) for _ in range(NPHASES)]
            self.command(0, c["dfii_command_ras"] | c["dfii_command_cs"], row, bank)        # Activate.
            for p in range(NPHASES):
                self.reg(f"pi{p}_wrdata").write(data[p])
            self.command(self.wrphase, c["dfii_command_cas"] | c["dfii_command_we"] |
                         c["dfii_command_cs"] | c["dfii_command_wrdata"], col, bank)        # Write.
            self.command(self.rdphase, c["dfii_command_cas"] | c["dfii_command_cs"] |
                         c["dfii_command_rddata"], col, bank)                               # Read.
            self.command(0, c["dfii_command_ras"] | c["dfii_command_we"] | c["dfii_command_cs"],
                         1 << 10)                                                           # Precharge all.
            for p in range(NPHASES):
                x = data[p] ^ self.reg(f"pi{p}_rddata").read()
                while x:
                    low = x & -x
                    err[(low.bit_length() - 1) % DATABITS] += 1
                    x ^= low
        return err

    def lane_errors(self, count):
        e = self.test_bits(count)
        return [sum(e[8*m:8*m + 8]) for m in range(MODULES)]

    # PHY delay/bitslip controls; `lanes` is a bit mask of byte lanes. -----------------------------
    def strobe(self, lanes, action, count=1):
        self.reg_phy("dly_sel").write(lanes)
        for _ in range(count):
            self.reg_phy(action).write(1)
        self.reg_phy("dly_sel").write(0)

    def set_lane(self, m, wbitslip, rbitslip, delay, bit_offsets):
        self.strobe(1 << m, "wdly_dq_bitslip_rst"); self.strobe(1 << m, "wdly_dq_bitslip", wbitslip)
        self.strobe(1 << m, "rdly_dq_bitslip_rst"); self.strobe(1 << m, "rdly_dq_bitslip", rbitslip)
        self.strobe(1 << m, "rdly_dq_rst");         self.strobe(1 << m, "rdly_dq_inc",     delay)
        for i, o in enumerate(bit_offsets):         # Bits framed one clock off their lane.
            if o:
                self.reg_phy("dly_sel_bits").write(1 << i)
                self.strobe(1 << m, "rdly_dq_bitslip_rst")
                self.strobe(1 << m, "rdly_dq_bitslip", rbitslip + o)
                self.reg_phy("dly_sel_bits").write(0xFF)

    # CK phase -------------------------------------------------------------------------------------
    def ck_steps(self):
        v = self.reg_clk("ck_steps").read()
        return v - (1 << 32) if v & (1 << 31) else v

    def ck_move(self, target):
        """Move CK to `target` steps. Kept within half a tCK of sys (the PHY's command crossing
        needs that); a target a whole tCK away is the same phase against the DRAM."""
        if not self.reg_clk("locked").read():
            raise RuntimeError(f"CH{self.n}: DDR clock MMCM not locked")
        target = (target + STEPS//2) % STEPS - STEPS//2
        t0 = time.time()
        while (s := self.ck_steps()) != target:
            while self.reg_clk("ck_busy").read():
                if time.time() - t0 > 5:
                    raise TimeoutError(f"CH{self.n}: CK phase stuck at {s}")
            self.reg_clk("ck_shift").write(1 if target > s else 0)

    # BIST -----------------------------------------------------------------------------------------
    def bist_run(self, unit, size):
        """BIST generator (write) or checker (read) over [0, size): (bad 64-byte words, seconds)."""
        u = lambda name: getattr(self.r, f"{self.bistn}{unit}_{name}")
        u("reset").write(1)
        u("base").write(0)
        u("end").write(size)
        u("length").write(size)
        u("random").write(1)  # Random data (same sequence in generator and checker), linear addresses.
        u("start").write(1)
        t0 = time.time()
        while not u("done").read():
            if time.time() - t0 > 30:
                raise TimeoutError(f"CH{self.n}: BIST {unit} did not finish")
        seconds = u("ticks").read()/self.sys_clk_freq
        return (u("errors").read() if unit == "checker" else 0), seconds

    def bist(self, size):
        self.bist_run("generator", size)
        return self.bist_run("checker", size)[0]

# Calibration at the current CK phase --------------------------------------------------------------

def windows(ok):
    """Longest run of True: (start, length)."""
    best, start = (0, 0), None
    for i, good in enumerate(list(ok) + [False]):
        if good and start is None:
            start = i
        if not good and start is not None:
            if i - start > best[1]:
                best = (start, i - start)
            start = None
    return best

def lane_best(scan, m):
    """Best read setting for lane m in scan[rbitslip][tap][bit] = errors.
    Returns (window, rbitslip, start, per-bit offsets). Every bit of a lane uses the lane's tap;
    a bit may read at the lane's bitslip +-2 (one clock off). Widest window wins; between equals,
    the one with the fewest bits off."""
    best = (0, 0, 0, [0]*8)
    for b in range(BITSLIPS):
        ok = {(i, o): [0 <= b + o < BITSLIPS and scan[b + o][t][8*m + i] == 0 for t in range(DELAYS)]
              for i in range(8) for o in (0, 2, -2)}
        start, n = windows([all(any(ok[i, o][t] for o in (0, 2, -2)) for i in range(8))
                            for t in range(DELAYS)])
        if n < best[0]:
            continue
        offs = [max((0, 2, -2), key=lambda o: sum(ok[i, o][start:start + n])) for i in range(8)]
        start, n = windows([all(ok[i, offs[i]][t] for i in range(8)) for t in range(DELAYS)])
        if (n, -sum(map(bool, offs))) > (best[0], -sum(map(bool, best[3]))):
            best = (n, b, start, offs)
    return best

def calibrate(ch, verbose=False):
    """Init the DRAM, find and set each lane's write bitslip, read bitslip, read delay (and per-bit
    offsets). Returns the read window per lane (0 = lane failed)."""
    ch.init_dram()
    all_lanes = (1 << MODULES) - 1
    best = [(0, None)]*MODULES   # Per lane: (window, (wbitslip, rbitslip, delay, offsets)).
    for wbs in WBITSLIPS:
        ch.strobe(all_lanes, "wdly_dq_bitslip_rst"); ch.strobe(all_lanes, "wdly_dq_bitslip", wbs)
        ch.strobe(all_lanes, "rdly_dq_bitslip_rst")
        scan = []
        for rbs in range(BITSLIPS):
            ch.strobe(all_lanes, "rdly_dq_rst")
            row = []
            for t in range(DELAYS):
                row.append(ch.test_bits())
                ch.strobe(all_lanes, "rdly_dq_inc")
            scan.append(row)
            ch.strobe(all_lanes, "rdly_dq_bitslip")
        for m in range(MODULES):
            n, b, start, offs = lane_best(scan, m)
            if n > best[m][0]:
                best[m] = (n, (wbs, b, start + n//2, offs))
    for m, (n, setting) in enumerate(best):
        if setting is not None:
            ch.set_lane(m, *setting)
        if verbose:
            if setting is None:
                print(f"  lane {m}: NO working setting")
            else:
                wbs, b, d, offs = setting
                print(f"  lane {m}: write bitslip {wbs}, read bitslip {b}, delay {d:2d} "
                      f"(window {n}/{DELAYS} taps)" + (f", bit offsets {offs}" if any(offs) else ""))
    return [n for n, _ in best]

# Per channel --------------------------------------------------------------------------------------

def ck_scan(ch, stride, mib):
    """Pass/fail per CK phase step over one tCK. Returns {step: passed}."""
    start, table = ch.ck_steps(), {}
    size = mib << 20
    line = ""
    for k in range(0, STEPS, stride):
        ch.ck_move(start + k)
        win = calibrate(ch)
        ok  = min(win) >= 3 and not any(ch.lane_errors(4))
        if ok:
            ch.hardware_control()
            ok = ch.bist(size) == 0
        table[k] = ok
        line += "#" if ok else "."
    print(f"  CK phase scan, {stride*1e12/(8*ch.sys_clk_freq)/56:.0f} ps per char, # = all lanes pass:")
    print(f"  |{line}|")
    ch.ck_move(start)
    return table

def pick(table):
    """Centre of the longest run of passing steps, around the circle (the scan covers one tCK)."""
    ks  = sorted(table)
    seq = ks + ks
    best, cur = [], []
    for k in seq:
        if table[k]:
            cur.append(k)
            if len(best) < len(cur) <= len(ks):
                best = list(cur)
        else:
            cur = []
    return (best[len(best)//2], len(best)) if best else (None, 0)

def run_channel(ch, args):
    name = f"DDR3 CH{ch.n}"
    print(f"{name}: CK phase scan (stride {args.stride})")
    t0    = time.time()
    table = ck_scan(ch, args.stride, args.scan_mib)
    k, n  = pick(table)
    step_ps = 1e12/(8*ch.sys_clk_freq)/56
    if k is None:
        print(f"{name}: no CK phase where every lane passes. FAIL")
        return False
    print(f"  window {n*args.stride} steps ({n*args.stride*step_ps:.0f} ps), CK at +{k} "
          f"({time.time() - t0:.0f} s)")
    ch.ck_move(ch.ck_steps() + k)
    print(f"{name}: calibration at CK step {ch.ck_steps()}")
    ok = min(calibrate(ch, verbose=True)) > 0
    bits  = [0]*MODULES
    fails = [0]*MODULES
    for _ in range(args.check):
        for m, e in enumerate(ch.lane_errors(1)):
            bits[m] += e; fails[m] += e > 0
    print(f"{name}: check, {args.check} random addresses, per lane:")
    print("    failed tests: " + " ".join(f"{f:5d}" for f in fails))
    print("    bit errors:   " + " ".join(f"{b:5d}" for b in bits))
    ok &= sum(bits) == 0
    ch.hardware_control()
    size  = args.bist_size
    words = size//64
    _, tw  = ch.bist_run("generator", size)
    e1, t1 = ch.bist_run("checker",   size)
    e2, _  = ch.bist_run("checker",   size)
    print(f"{name}: BIST {size >> 20} MB, bad 64-byte words: read 1 {e1}, read 2 {e2} of {words}"
          f" (write {size/tw/1e9:.2f} GB/s, read {size/t1/1e9:.2f} GB/s)")
    ok &= e1 == 0 and e2 == 0
    print(f"{name}: {'PASS' if ok else 'FAIL'}")
    return ok

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Calibrate and test the DDR3 channels over PCIe.")
    parser.add_argument("--bar",       required=True, help="BAR0 file: /sys/bus/pci/devices/<bdf>/resource0.")
    parser.add_argument("--channel",   default="all", choices=["all", "0", "1"], help="Channel(s) (default all).")
    parser.add_argument("--build-dir", default="build/dupcb", help="Build directory (csr.csv, sdram_init.py).")
    parser.add_argument("--stride",    default=2, type=int, help="CK phase scan step, fine steps (default 2).")
    parser.add_argument("--scan-mib",  default=64, type=int, help="BIST size per scan step, MB (default 64).")
    parser.add_argument("--check",     default=200, type=int, help="Random addresses for the final check.")
    parser.add_argument("--bist-size", default=0x4000_0000, type=lambda x: int(x, 0), help="Final BIST size, bytes (default 1 GB).")
    args = parser.parse_args()

    bus = Bar(args.bar, os.path.join(args.build_dir, "csr.csv"))
    bus.open()
    try:
        chans = [0, 1] if args.channel == "all" else [int(args.channel)]
        chans = [n for n in chans if hasattr(bus.regs, ("ddrphy_" if n == 0 else f"ddrphy{n}_") + "rst")]
        init  = os.path.join(args.build_dir, "sdram_init.py")
        results = {n: run_channel(Channel(bus, n, init), args) for n in chans}
    finally:
        bus.close()
    print("Result: " + ", ".join(f"CH{n} {'PASS' if ok else 'FAIL'}" for n, ok in results.items()))
    sys.exit(0 if all(results.values()) else 1)

if __name__ == "__main__":
    main()
