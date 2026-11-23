// gemm_kernel.hpp - control and counters of the HLS GEMM kernel in GemmSoC (hw/gemm/hls_kernel.py).
// Available only for builds with the kernel (their csr.h has the gemm_* registers): check GemmKernel::kAvailable.
#pragma once

#include <cstdint>
#include <map>
#include <string>

#include "card.hpp"

namespace gemm {

struct KernelRun {
    uint64_t cycles   = 0;    // accel cycles start -> done
    double   accel_hz = 0;    // measured accel clock (FreqMeter)
    double   seconds  = 0;    // cycles / accel_hz
    double   wall_s   = 0;    // host wall time start -> done seen (includes CSR polling)
    std::map<std::string, std::map<std::string, uint64_t>> perf;  // matrix (a|b|c) -> counter -> value
};

class GemmKernel {
public:
    static const bool kAvailable;

    explicit GemmKernel(Card &card) : card_(card) {}

    uint32_t build_id() const;
    // a, b, c: byte addresses inside the DDR channel of each matrix (see the build's memory_map).
    KernelRun run(uint32_t a, uint32_t b, uint32_t c, uint32_t n, uint32_t k, uint32_t m, double timeout_s = 60.0);

private:
    Card &card_;
};

}  // namespace gemm
