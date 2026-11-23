// gemm_runner.cpp - C = A * B on the FPGA (GemmSoC + HLS kernel), with timing, perf counters and verification.
//
//   gemm_runner --n N --k K --m M [--reps R] [--warmup W] [--seed S] [--verify full|sampled|off]
//               [--map a=ch0,b=ch1,c=ch1] [--expect-build-id HEX] [--json]
//
// Float32, row-major: A is N x K, B is K x M, C is N x M (kernels/gemm conventions). K and M must be multiples of the
// kernel's memory width (16 floats for 64-byte buses). One JSON line per repetition with --json.
// Exit status: 0 = all repetitions verified (or --verify off), 1 = mismatch, 2 = usage, 3 = error.
#include <algorithm>
#include <chrono>
#include <cinttypes>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include "card.hpp"
#include "ddr_dma.hpp"
#include "gemm_kernel.hpp"

using namespace gemm;

namespace {

struct Options {
    uint32_t n = 256, k = 256, m = 256;
    unsigned reps = 1, warmup = 0;
    uint64_t seed = 1;
    std::string verify = "sampled";
    unsigned ch[3] = {0, 1, 1};  // DDR channel of A, B, C
    long expect_build_id = -1;
    bool json = false;
    bool transposed_a = false;  // build has kernel.transposed_a: send A^T (K x N)
    // N, K, M must be multiples of these (bus words / transpose width of the build, BuildCfg.size_multiples()).
    // 0 = not given: the 64-byte float defaults (N: 16 with transposed A, else 1; K: 16; M: 16).
    uint32_t mult[3] = {0, 0, 0};
};

[[noreturn]] void usage(const char *argv0) {
    std::fprintf(stderr, "usage: %s --n N --k K --m M [--reps R] [--warmup W] [--seed S] [--verify full|sampled|off]\n"
                         "          [--map a=ch0,b=ch1,c=ch1] [--expect-build-id HEX] [--transposed-a]\n"
                         "          [--multiples N,K,M] [--json]\n", argv0);
    std::exit(2);
}

Options parse(int argc, char **argv) {
    Options o;
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        auto next = [&]() -> std::string { if (i + 1 >= argc) usage(argv[0]); return argv[++i]; };
        if (a == "--n") o.n = std::stoul(next());
        else if (a == "--k") o.k = std::stoul(next());
        else if (a == "--m") o.m = std::stoul(next());
        else if (a == "--reps") o.reps = std::stoul(next());
        else if (a == "--warmup") o.warmup = std::stoul(next());
        else if (a == "--seed") o.seed = std::stoull(next());
        else if (a == "--verify") { o.verify = next(); if (o.verify != "full" && o.verify != "sampled" && o.verify != "off") usage(argv[0]); }
        else if (a == "--map") {
            std::string v = next();  // a=ch0,b=ch1,c=ch1
            for (int x = 0; x < 3; x++) {
                const std::string key = std::string(1, "abc"[x]) + "=ch";
                const auto pos = v.find(key);
                if (pos == std::string::npos) usage(argv[0]);
                o.ch[x] = unsigned(v[pos + key.size()] - '0');
                if (o.ch[x] > 1) usage(argv[0]);
            }
        } else if (a == "--expect-build-id") o.expect_build_id = std::stol(next(), nullptr, 16);
        else if (a == "--json") o.json = true;
        else if (a == "--transposed-a") o.transposed_a = true;
        else if (a == "--multiples") {
            if (std::sscanf(next().c_str(), "%u,%u,%u", &o.mult[0], &o.mult[1], &o.mult[2]) != 3) usage(argv[0]);
            for (uint32_t x : o.mult) if (x == 0) usage(argv[0]);
        } else usage(argv[0]);
    }
    if (o.mult[0] == 0) o.mult[0] = o.transposed_a ? 16 : 1;
    if (o.mult[1] == 0) o.mult[1] = 16;
    if (o.mult[2] == 0) o.mult[2] = 16;
    return o;
}

// Reference C[i][j] and the sum of |a_ik * b_kj| (for the tolerance). B is passed transposed (M x K) for locality.
void ref_entry(const std::vector<float> &a, const std::vector<float> &bt, uint32_t k, uint32_t i, uint32_t j,
               double &val, double &absval) {
    const float *ar = &a[size_t(i) * k], *br = &bt[size_t(j) * k];
    double s = 0, sa = 0;
    for (uint32_t x = 0; x < k; x++) { const double p = double(ar[x]) * br[x]; s += p; sa += std::fabs(p); }
    val = s; absval = sa;
}

struct Verify { size_t checked = 0, bad = 0; double max_rel = 0; };

