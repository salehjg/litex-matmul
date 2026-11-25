# kernels/gemm_mixed: origin and changes

A copy of [`kernels/gemm`](../gemm/ORIGIN.md) (itself adapted from spcl/gemm_hls, BSD-3-Clause, see `LICENSE.md`)
with separate element types for A, B and C and a choice of MAC, for DenseShift (docs/notes3/denseshift-plan.md).
`kernels/gemm` is left as it is, so every earlier build id and HLS hash stays valid; the flow picks this tree for
configs with `kernel.b_type` / `b_bits` or an `accum_bits` wider than the element (`BuildCfg.kernel_dir()`).

The first commit of this directory (`b377caa`) is a verbatim copy of `kernels/gemm`; `git log -p kernels/gemm_mixed`
shows every change since.

## Changes against kernels/gemm

- `include/Config.h.in`: `DataA_t`, `DataB_t`, `DataC_t` (always `ap_int`/`ap_uint`), their bit widths, one memory
  width per operand (`kMemoryWidthBytesN/K` for A, `M` for B, `C` for C), `MM_MAC_MULTIPLY` or `MM_MAC_DENSESHIFT`
  with `kShiftBits`. Includes `ap_int.h` and requires `-DAP_INT_MAX_W=4096` (an int32 C pack with PM 64 is 2048
  bits). The upstream `OperatorMap`/`OperatorReduce` and the resource pragmas are gone.
- `include/Mac.h` (new): `Mac(acc, a, b)`. Multiply: `acc + a * b` in exact widths. DenseShift:
  `acc + (s ? -(a << e) : (a << e))` with `s = b[n-1]`, `e = b[n-2:0]`, no zero test (paper Fig. 3b): the narrow
  term is negated, then one C-width adder (12 % fewer LUTs by the HLS estimate than an adder, a subtractor and a
  mux). Sums wrap at the C width. Float activations (`MM_MAC_DENSESHIFT_FLOAT`, A and C float): e is added to A's
  exponent field and its sign bit flipped (paper Sec. 3.1.1), then a float add; zero/denormal A gives a signed zero.
- `include/MatrixMultiplication.h`: A packs of `DataA_t`, B packs of `DataB_t`, new `MemoryPackC_t`,
  `ComputePackC_t` (`OutputPack_t`), `kMemoryWidthC`, `kOuterTileSizeCMemory`, `SizeCMemory()`,
  `kCWordsPerPack` / `kCPacksPerWord`; the kernel's C port is `MemoryPackC_t`.
- `src/Compute.cpp`: the C buffer, C pipes and stores are `ComputePackC_t`; the MAC is `Mac()`. The C buffer is
  bound as `ram_s2p` (one write, one read port), so Vivado can use 72-bit BRAM36 at depth ≤ 512; the default `ram_2p`
  became true-dual-port blocks with 36-bit ports in kernels/gemm (half of each BRAM36 unused).
- `src/Memory.cpp`, `include/Memory.h`: A paths use `DataA_t`. `ConvertWidthC` handles three cases in one loop with
  `II = kCPacksPerWord`: gather several compute packs into one C word (as before), pass one through, or split one
  compute pack over `kCWordsPerPack` C words (int32 C with PM > 16). `WriteC` and `IndexC` count C words.
- `src/Top.cpp`: stream and port types as above.
- `include/Utility.h`: removed (only the old testbench used it).
- `tb/test_gemm.cpp`: new testbench: random codes over the whole range of A and B (all DenseShift codes), int64
  reference wrapped once to the C width, exact compare. `misc/gemm_mixed_sim/sim.py` runs it for every MAC, format
  and C conversion case. Float activations: A uniform in (−1, 1) with some zeros, reference = float sums in k order.
