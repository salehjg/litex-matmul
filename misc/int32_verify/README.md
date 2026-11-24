# int32_verify — wrong results from the int32 build at PE 32×8

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
  one A element is wrong, and A is read again for every M tile. So the corruption is on the **A read path**
  (A is on DDR channel 0; B and C are on channel 1 and are clean).
- With transposed A, a 64-byte word holds 16 consecutive rows of one A^T row, so `i mod 16` is the **lane in the
  512-bit word**. Only lanes 6, 10, 12 and 14 fail.
- The C differences for small data are multiples of 2^12…2^16. So the **upper bits of those lanes** are wrong, and
  only when they toggle: non-negative small values (upper bits always 0), small negative values (always 1) and a
  constant word never fail. Mixed signs or the full range fail.
- That looks like particular bits sampled wrong in silicon (a path that is not timed, or timed with too little margin),
  not a logic error. Every PE runs the same RTL.
- **float32 at the same array (`a88777ec`) is clean:** exact test with integer-valued floats (`--value-range` in
  float mode, 1024³, ±100, ±127, 0…127): 0 bad. The float campaign results are not affected.

## Suspect
The DRAM ports cross from each controller's clock to `accel` through LiteDRAM's native-port CDC (AsyncFIFOs). The
SoC declares `accel` and `sys` asynchronous (`set_clock_groups -asynchronous`), so Vivado does not time those
paths at all. A 512-bit read-data FIFO whose routes are long in one placement would give exactly this signature:
fixed bit positions, only when the data changes, and only in one build. Next check: open
`gateware/dupcb_route.dcp` and report the delays from the channel 0 read-data FIFO storage to its read register for
lanes 6/10/12/14 against the other lanes (needs ~5 GB of RAM: run while no Vivado build is active).
