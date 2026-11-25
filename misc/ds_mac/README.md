# DenseShift MAC: which form HLS makes smallest

**Case (2026-11-25):** int8 × dshift3 → int32 at PE 32×32, tile 512×1024, 200 MHz (`i8xds3c32_pe32x32.json`, HLS
stage only). Code b = sign << 2 | e, so the MAC adds ±(a << e) to a 32-bit sum. Three ways to write it in
`kernels/gemm_mixed/include/Mac.h`:

| Form | HLS LUT | HLS FF |
|---|---|---|
| A: `neg ? acc − x : acc + x` (adder, subtractor, mux) | 331.6k | 173.7k |
| **B: `acc + (neg ? −x : x)`** (negate the narrow term, one 32-bit adder) | **290.6k** | 173.7k |
| C: `acc + (x ^ neg) + neg` (carry-in) | 300.9k | 167.6k |

x = a << e with e ≤ 3 fits 12 bits (8 + 3, plus one so that uint8 A stays positive). B is in `Mac.h`; all
simulation cases stay exact ([gemm_mixed_sim](../gemm_mixed_sim/README.md)).

```
.venv/bin/python scripts/gemm_build.py misc/ds_mac/i8xds3c32_pe32x32.json --stages hls
```

**Caveat:** these are HLS estimates, which pointed the wrong way at this array. Against the multiply MAC, HLS
estimated +95k LUT for DenseShift, but Vivado used 5.9k *fewer* LUTs (161.6k against 167.5k). Whether Vivado merges
C's carry-in into one carry chain and beats B was not built (open point in the notes).
