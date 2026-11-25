// litex-matmul (kernels/gemm_mixed): the multiply-accumulate of one PE lane, chosen in Config.h
// (docs/notes3/denseshift-plan.md, Part B).
//
//   MM_MAC_MULTIPLY    C += A * B                      product in kBitsA + kBitsB bits, sum wraps at kBitsC
//   MM_MAC_DENSESHIFT  C += (-1)^s * (A << e)          B = DenseShift code of kShiftBits bits (Li et al., ICCV 2023):
//                                                      s = bit kShiftBits-1, e = bits [kShiftBits-2:0]; no zero
//                                                      weight, so no zero test (paper Fig. 3b). Sum wraps at kBitsC.
//
// A and B are ap_int/ap_uint types; the arithmetic is done in exact widths so that HLS sees the real operand sizes.
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
  return negative ? DataC_t(acc - shifted) : DataC_t(acc + shifted);
}

#else
#error "Config.h must define MM_MAC_MULTIPLY or MM_MAC_DENSESHIFT"
#endif
