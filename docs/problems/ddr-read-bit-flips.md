# DDR3 read bit flips under GEMM load

| | |
|---|---|
| **Status** | Root cause found 2026-11-24. Host-side workaround in the flow (`--ddr-load-tune`). Permanent fix not made: it changes `boards/dupcb`, so every bitstream would rebuild (decision for the project owner). |
| **Affects** | Some builds, some bring-ups: wrong C entries although timing is met and the kernel is correct. |
| **Detected by** | Exact verification: integer formats always, float32 bit for bit (`--float-exact`). |
| **Workaround** | Re-tune the DDR read delay taps under load after every bring-up: `gemm_run.py --ddr-load-tune`, or `"ddr_load_tune": true` in a campaign. About 30 s, no rebuild. |

## In short

When the GEMM array is busy, single data bits read from DDR3 sometimes flip, mostly data line DQ5 of channel 0 on
the odd beats of a burst. The kernel then multiplies with one wrong A or B element, and every C entry that uses it
is wrong. The DDR PHY samples the data lines with an internal clock and has one delay tap per byte lane. Bring-up
calibrates that tap on an idle chip, where the valid window is about 11 taps wide. Under GEMM load the window shrinks
to about 3 taps and the calibrated tap sits at its edge. Re-tuning each lane's tap with the kernel running moves it
back into the window, and the results become exact.

## Symptoms

The first cases, 2026-11-24 (`dtype_same_array` and `int8_scale`; exact integer checks with full-range data):

| Build | Timing | Wrong entries |
|---|---|---|
| int32 PE 32×8, 200 MHz (`c0ddddde`) | met, WNS 0.028 ns | 10–19 % of sampled entries; ~97–102 k of 1 M at 1024³ |
| int32 PE 32×8, 180 MHz | met | 2–4 k of 1 M |
| int8 PE 16×64, 200 MHz (`032f4a87`) | met, WNS 0.007 ns | 4–6 % of sampled entries |
| float32 PE 32×8, 200 MHz (`a88777ec`) | met | ~512 per 1024³ run, ~4·10⁻⁶ relative (passed the old float tolerance) |

What the errors looked like:

- **Whole rows or whole columns of one tile.** int32 had whole C rows of one M tile (512 entries), only in rows
  i mod 16 ∈ {6, 10, 12, 14}. int8 PE 16×64 had whole columns of one N tile, only in columns j mod 64 ∈
  {24, 25, 27, 40, 41, 43, 56, 57, 59}. A whole row means that one A element was wrong for a whole M tile; a whole
  column means one B element was wrong for every PE.
- **Data dependent.** Data that keeps the affected bits constant never failed: constant words, values 0…1 or
  0…100, small negative values. Mixed signs or full-range data failed. (Only a bit that changes between beats can
  be sampled at the wrong moment.)
- **B = I or A = I gave exact results**, with the same A or B traffic as before but almost no computation.
- **A lower clock did not help:** the int32 build at 180 MHz still failed. Another int32 build with the
  `aggressive` preset at 200 MHz passed, as it turned out because its placement coupled less into the PHY.
- **Some builds and some bring-ups only.** Most builds, including the headline float and int8 builds, were exact.
  The same build can give different error counts after different bring-ups, because each calibration lands a little
  differently (table under "Results").

## How it was found

The symptoms point at the kernel or its timing first, and those hypotheses took most of the time. All of them were
wrong:

| Hypothesis | Ruled out by |
|---|---|
| A's clock-domain crossing FIFO (LiteDRAM port, `sys` → `accel`, not timed by Vivado) | It is block RAM with a safe read schedule; B = I shows A arriving intact as the kernel read it. |
| DDR or PHY errors in general | 1 GB BIST at bring-up clean; DMA streaming (`ddr_dma_test`, 256 MB, ~1.5 GB/s) clean. |
| Timing failure in the PEs under high switching (supply droop) | 180 MHz still failed; XADC rails in spec; the slack per PE did not track the failing PEs. |

The decisive step was to recover the corrupted inputs themselves:

1. `gemm_runner --dump-inputs` writes A and B. For each wrong C row tile, `solve_delta.py` finds the single A
   element k and the change δ with C_got − C_ref ≡ δ·B[k][j] for every j (mod 2³²).
