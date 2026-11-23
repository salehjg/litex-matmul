#!/usr/bin/env python3
#
# csr_peek.py - read/write LiteX CSRs of the card from the host, by name, using a build's csr.csv.
#
# Two ways in:
#   - through the LitePCIe driver (/dev/litepcieN, LITEPCIE_IOCTL_REG) when the driver is loaded: the kernel refuses
#     a userspace mmap of a BAR that a driver owns;
#   - through the BAR0 sysfs file (resource0, mmap) when no driver is bound (needs the card enabled and the file
#     readable, see scripts/setup_permissions.sh).
#
#   misc/dma_debug/csr_peek.py --csv build/dupcb/csr.csv 'pcie_dma0_*' pcie_msi_enable
#   misc/dma_debug/csr_peek.py --csv build/dupcb/csr.csv --bar /sys/bus/pci/devices/0000:82:00.0/resource0 ctrl_scratch
#   misc/dma_debug/csr_peek.py --csv build/dma_probe/csr.csv --write dma_probe_clear=1
#
# Also used as a module by run_probe_test.py (CSRMap, DriverBus, BarBus).
#

import argparse
import csv
import fcntl
import fnmatch
import mmap
import os
import struct
import sys

CSR_BASE = 0xf0000000  # LiteX CSR origin with a CPU; without a CPU it is 0. CSRMap reads the real one from csr.csv.


class CSRMap:
    """Registers of one build, from its csr.csv: name -> (address, size in 32-bit words, mode)."""
    def __init__(self, csv_path):
        self.registers = {}
        self.csr_base  = CSR_BASE
        with open(csv_path) as f:
            for row in csv.reader(f):
                if row and row[0] == "csr_register":
                    self.registers[row[1]] = (int(row[2], 16), int(row[3]), row[4])
                elif row and row[0] == "memory_region" and row[1] == "csr":
                    self.csr_base = int(row[2], 16)  # BAR0 maps the CSR region from offset 0.

    def match(self, patterns):
        names = []
        for p in patterns:
            hits = [n for n in self.registers if fnmatch.fnmatchcase(n, p)]
            if not hits:
                raise KeyError(f"no CSR matches {p!r}")
            names += [n for n in sorted(hits, key=lambda n: self.registers[n][0]) if n not in names]
        return names

    def read(self, bus, name):
        addr, size, _ = self.registers[name]
        value = 0
        for i in range(size):  # Multi-word CSRs: 32-bit words, MSB first.
            value = (value << 32) | bus.read32(addr + 4*i)
        return value

    def write(self, bus, name, value):
        addr, size, _ = self.registers[name]
        for i in range(size):
            bus.write32(addr + 4*i, (value >> (32*(size - 1 - i))) & 0xffffffff)


class Bus:
    """32-bit CSR access by absolute CSR address."""
    def read32(self, addr):
        raise NotImplementedError

    def write32(self, addr, value):
        raise NotImplementedError


class DriverBus(Bus):
    """Through the LitePCIe driver: ioctl(LITEPCIE_IOCTL_REG). The driver subtracts CSR_BASE itself."""
    IOCTL_REG = (3 << 30) | (12 << 16) | (ord("S") << 8) | 0  # _IOWR('S', 0, struct litepcie_ioctl_reg)
    FMT       = "<IIB3x"                                     # {uint32 addr; uint32 val; uint8 is_write;}

    def __init__(self, dev="/dev/litepcie0"):
        self.fd = os.open(dev, os.O_RDWR)

    def _io(self, addr, value, is_write):
        buf = bytearray(struct.pack(self.FMT, addr, value, is_write))
        fcntl.ioctl(self.fd, self.IOCTL_REG, buf)
        return struct.unpack(self.FMT, buf)[1]

    def read32(self, addr):
        return self._io(addr, 0, 0)

    def write32(self, addr, value):
        self._io(addr, value, 1)


class BarBus(Bus):
    """Through the BAR0 sysfs file (no driver bound). csr_base: CSRMap.csr_base of the loaded build."""
    def __init__(self, bar, csr_base=CSR_BASE):
        self.base = csr_base
        self.fd   = os.open(bar, os.O_RDWR | os.O_SYNC)
        self.map  = mmap.mmap(self.fd, os.fstat(self.fd).st_size)

    def read32(self, addr):
        return struct.unpack_from("<I", self.map, addr - self.base)[0]

    def write32(self, addr, value):
        struct.pack_into("<I", self.map, addr - self.base, value)


def open_bus(dev=None, bar=None, csr_base=CSR_BASE):
    return BarBus(bar, csr_base) if bar else DriverBus(dev or "/dev/litepcie0")


def main():
    parser = argparse.ArgumentParser(description="Read/write LiteX CSRs of the card by name.")
    parser.add_argument("--csv", required=True, help="csr.csv of the build that is loaded on the card.")
    parser.add_argument("--dev", default="/dev/litepcie0", help="LitePCIe device (driver loaded).")
    parser.add_argument("--bar", help="BAR0 resource file instead of the driver (no driver bound).")
    parser.add_argument("--write", action="append", default=[], metavar="NAME=VALUE", help="Write a CSR (before reads).")
    parser.add_argument("names", nargs="*", help="CSR names or shell patterns (e.g. 'pcie_dma0_*').")
    args = parser.parse_args()

    csrs = CSRMap(args.csv)
    bus  = open_bus(args.dev, args.bar, csrs.csr_base)
    for w in args.write:
        name, value = w.split("=", 1)
        csrs.write(bus, name, int(value, 0))
        print(f"wrote {name} = {int(value, 0):#x}")
    if args.names:
        names = csrs.match(args.names)
        width = max(len(n) for n in names)
        for n in names:
            v = csrs.read(bus, n)
            print(f"{n:<{width}}  {v:#010x}  {v}")


if __name__ == "__main__":
    sys.exit(main())
