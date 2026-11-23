# misc/dma_debug: LitePCIe DMA reader stall (phase 1)

**Solved (2026-11-23):** LitePCIe's S7PCIEPHY takes the 128-bit RX byte enables from `tkeep`; with LitePCIe 2026.08
every 256 B completion then arrives as 17 beats and the DMA reader's `pending_words` underflows. Fixed by
`boards/dupcb/s7pciephy_rx_be.py` (used by `target.py`). DMA loopback 13.43 Gb/s, 0 errors.

Debug case for the DMA loopback that stalls on this host (details and results in
[docs/notes1/gemm-hls-progress.md](../../docs/notes1/gemm-hls-progress.md), phase 1). Kept for later reuse:
the probe SoC and the tools work for any LitePCIe DMA problem on this card.

| File | What |
|---|---|
| `dma_probe.py` | `DMAProbe`: 32-bit counters around the LitePCIe 2026.08 DMA reader and TLP controller (completions from the host by size/error, read requests, reader port/FIFO beats, descriptors issued/retired), a shadow of the reader's `pending_words` with min/max, live state and a snapshot taken when the reader is disabled, and the first error completion. Cleared by `dma_probe_clear`, read after a test |
| `dma_probe_soc.py` | the base SoC (`boards/dupcb/target.py`, unchanged) + `DMAProbe`. Builds into `build/dma_probe/` (own `csr.csv` and driver: CSR addresses differ from the base build). `--stock-phy` swaps LitePCIe's `S7PCIEPHY` back in (reproduces the stall) and builds into `build/dma_probe_stock/` |
| `test_rx_be.py` | simulation check of the RX byte-enable fix (`boards/dupcb/s7pciephy_rx_be.py`) |
| `csr_peek.py` | read/write CSRs by name from a build's `csr.csv`, through the driver (ioctl) or BAR0 (mmap, no driver) |
| `run_probe_test.py` | one `dma_test` with probes cleared before and read after; report + JSON log in `logs/` (git-ignored) |
| `set_mrrs.sh` | set/show the card's PCIe Max Read Request Size (root; lasts until the next rescan) |

## Use

```
source scripts/env.sh
python3 misc/dma_debug/dma_probe_soc.py --build                    # ~5 min, build/dma_probe/
python3 misc/dma_debug/dma_probe_soc.py --load                     # JTAG (scripts/load.sh is for build/dupcb)
scripts/pcie_rescan.sh
BUILD_DIR=build/dma_probe scripts/driver.sh build
BUILD_DIR=build/dma_probe scripts/driver.sh load
misc/dma_debug/run_probe_test.py --build-dir build/dma_probe --seconds 2
BUILD_DIR=build/dma_probe scripts/driver.sh unload
```

MRRS experiment (works on any build, the probe counters are optional):
```
sudo misc/dma_debug/set_mrrs.sh 256          # after the driver is loaded (a rescan resets it)
misc/dma_debug/run_probe_test.py --build-dir build/dupcb
```

## Safety

The first DMA failure was followed by a host hard lock (before the IOMMU was on). Run DMA tests only with the IOMMU
on (`scripts/iommu/`), one short test at a time, no CSR polling during the test, and unload the driver after.
