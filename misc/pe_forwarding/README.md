# Tile rules the upstream kernel does not check

**Case (2026-11-24):** `int4_wide`, int4 PE 48×64, tile 768×2048, 200 MHz. It met timing (WNS 0.000). On the card
the first exact check at 1024³ gave 983 555 of 1 048 576 entries wrong. After that every kernel run timed out (60 s),
so the DDR tuning sweep behind it would have spent ~5 h on timeouts. The campaign was stopped.

**Cause:** two assumptions in `kernels/gemm/src/Compute.cpp` (unchanged from spcl/gemm_hls) that neither the upstream
CMake nor our flow checked:

1. **A forwarding: tile_m/PM ≥ PN.** During each outer product, a PE reads the next product's A values, keeps
   its own and forwards the rest. It does this in the slots `m1 = locationN … PN−1` of the `m1` loop, which is only
   `kInnerTilesM = tile_m/PM` long. If that is shorter than PN, the PEs at the end of the chain get no new A values.
   The feeder has written PN values per row anyway, so the A stream falls out of step: C is wrong, and the values left
   over keep the feeder blocked. On the card that is a hung kernel. PE 48×64 with tile_m 2048: 2048/64 = 32 < 48.
   PE 64×64 with tile 1024×2048 (32 < 64) has the same fault. It failed timing, so it never ran.
2. **Powers of two: tile_m/PM and tile_n/PN.** The C write-back (`WriteC_Flattened`) counts `m1` and `n1` in
   `ap_uint<hlslib::ConstLog2(x)>`, and `ConstLog2` rounds down. The counters only reach x − 1 when x is a power of
   two; otherwise C is wrong from column (or row) 2^⌊log2 x⌋ of the tile on.

A non-power-of-two PN works when both rules hold (PN 12 below). Every build that ran on the card before this case
met both rules.

**Evidence:** `sim.py` builds the kernel with the upstream testbench (g++, exact int32 compare, ~2 s per case)
for small arrays with the same ratios:

| Case | tile_n/PN, tile_m/PM | Result |
|---|---|---|
| PN 8, PM 8, tile 32×32 (also with transposed A) | 4, 4 < PN | wrong; 2032 A values left in `aPipes[0]` |
| PN 8, PM 8, tile 32×64 (also with transposed A) | 4, 8 = PN | exact |
| PN 12, PM 8, tile 48×64 | 4, 8 < PN | wrong; 2032 A values left |
| PN 12, PM 8, tile 48×128 | 4, 16 | exact |
| PN 12, PM 8, tile 48×96 | 4, 12 (not 2^k) | wrong from column 64 |
| PN 8, PM 8, tile 32×96 | 4, 12 (not 2^k) | wrong from column 64 |
| PN 8, PM 8, tile 96×128 | 12 (not 2^k), 16 | wrong from row 64 |
| PN 8, PM 8, tile 64×128 | 8, 16 | exact |

**Fix:** both rules are errors in `flow/gemmflow/config.py` (`validate`), with unit tests. No kernel change.
`int4_pe64` uses tile 512×4096 for PE 64×64 (tile_n/PN = 8, tile_m/PM = 64): the same per-PE C buffer as 1024×2048.

```
.venv/bin/python misc/pe_forwarding/sim.py      # outputs in misc/pe_forwarding/logs/ (git-ignored)
```
