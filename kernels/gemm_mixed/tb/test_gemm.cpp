// litex-matmul (kernels/gemm_mixed): g++ simulation test of the mixed-type kernel (docs/notes3/denseshift-plan.md).
//
//   ./test_gemm N K M [seed]
//
// A and B are random codes over their whole range (DenseShift B: all 2^kShiftBits codes). The reference sums
// value(a) * value(b) in 64-bit arithmetic and wraps once to kBitsC bits, which equals the kernel's per-step wrap
// (arithmetic mod 2^kBitsC). Float activations (MM_MAC_DENSESHIFT_FLOAT): A uniform in (-1, 1), the reference
// accumulates a * value(b) in float in k order, as each PE does. Exact compare; exit 1 on the first mismatches.
#include <cstdint>
#include <cstring>
#include <iostream>
#include <random>
#include <string>
#include <vector>
#include "MatrixMultiplication.h"

#ifdef MM_MAC_DENSESHIFT_FLOAT
using Ref_t = float;  // the PE's own arithmetic: float sums in k order
static Ref_t ValueA(const DataA_t &a) { return a; }
#else
using Ref_t = int64_t;
static int64_t ValueA(const DataA_t &a) { return a.to_int64(); }
#endif

static int64_t ValueB(const DataB_t &b) {
#if defined(MM_MAC_DENSESHIFT) || defined(MM_MAC_DENSESHIFT_FLOAT)
  const int e = b(kShiftBits - 2, 0).to_int();
  return b[kShiftBits - 1] ? -(int64_t(1) << e) : (int64_t(1) << e);
#else
  return b.to_int64();
#endif
}

static int64_t ToInt(const DataC_t &c) {
#ifdef MM_MAC_DENSESHIFT_FLOAT
  int64_t bits = 0;
  std::memcpy(&bits, &c, sizeof(float));
  return bits;
#else
  return c.to_int64();
#endif
}

#ifndef MM_MAC_DENSESHIFT_FLOAT
static int64_t WrapC(uint64_t x) {
  DataC_t c = DataC_t(int64_t(x));  // keeps the low kBitsC bits, sign-extended for signed C
  return c.to_int64();
}
#endif

template <int W, typename T>
std::vector<hlslib::DataPack<T, W>> Pack(const std::vector<T> &v) {
  std::vector<hlslib::DataPack<T, W>> out(v.size() / W);
  for (size_t i = 0; i < out.size(); ++i)
    for (int w = 0; w < W; ++w) out[i][w] = v[i * W + w];
  return out;
}

int main(int argc, char **argv) {
  if (argc < 4 || argc > 5) {
    std::cerr << "Usage: ./test_gemm N K M [seed]" << std::endl;
    return 1;
  }
  const unsigned size_n = std::stoul(argv[1]);
  const unsigned size_k = std::stoul(argv[2]);
  const unsigned size_m = std::stoul(argv[3]);
  const unsigned seed = argc > 4 ? std::stoul(argv[4]) : kSeed;
#ifdef MM_TRANSPOSED_A
  if (size_n % kMemoryWidthN != 0) { std::cerr << "N must be divisable by the A memory width.\n"; return 1; }
#else
  if (size_k % kMemoryWidthK != 0 || size_k % kTransposeWidth != 0) {
    std::cerr << "K must be divisable by the A memory and transpose widths.\n";
    return 1;
  }
#endif
  if (size_m % kMemoryWidthM != 0 || size_m % kMemoryWidthC != 0) {
    std::cerr << "M must be divisable by the B and C memory widths.\n";
    return 1;
  }

  std::mt19937_64 rng(seed);
  std::vector<DataA_t> a(size_n * size_k);  // row-major N x K
  std::vector<DataB_t> b(size_k * size_m);  // row-major K x M
#ifdef MM_MAC_DENSESHIFT_FLOAT
  std::uniform_real_distribution<float> unif(-1.0f, 1.0f);
  for (auto &x : a) x = unif(rng);
  for (unsigned i = 0; i < a.size(); i += 97) a[i] = 0.0f;  // some zeros (the zero-exponent path)
#else
  for (auto &x : a) x = DataA_t(int64_t(rng()));  // any bit pattern: the whole code range
#endif
#if defined(MM_MAC_DENSESHIFT) || defined(MM_MAC_DENSESHIFT_FLOAT)
  for (auto &x : b) x = DataB_t(rng() & ((1u << kShiftBits) - 1));
#else
  for (auto &x : b) x = DataB_t(int64_t(rng()));
#endif

#ifdef MM_TRANSPOSED_A
  std::vector<DataA_t> at(size_k * size_n);  // the kernel reads A^T, row-major K x N
  for (unsigned i = 0; i < size_n; ++i)
    for (unsigned k = 0; k < size_k; ++k) at[k * size_n + i] = a[i * size_k + k];
  const auto aKernel = Pack<kMemoryWidthA>(at);
#else
  const auto aKernel = Pack<kMemoryWidthA>(a);
#endif
  const auto bKernel = Pack<kMemoryWidthM>(b);
  std::vector<MemoryPackC_t> cKernel(size_n * size_m / kMemoryWidthC);

  std::cout << "Running simulation...\n" << std::flush;
  MatrixMultiplicationKernel(aKernel.data(), bKernel.data(), cKernel.data(), size_n, size_k, size_m);
  std::cout << "Verifying results...\n" << std::flush;

  unsigned bad = 0;
  for (unsigned i = 0; i < size_n; ++i) {
    for (unsigned j = 0; j < size_m; ++j) {
#ifdef MM_MAC_DENSESHIFT_FLOAT
      float acc = 0.0f;
      for (unsigned k = 0; k < size_k; ++k) acc = acc + ValueA(a[i * size_k + k]) * float(ValueB(b[k * size_m + j]));
      DataC_t ref_c = acc;
      const int64_t ref = ToInt(ref_c);
#else
      uint64_t acc = 0;
      for (unsigned k = 0; k < size_k; ++k) acc += uint64_t(ValueA(a[i * size_k + k]) * ValueB(b[k * size_m + j]));
      const int64_t ref = WrapC(acc);
#endif
      const DataC_t got_c = cKernel[(i * size_m + j) / kMemoryWidthC][(i * size_m + j) % kMemoryWidthC];
      const int64_t got = ToInt(got_c);
      if (got != ref && bad++ < 10)
        std::cerr << "Mismatch at (" << i << ", " << j << "): " << got << " vs. " << ref << "\n";
    }
  }
  if (bad) {
    std::cerr << bad << " of " << size_n * size_m << " entries wrong\n";
    return 1;
  }
  std::cout << "Matrix-matrix multiplication successfully verified (" << size_n << "x" << size_k << "x" << size_m
            << ", " << kBitsA << "-bit A, " << kBitsB << "-bit B, " << kBitsC << "-bit C"
#ifdef MM_MAC_DENSESHIFT_FLOAT
            << ", float"
#endif
            << ").\n";
  return 0;
}
