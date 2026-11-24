# int32_verify — wrong results under load: DDR3 read-capture margin (root cause found 2026-11-24 08:10)

**Root cause:** single-bit read errors on a few DDR3 data lines when the FPGA core is busy, not a kernel problem.
- CH0 byte lane 0, DQ4–6 (mostly DQ5). CH1 lanes 0/1/3, DQ5–7.
- Almost always on the odd beats of the burst.

The WL7DDRPHY captures DQ in ISERDES with the internal `sys4x_w` clock, not DQS; odd beats are the `CLKB`
samples. It has one IDELAY tap per byte lane, which `ddr_init.py` puts at the centre of the lane's window, found
with single bursts on an idle chip. Under GEMM load that window shrinks from 11 taps to about 3, with the
calibrated tap a single tap from failure, so late bits flip.

Evidence:
- `solve_delta.py`: every solved corrupted A element is a 1-bit flip at word bit ≡ 5 mod 64 (DQ5) on beats 3/5/7.
- `lane_sweep.sh`: moving only CH0 lane 0's tap changes the error rate from ~17 k to 524 k of 1 M.
- `ddr_dma_test` streaming at the same taps (idle core) is clean, and so is B = I with the same A traffic but
  little compute.
- float, int32 and int8 failures all map to the same DQ/beat pattern.

The sections below record how it was found; the timing-closure hypotheses were wrong.

## Host-only fix (08:10–08:30): tune the taps under load
`load_tune.py` (now `gemmflow.ddr_tune`, used by `gemm_run.py --ddr-load-tune` and the campaign key
`"ddr_load_tune"`): per byte lane, sweep the read tap ±4 around the calibrated value while the build's kernel runs an
exact 1024³ check, take the centre of the longest zero-error run, two passes. `tune_and_screen.sh` = screen, tune,
screen. All four failing builds become exact (int32 200 and 180 MHz, int8 PE 16×64, float32 `a88777ec`: 0 bad over
2×2048³ full each). Logs: `logs/tune_*.log`. Under load, windows are 3–8 taps (CH0) and 2–7 (CH1), and the idle centre
of CH0 lanes 0–2 is 1–2 taps too high.

## Root-cause evidence (07:50–08:10)

**Which bits flip.** `gemm_runner --dump-inputs` writes A and B; `solve_delta.py` finds, for each wrong C row-tile,
the single A element and δ with diff_j ≡ δ·B[k][j] (mod 2³²). int32 build `c0ddddde`, 1024³: 42 row-tiles solved,
all **single-bit flips**. The bits: lane 10 bit 5 ×20 and bit 6 ×5, lane 14 bit 5 ×8 and bit 6 ×2, lane 6 bit 5 ×4
(plus bits 4/6), lane 4 bit 5 ×1. The seen value never equals another A element, so this is not a stale or
neighbouring value. As word bits that is (beat, DQ) = (5,5) ×20, (7,5) ×8, (5,6) ×5, (3,5) ×4, … The 131 unsolved
row-tiles (lane 12, i.e. beat 6) carry more than one flipped element. The PHY maps word bit = 64·beat + DQ
(`wl7ddrphy.py`: beat n of DQ i → `rddata[n%2*64 + i]` of phase n//2).

- int8 PE 16×64 (B on CH1): B bytes 24/25/27/40/41/43/56/57/59, bits 5–7 → DQ 5–7, 13–15, 29–31 on beats 3/5/7.
- float32 `a88777ec` (A on CH0, lane 12 = beat 6): mantissa flips of ~2⁵⁻⁶ LSB (4·10⁻⁶ relative).

**Moving one byte lane's read tap** (`lane_sweep.sh` + `lane_delay.py`, which calls `ddr_init.Channel.set_lane`
without re-initialising the DRAM; same bitstream, data and seed; int32 1024³ full, bad of 1 048 576):

| CH0 lane 0 tap | 6–11 | 12 | 13 | 14 | 15 | **16 (calibrated)** | 17–20 |
|---|---|---|---|---|---|---|---|
| bad | ~524 k (all byte-lane-0 rows) | 125 k | 18 k | 17 k | 20 k | **97 k** | ~524 k |