2. In the int32 build all 42 solved cases are **single-bit flips**. The flipped bit is word bit ≡ 5 mod 64 (a few
   ≡ 4 or 6). The PHY maps word bit = 64·beat + DQ, so that is **DQ5 of channel 0 (byte lane 0), on beats 3, 5, 7**.
   The wrong value never equals another A element, so it is not a stale or neighbouring word.
3. The int8 PE 16×64 errors (B, on channel 1) map to DQ 5–7, 13–15 and 29–31 on beats 3, 5, 7. The float
   errors are mantissa flips of the same kind.

Then the read delay of that one byte lane was moved, with the same bitstream, data and seed (`lane_sweep.sh`,
int32, 1024³, wrong entries of 1 048 576):

| CH0 lane 0 read tap | 6–11 | 12 | 13 | 14 | 15 | **16 (calibrated)** | 17–20 |
|---|---|---|---|---|---|---|---|
| Wrong entries | ~524 k | 125 k | 18 k | 17 k | 20 k | **97 k** | ~524 k |

Calibration on the idle chip had found a window of 11 taps (11–21). Under GEMM load it is about 3 taps (13–15), and
not fully clean even there. Two more results show that it is the core's activity, not the memory traffic:

- DMA streaming with an idle core is clean at the same taps, even at taps 17–18 where the kernel fails badly.
- B = I keeps exactly the same A traffic on channel 0 but makes the array compute almost nothing, and it is clean.

## Root cause

