#
# ddr_tune.py - DDR3 read taps tuned under GEMM load (docs/notes2/int-dtype-progress.md, misc/int32_verify).
#
# boards/dupcb/ddr_init.py calibrates each byte lane's read IDELAY tap on an idle chip (single bursts; CK phase from
# BIST). The WL7DDRPHY samples DQ with the internal sys4x clock and has one tap per byte lane. With the GEMM array
# busy, a lane's window shrinks to 3-8 taps and moves by 1-2 taps, so the late bits of a lane (CH0 DQ5 on odd beats on
# this card) flip: wrong C entries in some builds although the kernel is correct.
#
# tune() keeps the bring-up's calibration (bitslips, per-bit offsets, CK phase) and, lane by lane, sweeps the read tap
# around its value while the build's own kernel runs an exact check (gemm_runner, full verification, full-range
# data; float32 with --float-exact). The lane goes to the centre of its longest zero-error run; with no clean run it
# moves only if that halves the errors (else they come from other lanes). Two passes. Host-only: no rebuild.
#

import importlib.util
import os
import re
import subprocess

from .config import ROOT

LANE_RE = re.compile(r"lane (\d): write bitslip (\d+), read bitslip (\d+), delay\s+(\d+) \(window \d+/32 taps\)"
                     r"(?:, bit offsets \[([-\d, ]+)\])?")


def _ddr_init():
    spec = importlib.util.spec_from_file_location("ddr_init", os.path.join(ROOT, "boards", "dupcb", "ddr_init.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def calibrated(build_dir, ch):
    """{lane: [wbitslip, rbitslip, delay, offsets]} from the build's last ddr_init.log."""
    with open(os.path.join(build_dir, "logs", "ddr_init.log")) as f:
        sec = f.read().split(f"DDR3 CH{ch}: calibration at")[-1].split("check,")[0]
    out = {}
    for m in LANE_RE.finditer(sec):
        offs = [int(x) for x in m.group(5).split(",")] if m.group(5) else [0] * 8
        out[int(m.group(1))] = [int(m.group(2)), int(m.group(3)), int(m.group(4)), offs]
    return out


def count_errors(runner, size, seed=1):
    p = subprocess.run(runner + ["--n", str(size), "--k", str(size), "--m", str(size), "--verify", "full",
                                 "--seed", str(seed)], capture_output=True, text=True, timeout=600)
    m = re.search(r"\((\d+) checked, (\d+) bad", p.stdout)
    return int(m.group(2)) if m else 10**9


def best_tap(results):
    """(centre of the longest zero-error run, its length), or (fewest-error tap, 0) if no tap is clean."""
    best, run = [], []
    for t in sorted(results) + [None]:
        if t is not None and results[t] == 0:
            run.append(t)
            continue
        if len(run) > len(best):
            best = run
        run = []
    if best:
        return best[len(best) // 2], len(best)
    return min(results, key=lambda t: results[t]), 0


def tune(build_dir, runner, channels=(0, 1), span=4, passes=2, size=1024, log=print):
    """Tune the read taps of `channels` under load; `runner` is the gemm_runner base command (gemmflow.runner).
    Returns {"start_bad", "end_bad", "taps": {ch: {lane: tap}}, "windows": {ch: {lane: clean taps}}}."""
    ddr_init = _ddr_init()
    bus = ddr_init.Driver("/dev/litepcie0", os.path.join(build_dir, "csr.csv"))
    bus.open()
    try:
        chans = {c: ddr_init.Channel(bus, c, os.path.join(build_dir, "sdram_init.py")) for c in channels}
        cal = {c: calibrated(build_dir, c) for c in chans}
        out = {"start_bad": count_errors(runner, size), "taps": {}, "windows": {}}
        log(f"load tune: {out['start_bad']} bad before")
        for p in range(passes):
            for c, chan in chans.items():
                for lane, (wbs, rbs, d0, offs) in sorted(cal[c].items()):
                    res = {}
                    for t in range(max(0, d0 - span), min(31, d0 + span) + 1):
                        chan.set_lane(lane, wbs, rbs, t, offs)
                        res[t] = count_errors(runner, size)
                    t, width = best_tap(res)
                    if not width and not res[t] < 0.5 * res.get(d0, 10**9):
                        t = d0
                    chan.set_lane(lane, wbs, rbs, t, offs)
                    cal[c][lane][2] = t
                    out["windows"].setdefault(c, {})[lane] = width
                    if t != d0 or p == passes - 1:
                        log(f"load tune pass {p} CH{c} lane {lane}: tap {d0} -> {t} (clean {width} taps)")
        out["end_bad"] = count_errors(runner, size)
        out["taps"] = {c: {lane: v[2] for lane, v in sorted(cal[c].items())} for c in chans}
        log(f"load tune: {out['end_bad']} bad after; taps " +
            "; ".join(f"CH{c} " + " ".join(f"{lane}={t}" for lane, t in taps.items()) for c, taps in out["taps"].items()))
        return out
    finally:
        bus.close()