Calibration at idle reported an 11-tap window (taps 11–21). Under GEMM load it is ~3 taps (13–15), and even there
some bit fails: one shared tap can't capture every bit of the lane.

**Load, not traffic:**
- `ddr_dma_test` (256 MB random, read twice, ~1.5 GB/s, idle core) is clean on CH0 at taps 13, 16, 17 and 18,
  where the kernel fails badly at 17–18.
- B = I keeps exactly the same A traffic on CH0 but makes the array compute almost nothing (and CH1 carry
  zeros/ones), and it is clean.

So the switching activity of the core (and/or CH1's data lines) shifts CH0's capture timing.

**Why some builds pass:** the coupling depends on what the build's core does and where it sits. Placement, preset,
format and array change it; global WNS doesn't predict it (`pe_slack.tcl`: the `aBuffer` path slack per PE does
not track the failing PEs either). Passing builds (most of them, incl. the headline float and int8 builds) have
the same marginal PHY, just less coupling.

## Fix options (SoC/board changes, for the user to decide: they touch `boards/dupcb`, so every build rebuilds)
1. **Per-bit read delay**: gate the IDELAYE2 `CE`/`LD` with `dly_sel_bits` as the bitslips already are, and
   calibrate every DQ bit at the centre of its own window (per-bit deskew).
2. **Calibrate under load**: choose the lane taps (or CK phase) with the GEMM array running. This works from the
   host with the existing PHY and is now in the flow as an opt-in step (`gemmflow.ddr_tune`, above).
3. Capture with DQS (a K7DDRPHY-style read path) or lower the DDR rate for margin.
4. Keep exact verification on (done: integers always, float via `--float-exact`).


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

**Diagnosis at the time (superseded by the root cause at the top):** a silicon timing failure in this placement under high switching activity (dynamic supply
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

**Conclusion at the time (superseded):** two builds with little slack (0.028 and 0.007 ns) give wrong results only under heavy
switching activity. Builds with more slack or fewer MACs pass. The bad bits are where the routed paths are
longest, and the global activity decides whether they fail. STA "met" by a few tens of ps is not a guarantee
on this card. The DSE's exact full-range verification is what catches it (`int32_margin` tests the remedy).

## Float builds, bit-exact (2026-11-24 07:00–07:23)
`gemm_runner --float-exact` compares float32 bit for bit with an fp32 reference accumulated in the kernel's order
(`c = c + a·b`, k = 0…K−1). The headline build (`…225mhz_tA_aggressive-db5adf1c`) matches exactly with uniform(−1, 1)
data, so the model is right. `screen_all.sh` over all float builds (2×2048³ full each): 17 of 18 have 0 bad.
`a88777ec` (PE 32×8, 200 MHz, default preset) has rare errors, all in **PE 12** (rows ≡ 12 mod 32): ~512 entries
per 1024³ run at ~4·10⁻⁶ relative, which the old tolerance check passed. PE 12 is also the worst PE of the int32
build at the same array. Its `aBuffer` is at different BRAM sites in the two builds (int32 RAMB18_X5Y56, float
RAMB18_X8Y76; `abuf_sites.tcl`), so the shared factor is the 32-bit A distribution (16 lanes per word, PE p ←
lane p mod 16), not a weak site.

Screened exact with full-range data (`screen.sh`): int8 PE 32×8, int16 PE 32×8, fixed16.8 PE 32×8, int8 PE 32×32.

## Earlier suspect (ruled out)
At first the lane pattern pointed at A's read path. The DRAM ports cross from the controller clock (`sys`) to
`accel` through LiteDRAM's native-port CDC FIFOs, and the SoC declares those clocks asynchronous
(`set_clock_groups -asynchronous`), so Vivado does not time the crossing. Two results rule this out:
- The FIFO is block RAM with a safe read schedule.
- The B = I test shows A arriving intact.

The row pattern is the PE index instead (row i is computed by PE i mod 32). PEs 12 and 28 fail in every tile, while
6, 10, 14, 22, 26 and 30 fail sometimes. `cdc_delays.tcl` and `names.tcl` are the checkpoint probes from that step.
