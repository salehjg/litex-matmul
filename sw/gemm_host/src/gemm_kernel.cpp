// gemm_kernel.cpp - see gemm_kernel.hpp.
#include "gemm_kernel.hpp"

#include <chrono>
#include <string>

extern "C" {
#include "csr.h"
}

namespace gemm {

#ifdef CSR_GEMM_START_ADDR

const bool GemmKernel::kAvailable = true;

namespace {

uint64_t read_csr(const Card &card, uint32_t addr, unsigned words) {
    uint64_t v = 0;
    for (unsigned i = 0; i < words; i++) v = (v << 32) | card.csr_read(addr + 4 * i);  // MSB word first
    return v;
}

// Perf counter X-list: (matrix, counter, CSR address, size in 32-bit words), from the build's csr.h.
#define GEMM_PERF_COUNTER(mat, MAT, name, NAME) \
    {#mat, #name, CSR_GEMM_PERF_##MAT##_##NAME##_ADDR, CSR_GEMM_PERF_##MAT##_##NAME##_SIZE},
#define GEMM_PERF_MATRIX(mat, MAT)                                                            \
    GEMM_PERF_COUNTER(mat, MAT, ar_count, AR_COUNT) GEMM_PERF_COUNTER(mat, MAT, aw_count, AW_COUNT)      \
    GEMM_PERF_COUNTER(mat, MAT, r_beats, R_BEATS) GEMM_PERF_COUNTER(mat, MAT, w_beats, W_BEATS)          \
    GEMM_PERF_COUNTER(mat, MAT, b_count, B_COUNT) GEMM_PERF_COUNTER(mat, MAT, ar_stall, AR_STALL)        \
    GEMM_PERF_COUNTER(mat, MAT, aw_stall, AW_STALL) GEMM_PERF_COUNTER(mat, MAT, r_wait, R_WAIT)          \
    GEMM_PERF_COUNTER(mat, MAT, r_backpressure, R_BACKPRESSURE)                                          \
    GEMM_PERF_COUNTER(mat, MAT, w_stall, W_STALL) GEMM_PERF_COUNTER(mat, MAT, b_wait, B_WAIT)            \
    GEMM_PERF_COUNTER(mat, MAT, rd_outstanding_sum, RD_OUTSTANDING_SUM)                                  \
    GEMM_PERF_COUNTER(mat, MAT, rd_lat_sum, RD_LAT_SUM) GEMM_PERF_COUNTER(mat, MAT, rd_lat_min, RD_LAT_MIN) \
    GEMM_PERF_COUNTER(mat, MAT, rd_lat_max, RD_LAT_MAX) GEMM_PERF_COUNTER(mat, MAT, ts_overflow, TS_OVERFLOW)

struct PerfCsr {
    const char *mat, *name;
    uint32_t addr;
    unsigned words;
};
const PerfCsr kPerf[] = {GEMM_PERF_MATRIX(a, A) GEMM_PERF_MATRIX(b, B) GEMM_PERF_MATRIX(c, C)};

}  // namespace

uint32_t GemmKernel::build_id() const { return card_.csr_read(CSR_GEMM_BUILD_ID_ADDR); }

KernelRun GemmKernel::run(uint32_t a, uint32_t b, uint32_t c, uint32_t n, uint32_t k, uint32_t m, double timeout_s) {
    using Clock = std::chrono::steady_clock;
    card_.csr_write(CSR_GEMM_A_ADDR, a);
    card_.csr_write(CSR_GEMM_B_ADDR, b);
    card_.csr_write(CSR_GEMM_C_ADDR, c);
    card_.csr_write(CSR_GEMM_SIZE_N_ADDR, n);
    card_.csr_write(CSR_GEMM_SIZE_K_ADDR, k);
    card_.csr_write(CSR_GEMM_SIZE_M_ADDR, m);
    card_.csr_read(CSR_GEMM_SIZE_M_ADDR);  // flush the posted writes; the args cross with MultiReg (few cycles)

    const auto t0 = Clock::now();
    card_.csr_write(CSR_GEMM_START_ADDR, 1);
    // status: bit0 done, bit1 idle, bit2 busy. Start reaches the kernel domain and clears done a few cycles later
    // (CDC), so first wait until this run is visible (busy, or done cleared), then until it is done.
    while (true) {
        const uint32_t st = card_.csr_read(CSR_GEMM_STATUS_ADDR);
        if ((st & 4) || !(st & 1)) break;
        if (std::chrono::duration<double>(Clock::now() - t0).count() > 0.1)
            throw CardError("GEMM kernel did not start (status " + std::to_string(st) + ")");
    }
    while (true) {
        const uint32_t st = card_.csr_read(CSR_GEMM_STATUS_ADDR);
        if ((st & 1) && !(st & 4)) break;
        if (std::chrono::duration<double>(Clock::now() - t0).count() > timeout_s)
            throw CardError("GEMM kernel timeout (status " + std::to_string(st) + ")");
    }
    KernelRun r;
    r.wall_s   = std::chrono::duration<double>(Clock::now() - t0).count();
    r.cycles   = read_csr(card_, CSR_GEMM_CYCLES_ADDR, CSR_GEMM_CYCLES_SIZE);
    r.accel_hz = double(card_.csr_read(CSR_ACCEL_FREQ_VALUE_ADDR));
    r.seconds  = r.accel_hz > 0 ? r.cycles / r.accel_hz : 0;
    for (const auto &p : kPerf) r.perf[p.mat][p.name] = read_csr(card_, p.addr, p.words);
    return r;
}

#else  // Build without the kernel.

const bool GemmKernel::kAvailable = false;
uint32_t GemmKernel::build_id() const { throw CardError("this build has no GEMM kernel"); }
KernelRun GemmKernel::run(uint32_t, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t, double) {
    throw CardError("this build has no GEMM kernel");
}

#endif

}  // namespace gemm
