# kernels/gemm_mixed in g++ simulation

**Purpose:** check the mixed-type kernel (`kernels/gemm_mixed/`, DenseShift and multiply MACs, separate A/B/C types)
without HLS or the card. `sim.py` renders a `Config.h` per case through the flow (`BuildCfg`), compiles the kernel
with its testbench (`tb/test_gemm.cpp`) and runs it. The testbench draws codes over the full range of A and B (every
DenseShift code) and compares every entry exactly with an int64 reference wrapped once to the C width. Float
activations are compared bit for bit with float sums in the kernel's k order.

```
.venv/bin/python misc/gemm_mixed_sim/sim.py [name-filter]      # seconds to a minute per case, parallel
```

Logs in `misc/gemm_mixed_sim/logs/` (git-ignored). Mixed configs need `-DAP_INT_MAX_W=4096` (an int32 C pack at
PM 64 is 2048 bits); `sim.py`, the flow's HLS step and `SimStep` all pass it.

**Cases (16, each at a whole-tile and a partial-tile size, all 32 exact on 2026-11-25):**

- int8 × dshift3 → int32 with each C conversion: PM 32 split 2, PM 16 pass, PM 8 gather 2, PM 64 split 4.
- int8 × dshift2 and dshift4 → int32; uint8 × dshift3 → int32; int4 × dshift3 → int16; int16 × dshift4 → int32.
- int8 × int8 → int32 with the multiply MAC.
- int8 × dshift3 → int16 at PM 64, with wrap-around.
- Non-transposed A.
- float32 × dshift2/3/4 → float32 (exponent add, then a float add): PM 16 pass (3- and 4-bit), PM 32 split 2,
  PM 8 gather 2.

**Found with it:**

- **Split 4 failed at first:** `ap_uint<2048>` is above the default `AP_INT_MAX_W` of 1024. Hence the flag above,
  and `Config.h.in` stops with an error without it.
- **Every float entry was wrong at first:** the testbench decoded B only under `MM_MAC_DENSESHIFT`. The MAC itself
  was right.
