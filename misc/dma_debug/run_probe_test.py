#!/usr/bin/env python3
#
# run_probe_test.py - one LitePCIe DMA loopback test (litepcie_util dma_test), with the DMA probe counters cleared
# before and read *after* the test (no CSR polling while DMA runs). Prints a report and saves a JSON log to
# misc/dma_debug/logs/ (git-ignored). Works on builds without the probe too (then only PCIe/DMA status is read).
#
#   BUILD_DIR=build/dma_probe scripts/driver.sh build && BUILD_DIR=build/dma_probe scripts/driver.sh load
#   misc/dma_debug/run_probe_test.py --build-dir build/dma_probe --seconds 2 [--tag mrrs256]
#   BUILD_DIR=build/dma_probe scripts/driver.sh unload
#

import argparse
import datetime
import json
import os
import platform
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

from csr_peek import CSRMap, DriverBus  # noqa: E402

LINK_CSRS = [
    "pcie_endpoint_phy_link_status", "pcie_endpoint_phy_max_request_size", "pcie_endpoint_phy_max_payload_size",
    "pcie_endpoint_phy_bus_master_enable", "pcie_endpoint_phy_msi_enable",
]
DMA_CSRS = [
    "pcie_dma0_reader_enable", "pcie_dma0_reader_table_level", "pcie_dma0_reader_table_loop_status",
    "pcie_dma0_writer_enable", "pcie_dma0_writer_table_level", "pcie_dma0_writer_table_loop_status",
    "pcie_dma0_buffering_reader_fifo_status", "pcie_dma0_buffering_writer_fifo_status",
    "pcie_msi_enable", "pcie_msi_vector",
]
SIGNED_CSRS = {"dma_probe_pending_live", "dma_probe_pending_min", "dma_probe_pending_max", "dma_probe_snap_pending"}
FLAG_BITS = ["reader_enable", "reader_idle", "splitter_valid", "table_valid", "port_req_valid", "port_req_ready",
             "port_cmp_valid", "port_cmp_ready", "metadata_sink_ready", "metadata_source_valid",
             "data_fifo_sink_ready", "tag_available", "req_to_host_valid", "req_to_host_ready"]


def signed32(v):
    return v - (1 << 32) if v & (1 << 31) else v


def irq_count():
    with open("/proc/interrupts") as f:
        for line in f:
            if "litepcie" in line:
                return sum(int(x) for x in line.split()[1:] if x.isdigit())
    return None


