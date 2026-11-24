# int32_verify — builds that fail exact verification (int32 PE 32×8, int8 PE 16×64)

**Case:** `dtype_same_array` (2026-11-24): the int32 build `i32_pe32x8_t1024x512_200mhz_tA-c0ddddde` (timing met,
WNS 0.028 ns) fails exact verification at every size (10–19 % of sampled entries). int8, int16 and fixed16.8 at the
same array and clock verify exactly, and so does float32.

`run.sh [build_dir] [--no-program]` loads the build and runs `gemm_runner` with several `--value-range` settings,
writing the mismatches with `--dump-bad` (`logs/bad_*.txt`, lines `i j got ref`; git-ignored).

## Observations (2026-11-24, 1024³, full verification)

| Data (int32 codes) | Bad entries | Rows (i mod 16) |
|---|---|---|
| full range | 93 184 | 6, 10, 12, 14 (12 and 28 in every tile) |
| −100…100 | 56 102 | 6, 10, 12, 14 |
| −30000…30000 | 54 270 | same |
| 0…65535 | 2 048 | 12 only |
| 0…1, 0…100, −100…0, −1…0, constant −2³¹ | 0 | — |
| K = 16, full range | 9 216 | 12, 28 |

- Errors come as **whole rows of C inside one M tile** (512 entries), never as columns. A whole C row is wrong when
  the A value a PE uses for one k step is wrong for the whole M tile. (The first guess, corruption on A's read path,
  was ruled out below.)
- Only rows with `i mod 16` ∈ {6, 10, 12, 14} fail, i.e. PEs 6, 10, 12, 14, 22, 26, 28, 30 of the 32.
- The C differences for small data are multiples of 2^12…2^16. So the **upper bits of those lanes** are wrong, and
  only when they toggle: non-negative small values (upper bits always 0), small negative values (always 1) and a
  constant word never fail. Mixed signs or the full range fail.
- That looks like particular bits sampled wrong in silicon (a path that is not timed, or timed with too little margin),
  not a logic error. Every PE runs the same RTL.
- **float32 at the same array (`a88777ec`) is clean:** exact test with integer-valued floats (`--value-range` in
  float mode, 1024³, ±100, ±127, 0…127): 0 bad. The float campaign results are not affected.

## More experiments (2026-11-24 06:00–06:10)

| Experiment | Result | Rules out |
|---|---|---|
| Routed FIFO for A's read data (`storage_43`, LiteDRAM native-port CDC `sys` → `accel`) | block RAM (7 RAMB36 + 1 RAMB18), port A write `NO_CHANGE`, port B read every cycle; a word is used ≥ ~5 ns after its write | LUTRAM clock-crossing path |
| DDR CH0 BIST at bring-up (1 GB, LFSR data, read twice) | 0 bad words | DDR / PHY reads |
| `identity.sh`: B = I, so C = A as the kernel read it (1024³, full-range int32, 3 reps + another seed) | **0 bad** | A read path, transpose, distribution to PEs |
| XADC under int32 load (4096³ × 25) | VCCINT 0.954 → 0.946–0.948 V (averaged), 45.6 → 48.2 °C | overheating; rails in spec |
| Exact integer-valued float32 test on the float build at the same array | 0 bad | general corruption |

So A arrives intact, and the errors need **both** operands to toggle the upper bits of the multiply-add (small
non-negative data, or same-sign data whose products are positive, never fails). They are whole rows of one M tile.
The A value is written once per k step into each PE's `aBuffer` and reused across the whole M tile, so a failed
capture there gives exactly that. The routed timing report has those paths at the edge of the clock period. Worst
`accel` paths:
`ProcessingElement_20_U0/aBuffer_U/ram_reg` (BRAM, no output register, 1.8 ns clock-to-out) → `mul_32s_32s_32_5_1`
input `A[13]`: slack **0.056 ns**. PE carry chains (9 levels): 0.062 ns. `cPipes` FIFOs: 0.049 ns.

**Diagnosis (working):** a silicon timing failure in this placement under high switching activity (dynamic supply
droop next to the busy DSP columns, which the averaged XADC reading cannot show). STA passes with ~30–60 ps. int8
(multiplies in LUTs, WNS 0.005 ns) and the other formats at this array verify. The test is
`configs/dse/int32_margin.json`: the same int32 array with the aggressive preset at 200 MHz, and at 180 MHz. If those
verify, a slack guard band (or exact full-range verification of every build, which the DSE already does) is the
remedy, not a design change.

## Second case: int8 PE 16×64 (2026-11-24 06:45–07:00)
`int8_scale` build `i8_pe16x64_t256x1024_200mhz_tA-032f4a87` (WNS 0.007 ns, 42 DSPs: the multiplies are in LUTs)
fails at 4–6 % of sampled entries. The scripts now take any build (`runner_cmd.py` reads the runner arguments from
`config.resolved.json`; file names start with the build name):

| Test (1024³, full) | Bad |
|---|---|
| full range | 5 671 |
| −3…3 | 13 730 |
| 0…3, 0…1 | 0 |
| −3…0 | 5 098 |
| B = I (C = A), 3 runs + another seed | 0 |
| **A = I (C = B), 3 seeds** (`--a-identity`) | **0** |
| 512³ (bring-up) | 0 |

- Errors are whole **columns** of one N tile: j mod 64 ∈ {24, 25, 27, 40, 41, 43, 56, 57, 59} (B lanes; PM = 64,
  so a lane is a byte of B's 512-bit word), C off by multiples of 0x20/0x40, i.e. bits 5–7 of a B element.
- A wrong B element seen by all 16 PEs means it was wrong on B's feed path before PE 0. But with A not toggling
  (A = I) B arrives intact. At the full-range rate (~25 corrupted elements per run, a quarter of them landing on
  the diagonal) about 6 bad entries per run would be expected. So, as for int32, the failure needs both operands
  switching.

**Conclusion so far:** two builds with little slack (0.028 and 0.007 ns) give wrong results only under heavy
switching activity. Builds with more slack or fewer MACs pass. The bad bits are where the routed paths are
longest, and the global activity decides whether they fail. STA "met" by a few tens of ps is not a guarantee
on this card. The DSE's exact full-range verification is what catches it (`int32_margin` tests the remedy).

## Earlier suspect (ruled out)
At first the lane pattern pointed at A's read path. The DRAM ports cross from the controller clock (`sys`) to
`accel` through LiteDRAM's native-port CDC FIFOs, and the SoC declares those clocks asynchronous
(`set_clock_groups -asynchronous`), so Vivado does not time the crossing. Two results rule this out:
- The FIFO is block RAM with a safe read schedule.
- The B = I test shows A arriving intact.

The row pattern is the PE index instead (row i is computed by PE i mod 32). PEs 12 and 28 fail in every tile, while
6, 10, 14, 22, 26 and 30 fail sometimes. `cdc_delays.tcl` and `names.tcl` are the checkpoint probes from that step.
