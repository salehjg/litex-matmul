// litex-matmul (kernels/gemm_mixed): the multiply-accumulate of one PE lane, chosen in Config.h
// (docs/notes3/denseshift-plan.md, Part B).
//
//   MM_MAC_MULTIPLY    C += A * B                      product in kBitsA + kBitsB bits, sum wraps at kBitsC
//   MM_MAC_DENSESHIFT  C += (-1)^s * (A << e)          B = DenseShift code of kShiftBits bits (Li et al., ICCV 2023):
//                                                      s = bit kShiftBits-1, e = bits [kShiftBits-2:0]; no zero
//                                                      weight, so no zero test (paper Fig. 3b). Sum wraps at kBitsC.
//   MM_MAC_DENSESHIFT_FLOAT  C += (-1)^s * 2^e * A   A, C float: add e to A's exponent field and flip its sign bit
//                                                      (paper Sec. 3.1.1, Eq. 2), then a float add; no multiplier.
//
// A and B are ap_int/ap_uint types (A and C float for MM_MAC_DENSESHIFT_FLOAT); integer arithmetic is done in exact
// widths so that HLS sees the real operand sizes.
#pragma once

#include "Config.h"

#if defined(MM_MAC_MULTIPLY)

inline DataC_t Mac(const DataC_t acc, const DataA_t a, const DataB_t b) {
  #pragma HLS INLINE
  const auto product = a * b;  // ap_[u]int<kBitsA> * ap_[u]int<kBitsB>: exact
  return DataC_t(acc + product);
}

#elif defined(MM_MAC_DENSESHIFT)

static_assert(kShiftBits >= 2 && kShiftBits <= kBitsB, "DenseShift code must fit the B lane");
constexpr int kMaxShift = (1 << (kShiftBits - 1)) - 1;
// a << e with e <= kMaxShift, one more bit so that unsigned A stays positive in a signed type.
using Shifted_t = ap_int<kBitsA + kMaxShift + 1>;

inline DataC_t Mac(const DataC_t acc, const DataA_t a, const DataB_t b) {
  #pragma HLS INLINE
  const ap_uint<kShiftBits - 1> e = b(kShiftBits - 2, 0);
  const bool negative = b[kShiftBits - 1];
  const Shifted_t shifted = Shifted_t(a) << e;
  // Negate the narrow term, then one C-width adder. HLS estimates for int8 x dshift3 -> int32 at PE 32x32
  // (misc/ds_mac): acc - x / acc + x with a mux 331.6k LUT; this form 290.6k; acc + (x ^ s) + s (carry-in) 300.9k.
  const Shifted_t term = negative ? Shifted_t(-shifted) : shifted;
  return DataC_t(acc + term);
}

#elif defined(MM_MAC_DENSESHIFT_FLOAT)

static_assert(kShiftBits >= 2 && kShiftBits <= kBitsB, "DenseShift code must fit the B lane");

// x * (-1)^s * 2^e by integer operations on the float's bits. Zero and denormal x give a signed zero (denormals are
// flushed). No overflow check: the exponent field plus e (<= 7 for 4-bit codes) only overflows for |x| >= 2^120.
inline DataC_t Mac(const DataC_t acc, const DataA_t a, const DataB_t b) {
  #pragma HLS INLINE
  const ap_uint<kShiftBits - 1> e = b(kShiftBits - 2, 0);
  const bool negative = b[kShiftBits - 1];
  union { float f; uint32_t u; } in, out;
  in.f = a;
  const uint32_t exponent = (in.u >> 23) & 0xFFu;
  uint32_t u = in.u ^ (uint32_t(negative) << 31);
  u = (exponent == 0) ? (u & 0x80000000u) : (u + (uint32_t(e) << 23));
  out.u = u;
  return acc + out.f;
}

#else
#error "Config.h must define MM_MAC_MULTIPLY, MM_MAC_DENSESHIFT or MM_MAC_DENSESHIFT_FLOAT"
#endif