Verify verify(const std::vector<float> &a, const std::vector<float> &bt, const std::vector<float> &c,
              const Options &o, uint64_t seed) {
    const double eps = std::ldexp(1.0, -23);
    const double tol_factor = 4.0 * o.k * eps;  // |c - ref| <= 4 K eps sum|a b|
    std::vector<std::pair<uint32_t, uint32_t>> idx;
    if (o.verify == "full") {
        for (uint32_t i = 0; i < o.n; i++) for (uint32_t j = 0; j < o.m; j++) idx.push_back({i, j});
    } else {
        std::mt19937_64 rng(seed ^ 0x5eed);
        for (int s = 0; s < 4096; s++) idx.push_back({uint32_t(rng() % o.n), uint32_t(rng() % o.m)});
        idx.push_back({0, 0}); idx.push_back({o.n - 1, o.m - 1});
    }
    const unsigned nt = std::max(1u, std::thread::hardware_concurrency());
    std::vector<Verify> part(nt);
    std::vector<std::thread> th;
    for (unsigned t = 0; t < nt; t++)
        th.emplace_back([&, t]() {
            for (size_t q = t; q < idx.size(); q += nt) {
                double ref, absr;
                ref_entry(a, bt, o.k, idx[q].first, idx[q].second, ref, absr);
                const double got = c[size_t(idx[q].first) * o.m + idx[q].second];
                const double err = std::fabs(got - ref);
                const double rel = absr > 0 ? err / absr : err;
                part[t].checked++;
                part[t].max_rel = std::max(part[t].max_rel, rel);
                if (err > tol_factor * absr + 1e-30 || std::isnan(got)) part[t].bad++;
            }
        });
    for (auto &x : th) x.join();
    Verify v;
    for (auto &p : part) { v.checked += p.checked; v.bad += p.bad; v.max_rel = std::max(v.max_rel, p.max_rel); }
    return v;
}

}  // namespace