def kernel_log_since(t0):
    try:
        out = subprocess.run(["journalctl", "-k", "--no-pager", "-o", "short-precise", "--since", f"@{int(t0)}"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [l for l in out.splitlines() if re.search(r"DMAR|AER|Bus Error|litepcie|iommu", l, re.I)]


def decode_levels(v):
    return {"data_fifo": v & 0xfff, "request_metadata": (v >> 12) & 0xf, "tag_queue": (v >> 16) & 0xf,
            "req_queue": (v >> 20) & 0xf}


def decode_flags(v):
    return {name: (v >> i) & 1 for i, name in enumerate(FLAG_BITS)}


def soc_identifier(build_dir):
    try:
        with open(os.path.join(build_dir, "driver", "kernel", "soc.h")) as f:
            m = re.search(r'CONFIG_IDENTIFIER "([^"]*)"', f.read())
            return m.group(1) if m else None
    except OSError:
        return None


def git_describe():
    try:
        sha   = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", ROOT, "status", "--porcelain"], capture_output=True, text=True).stdout.strip() != ""
        return sha + ("-dirty" if dirty else "")
    except OSError:
        return None


def derive(r):
    """Cross-checks between counters. Each one should hold for a correct reader."""
    g = lambda k: r.get("dma_probe_" + k)
    d = {}
    if g("cmp_tlps") is None:
        return d
    d["bytes_per_read_request"]      = g("cmp_bytes") / g("ctl_rd_reqs") if g("ctl_rd_reqs") else None
    d["beats_per_completion"]        = g("cmp_beats") / g("cmp_tlps") if g("cmp_tlps") else None
    d["requests_not_ended"]          = g("ctl_rd_reqs") - g("cmp_ends") - g("cmp_errs")        # in flight or lost
    d["queued_minus_dequeued_words"] = g("rd_split_words") - g("rd_fifo_out")                # pending_words if one run
    d["cmp_beats_lost_before_fifo"]  = g("rd_port_cmp_beats") - g("rd_fifo_in")
    d["ctl_vs_host_cmp_tlps"]        = g("ctl_cmp_tlps") - g("cmp_tlps")
    d["expected_beats_for_requests"] = g("rd_split_words")
    d["levels_live"]  = decode_levels(g("levels_live"))
    d["flags_live"]   = decode_flags(g("flags_live"))
    d["levels_snap"]  = decode_levels(g("snap_levels"))
    d["flags_snap"]   = decode_flags(g("snap_flags"))
    return d


def main():
    parser = argparse.ArgumentParser(description="One DMA loopback test with probe counters read afterwards.")
    parser.add_argument("--build-dir", default=os.path.join(ROOT, "build", "dma_probe"), help="Build loaded on the card.")
    parser.add_argument("--dev",      default="/dev/litepcie0")
    parser.add_argument("--seconds",  default=2, type=int, help="dma_test duration.")
    parser.add_argument("--tag",      default=None, help="Label for the log file (default: mrrs<size>).")
    parser.add_argument("--no-save",  action="store_true")
    args = parser.parse_args()

    build_dir = os.path.abspath(args.build_dir)
    csrs = CSRMap(os.path.join(build_dir, "csr.csv"))
    bus  = DriverBus(args.dev)
    util = os.path.join(build_dir, "driver", "user", "litepcie_util")
    has_probe = "dma_probe_clear" in csrs.registers

    link = {n: csrs.read(bus, n) for n in LINK_CSRS if n in csrs.registers}
    tag  = args.tag or f"mrrs{link.get('pcie_endpoint_phy_max_request_size', 'x')}"

    # Test: clear probes, run dma_test, then read everything.
    if has_probe:
        csrs.write(bus, "dma_probe_clear", 1)
    irq0, t0 = irq_count(), time.time()
    proc = subprocess.run([util, "-w", "32", "-t", str(args.seconds), "dma_test"],
                          capture_output=True, text=True, timeout=args.seconds + 30)
    t1, irq1 = time.time(), irq_count()
    stdout = re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout)
    rows = [l.split() for l in stdout.splitlines() if re.match(r"^\s*[0-9.]+\s+\d+\s+\d+\s+\d+\s+\d+\s*$", l)]
    last = rows[-1] if rows else None

    names = [n for n in DMA_CSRS if n in csrs.registers] + (csrs.match(["dma_probe_*"]) if has_probe else [])
    results = {}
    for n in names:
        v = csrs.read(bus, n)
        results[n] = signed32(v) if n in SIGNED_CSRS else v
    log = {
        "timestamp":   datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "tag":         tag,
        "build_dir":   os.path.relpath(build_dir, ROOT),
        "identifier":  soc_identifier(build_dir),
        "git":         git_describe(),
        "kernel":      platform.release(),
        "cmdline":     open("/proc/cmdline").read().strip(),
        "seconds":     args.seconds,
        "dma_test":    {"exit": proc.returncode, "last_row": last, "stderr": proc.stderr.strip()[-500:],
                        "columns": ["gbps", "tx_buffers", "rx_buffers", "diff", "errors"]},
        "irqs":        None if irq0 is None else irq1 - irq0,
        "link":        link,
        "csrs":        results,
        "derived":     derive(results),
        "kernel_log":  kernel_log_since(t0 - 1),
        "duration_s":  round(t1 - t0, 2),
    }

    # Report.
    print(f"[{tag}] {log['identifier']}  ({log['build_dir']}, git {log['git']})")
    print(f"link: " + ", ".join(f"{k.replace('pcie_endpoint_phy_', '')}={v:#x}" for k, v in link.items()))
    print(f"dma_test: exit {proc.returncode}, last row {last} (gbps tx rx diff errors), irqs {log['irqs']}")
    width = max(len(n) for n in names)
    for n in names:
        v = results[n]
        print(f"  {n:<{width}}  {v:>12}  {v & 0xffffffff:#010x}")
    for k, v in log["derived"].items():
        print(f"  derived {k}: {v}")
    print("kernel log: " + ("\n  ".join([""] + log["kernel_log"]) if log["kernel_log"] else "nothing (no DMAR/AER)"))

    if not args.no_save:
        os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
        path = os.path.join(HERE, "logs", f"{time.strftime('%Y%m%d-%H%M%S')}_{tag}.json")
        with open(path, "w") as f:
            json.dump(log, f, indent=2)
        print(f"saved {os.path.relpath(path, ROOT)}")


if __name__ == "__main__":
    main()