- **The PHY** (`boards/dupcb/wl7ddrphy.py`, adapted from LiteDRAM's A7DDRPHY via openTPU) captures DQ in ISERDES with
  the internal `sys4x_w` clock, not with DQS. The odd beats are the `CLKB` samples. Each byte lane has **one**
  IDELAY tap for its eight DQ bits (the IDELAY `CE`/`LD` are not gated per bit), so all bits of a lane share one
  sampling point.
- **The calibration** (`boards/dupcb/ddr_init.py`, run at every bring-up) puts each lane's tap at the centre of its
  window, measured with test bursts on an idle chip.
- **Under GEMM load** the switching activity of the core shifts the capture timing. A lane's window shrinks from
  10–12 taps to 3–8 taps on channel 0 and 2–7 on channel 1. On channel 0 lanes 0–2 the idle centre is 1–2 taps too
  high, at or just past the upper edge of the loaded window. The late bits of the lane (DQ5 on the odd beats) then fall outside it.
- **Why only some builds:** how strongly a build's activity couples into the PHY depends on what the core does
  and where it is placed (array, format, placement, preset). Global WNS does not predict it. Passing builds have
  the same marginal PHY, just less coupling.

## Workaround: read taps tuned under load

`flow/gemmflow/ddr_tune.py` runs right after the normal bring-up calibration:

1. Keep the calibration's bitslips, per-bit offsets and CK phase.
2. Run the build's own kernel with an exact check: 1024³, full-range random data, **every entry** compared
   (float32 bit for bit). That gives the "bad before" count.
3. Lane by lane, sweep the read tap ±4 around its value with that check, and put the lane at the centre of its
   longest error-free run. If no tap is clean, move only when that halves the errors (else they come from other
   lanes). Two passes.
4. Repeat the check for the "bad after" count.

It is host-only, takes about 30 s and changes no bitstream. The taps live in the PHY's registers, so the tuning is
lost at the next load or driver probe and has to run at every bring-up.

How to use it:

- `gemm_run.py <build> --program --ddr-load-tune ...`, or `"ddr_load_tune": true` in a `gemm_dse.py` campaign
  (`Board.bring_up(tune_runner=...)` in `flow/gemmflow/board.py`).
- Result in `<build>/logs/ddr_tune.json`; result rows carry `ddr_load_tuned`, `ddr_tune_bad_before` and
  `ddr_tune_bad_after`.
- It is off by default. Every campaign started after 2026-11-24 08:30 uses it, and so do all DenseShift card runs.

## Results

The builds that had failed, after tuning (full exact checks of 2 × 2048³, 8.4 M entries each):

| Build | Wrong before | Wrong after | Taps moved (calibrated → tuned) |
|---|---|---|---|
| int32 PE 32×8, 200 MHz (`c0ddddde`) | 97–102 k per 1024³ | **0** | CH0 lanes 0, 1: 16 → 14 |
| int32 PE 32×8, 180 MHz | 2–4 k | **0** | CH0 lanes 0–4 down 1–2 taps |
| int8 PE 16×64 | ~31 k | **0** | CH1 lanes 0–2, 7 down 1–2 taps |
| float32 PE 32×8, 200 MHz (`a88777ec`) | ~34 k (bit-exact) | **0** | CH0 lanes 0–2 down 1–2 taps |

Throughput is unchanged (for example int32 98.59 GOP/s at 8192³).

The problem is still there on every bring-up. The DenseShift baseline session of 2026-11-25 loaded seven builds in
turn; wrong entries of 1 048 576 in the tuner's check:

| Build | Before tuning | After tuning |
|---|---|---|
| float32 PE 32×8, 200 MHz (`a88777ec`) | 4 548 | 0 |
| float32 PE 32×8, 225 MHz `aggressive` (`db5adf1c`) | 0 | 0 |
| float32 × dshift3 PE 32×16 (`b5f615c5`) | **50 223** | 0 |
| uint8 × dshift3 → int32 PE 32×32 (`72f311dc`) | 0 | 0 |
| int8 × dshift3 → int32 PE 32×32 (`555d202a`) | 0 | 0 |
| int8 × dshift3 → int16 PE 32×64 (`863a7f43`) | 0 | 0 |
| int8 × int8 → int32 PE 32×32 (`29a9315e`) | 4 087 | 0 |

## What it means for the results

- Wrong entries from this problem say nothing about the kernel or the number format: the kernel computes exactly
  what it reads. Rows marked `verify_failed` for this reason (int32 PE 32×8 at 200/180 MHz, int8 PE 16×64) have
  valid throughput.
- A build that was exact once is not safe without tuning: the error count changes between bring-ups (`a88777ec`:
  ~512 per 1024³ run when it was found, 4 548 on 2026-11-25).
- Exact verification is what catches it. Float results are compared bit for bit since this was found; a tolerance
  check had passed the `a88777ec` errors. 17 of the 18 float builds were bit-exact without tuning.
- The sampled check (4098 entries per run) catches a broken bring-up like the ones above (thousands of wrong
  entries per million). Rarer errors need the full checks: the tuner's 1024³ check, the `screen.sh` screens
  (2 × 2048³), or full verification of real data.

## Permanent fix options (not made)

Each changes `boards/dupcb`, which is part of every SoC build's hash, so every bitstream would rebuild. That is a
decision for the project owner.

1. **Per-bit read delay:** gate the IDELAYE2 `CE`/`LD` per DQ bit (as the bitslips already are) and calibrate every
   bit at the centre of its own window (per-bit deskew).
2. **Calibrate under load:** choose the lane taps (or the CK phase) in `ddr_init.py` with the GEMM array running, as
   the tuner does now from the flow.
3. **Capture with DQS** (a K7DDRPHY-style read path), or run the DDR slower for margin.
4. **Keep exact verification on** (done: integers always, float32 bit for bit).

## Side issue found on the way

Importing `boards/dupcb/ddr_init.py` as a Python module (as the tuner does) wrote a `.pyc` into
`boards/dupcb/__pycache__`. That tree is hashed into every SoC build's stamp, so every build looked stale and a DSE
smoke test started a rebuild (stopped in placement). The tuner now imports with `sys.dont_write_bytecode`. Making
`tree_hash` skip `__pycache__` would remove the trap, but it changes every stamp once, so it is left open.

## Where things are

- `misc/int32_verify/`: the debug case, with the full investigation log in its `README.md` and the scripts:
  - `solve_delta.py` (recover the flipped bits), `lane_sweep.sh` and `lane_delay.py` (move one lane's tap);
  - `identity.sh` (B = I), `screen.sh` and `screen_all.sh` (full exact screens), `load_tune.py` (prototype of
    the tuner).
- `flow/gemmflow/ddr_tune.py` (the tuner), `flow/gemmflow/board.py` (`bring_up`), `scripts/gemm_run.py`,
  `scripts/gemm_dse.py`.
- `boards/dupcb/wl7ddrphy.py` (PHY), `boards/dupcb/ddr_init.py` (calibration).
- Dated record: `docs/notes2/int-dtype-progress.md` (2026-11-24, 07:13–08:30). Report: section "Wrong results
  under load: DDR3 read margin" in `docs/report/sections/10-formats.tex`.