int main(int argc, char **argv) {
    const Options o = parse(argc, argv);
    if (!GemmKernel::kAvailable) { std::fprintf(stderr, "error: this build has no GEMM kernel\n"); return 3; }
    try {
        Card card;
        DdrDma ddr(card);
        GemmKernel kern(card);
        const uint32_t bid = kern.build_id();
        if (o.expect_build_id >= 0 && uint32_t(o.expect_build_id) != bid) {
            std::fprintf(stderr, "error: card build id %08x != expected %08lx (wrong bitstream loaded?)\n", bid,
                         o.expect_build_id);
            return 3;
        }
        const size_t bytes_a = size_t(o.n) * o.k * 4, bytes_b = size_t(o.k) * o.m * 4, bytes_c = size_t(o.n) * o.m * 4;
        auto round64 = [](size_t x) { return (x + 63) & ~size_t(63); };
        // DDR layout (docs/notes1/gemm-hls-progress.md, "DDR memory map"): each matrix at the start of its channel;
        // B at 512 MB if it shares the channel with A, C at 1 GB if it shares it with A or B.
        const uint32_t addr[3] = {0, (o.ch[1] == o.ch[0]) ? 0x20000000u : 0u,
                                  (o.ch[2] == o.ch[0] || o.ch[2] == o.ch[1]) ? 0x40000000u : 0u};
        // Capacity: each matrix (plus the C2H read slack for C) must end before the next region on its channel.
        {
            const size_t bytes[3] = {round64(bytes_a), round64(bytes_b), round64(bytes_c) + 2 * Card::kBufferBytes};
            const char *names = "ABC";
            for (int x = 0; x < 3; x++) {
                uint64_t limit = DdrDma::kChannelBytes;
                for (int y = 0; y < 3; y++)
                    if (y != x && o.ch[y] == o.ch[x] && addr[y] > addr[x]) limit = std::min<uint64_t>(limit, addr[y]);
                if (addr[x] + bytes[x] > limit) {
                    std::fprintf(stderr, "error: %c (%zu bytes at 0x%08x on CH%u) does not fit before 0x%08" PRIx64 "\n",
                                 names[x], bytes[x], addr[x], o.ch[x], limit);
                    return 2;
                }
            }
        }
        // The kernel reads whole bus words: other sizes give wrong results, not an error (gemm_hls RunHardware.cpp).
        {
            const uint32_t size[3] = {o.n, o.k, o.m};
            for (int x = 0; x < 3; x++)
                if (size[x] % o.mult[x]) {
                    std::fprintf(stderr, "error: %c = %u must be a multiple of %u for this build\n", "NKM"[x], size[x],
                                 o.mult[x]);
                    return 2;
                }
        }

        std::mt19937_64 rng(o.seed);
        std::uniform_real_distribution<float> dist(-1.0f, 1.0f);
        std::vector<float> a(size_t(o.n) * o.k), b(size_t(o.k) * o.m), bt(size_t(o.m) * o.k);
        for (auto &x : a) x = dist(rng);
        for (auto &x : b) x = dist(rng);
        for (uint32_t r = 0; r < o.k; r++) for (uint32_t col = 0; col < o.m; col++) bt[size_t(col) * o.k + r] = b[size_t(r) * o.m + col];
        std::vector<float> c(size_t(o.n) * o.m);
        // What goes to the card for A: A (N x K) or, for transposed-A builds, A^T (K x N). The reference uses A.
        std::vector<float> a_dev;
        if (o.transposed_a) {
            a_dev.resize(size_t(o.k) * o.n);
            for (uint32_t r = 0; r < o.n; r++)
                for (uint32_t col = 0; col < o.k; col++) a_dev[size_t(col) * o.n + r] = a[size_t(r) * o.k + col];
        } else {
            a_dev = a;
        }
        a_dev.resize(round64(bytes_a) / 4, 0.0f);  // DMA lengths in 64-byte words
        b.resize(round64(bytes_b) / 4, 0.0f);

        int rc = 0;
        for (unsigned rep = 0; rep < o.warmup + o.reps; rep++) {
            const bool warm = rep < o.warmup;
            const auto t0 = std::chrono::steady_clock::now();
            const TransferStats ta = ddr.write(o.ch[0], addr[0], a_dev.data(), round64(bytes_a));
            const TransferStats tb = ddr.write(o.ch[1], addr[1], b.data(), round64(bytes_b));
            const KernelRun kr = kern.run(addr[0], addr[1], addr[2], o.n, o.k, o.m);
            std::vector<float> cbuf(round64(bytes_c) / 4);
            const TransferStats tc = ddr.read(o.ch[2], addr[2], cbuf.data(), round64(bytes_c));
            const double e2e = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
            std::memcpy(c.data(), cbuf.data(), bytes_c);

            Verify v;
            if (o.verify != "off") v = verify(a, bt, c, o, o.seed + rep);
            const bool ok = v.bad == 0;
            if (!warm && !ok) rc = 1;
            const double flops = 2.0 * o.n * o.k * o.m;
            const double gflops = kr.seconds > 0 ? flops / kr.seconds / 1e9 : 0;
            std::printf("%s rep %u: %ux%ux%u  kernel %" PRIu64 " cycles @ %.3f MHz = %.3f ms, %.2f GFLOP/s  "
                        "H2C %.2f GB/s  C2H %.2f GB/s  e2e %.1f ms  verify %s (%zu checked, %zu bad, max rel %.2e)\n",
                        warm ? "warmup" : "run", rep, o.n, o.k, o.m, kr.cycles, kr.accel_hz / 1e6, kr.seconds * 1e3,
                        gflops, (ta.bytes + tb.bytes) / (ta.seconds + tb.seconds) / 1e9, tc.gbps(), e2e * 1e3,
                        o.verify == "off" ? "off" : ok ? "OK" : "FAIL", v.checked, v.bad, v.max_rel);
            if (o.json && !warm) {
                std::printf("{\"build_id\":\"%08x\",\"n\":%u,\"k\":%u,\"m\":%u,\"rep\":%u,\"seed\":%" PRIu64
                            ",\"map\":\"a=ch%u,b=ch%u,c=ch%u\",\"kernel_cycles\":%" PRIu64 ",\"accel_hz\":%.0f,"
                            "\"kernel_s\":%.9f,\"kernel_wall_s\":%.6f,\"gflops\":%.4f,\"h2c_bytes\":%zu,\"h2c_s\":%.6f,"
                            "\"c2h_bytes\":%zu,\"c2h_s\":%.6f,\"e2e_s\":%.6f,\"verify\":\"%s\",\"verify_checked\":%zu,"
                            "\"verify_bad\":%zu,\"max_rel_err\":%.3e,\"perf\":{",
                            bid, o.n, o.k, o.m, rep - o.warmup, o.seed, o.ch[0], o.ch[1], o.ch[2], kr.cycles,
                            kr.accel_hz, kr.seconds, kr.wall_s, gflops, ta.bytes + tb.bytes, ta.seconds + tb.seconds,
                            tc.bytes, tc.seconds, e2e, o.verify.c_str(), v.checked, v.bad, v.max_rel);
                bool first_m = true;
                for (auto &mat : kr.perf) {
                    std::printf("%s\"%s\":{", first_m ? "" : ",", mat.first.c_str());
                    bool first_c = true;
                    for (auto &cnt : mat.second) {
                        std::printf("%s\"%s\":%" PRIu64, first_c ? "" : ",", cnt.first.c_str(), cnt.second);
                        first_c = false;
                    }
                    std::printf("}");
                    first_m = false;
                }
                std::printf("}}\n");
            }
        }
        return rc;
    } catch (const std::exception &e) {
        std::fprintf(stderr, "error: %s\n", e.what());
        return 3;
    }
}
