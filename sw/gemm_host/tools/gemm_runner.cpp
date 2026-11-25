// gemm_runner.cpp - C = A * B on the FPGA (GemmSoC + HLS kernel), with timing, perf counters and verification.
//
//   gemm_runner --n N --k K --m M [--reps R] [--warmup W] [--seed S] [--verify full|sampled|off]
//               [--map a=ch0,b=ch1,c=ch1] [--expect-build-id HEX] [--transposed-a] [--multiples N,K,M]
//               [--dtype float32|int8|uint16|int4|fixed16.8|...] [--value-range LO,HI] [--json]
//
// Row-major: A is N x K, B is K x M, C is N x M (kernels/gemm conventions). The element format must be the build's
// (--dtype, from the build config; default float32). Integer and fixed-point data are raw codes, uniform over the
// whole code range unless --value-range LO,HI (codes) is given; results are checked exactly against a model of the
// kernel's arithmetic (src/numeric.hpp: products and sums wrap at the element width). N, K, M must be multiples of
// the build's bus words (--multiples; default 512/bits elements per 64-byte word). One JSON line per repetition with
// --json. For float32, --value-range LO,HI draws integer values instead of uniform(-1, 1) and checks exactly (every
// partial sum is an integer below 2^24, so the kernel's float result must equal the reference bit for bit): a
// corrupted element is then visible instead of hidden by the relative tolerance. --dump-bad FILE writes the
// mismatching entries as "i j got ref" lines; --b-identity (K == M) sets B = I, so C is A as the kernel read it;
// --a-identity (N == K) sets A = I, so C is B. --float-exact checks float32 bit for bit against a reference that
// accumulates like the kernel's PEs (fp32, c = c + a*b in k order, round to nearest; the host build has no FMA
// contraction: -std=c++17 and no -march), so even a flipped low mantissa bit counts as a mismatch.
// Mixed formats (kernels/gemm_mixed, docs/notes3): --b-dtype SPEC --c-dtype SPEC (B int/uint or dshift<n> DenseShift
// codes, C int16/int32), e.g. --dtype int8 --b-dtype dshift3 --c-dtype int32: C = sum value(a) * value(b) wrapped to C.
// --a-file / --b-file read the codes (int64, A row-major N x K, B row-major K x M) instead of random ones; --c-out
// writes C (int64, N x M) of the last repetition. --value-range then applies to A only.
// Exit status: 0 = all repetitions verified (or --verify off), 1 = mismatch, 2 = usage, 3 = error.
#include <algorithm>
#include <array>
#include <chrono>
#include <cinttypes>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "card.hpp"
#include "ddr_dma.hpp"
#include "gemm_kernel.hpp"
#include "numeric.hpp"

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
    // 0 = not given: whole 64-byte words (N: 512/bits with transposed A, else 1; K and M: 512/bits).
    uint32_t mult[3] = {0, 0, 0};
    Dtype dt;                   // element format of the build (default float32)
    bool have_range = false;    // --value-range: integer/fixed codes in [lo, hi] (float32: integer values, exact check)
    int64_t lo = 0, hi = 0;
    std::string dump_bad;       // --dump-bad FILE: mismatches as "i j got ref" lines (debugging)
    bool b_identity = false;    // --b-identity (K == M): B = I, so C = A as the kernel read it (debugging)
    bool a_identity = false;    // --a-identity (N == K): A = I, so C = B as the kernel read it (debugging)
    std::string dump_inputs;    // --dump-inputs PREFIX: PREFIX.a / PREFIX.b raw arrays (A row-major N x K, B row-major K x M;
                                // int64 codes, or float32) for offline analysis of mismatches (debugging)
    bool float_exact = false;   // --float-exact: float32 reference in the kernel's order (c = c + a*b, k = 0..K-1, fp32)
    // Mixed formats (kernels/gemm_mixed): --b-dtype / --c-dtype give B and C their own format (B may be dshift<n>);
    // otherwise both equal --dtype. --value-range then applies to A only.
    Dtype bdt, cdt;
    bool mixed = false;
    std::string a_file, b_file, c_out;  // int64 codes: A row-major N x K, B row-major K x M; C written N x M
    std::string spec() const { return mixed ? mixed_spec(dt, bdt, cdt) : dt.spec(); }
};

[[noreturn]] void usage(const char *argv0) {
    std::fprintf(stderr, "usage: %s --n N --k K --m M [--reps R] [--warmup W] [--seed S] [--verify full|sampled|off]\n"
                         "          [--map a=ch0,b=ch1,c=ch1] [--expect-build-id HEX] [--transposed-a]\n"
                         "          [--multiples N,K,M] [--dtype SPEC] [--b-dtype SPEC --c-dtype SPEC] [--value-range LO,HI] [--dump-bad FILE]\n"
                         "          [--a-identity] [--b-identity] [--float-exact] [--dump-inputs PREFIX]\n"
                         "          [--a-file F --b-file F] [--c-out F] [--json]\n", argv0);
    std::exit(2);
}

Options parse(int argc, char **argv) {
    Options o;
    bool have_b = false, have_c = false;
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
        } else if (a == "--dtype") {
            const std::string spec = next();
            try { o.dt = Dtype::parse(spec); } catch (const std::exception &e) { std::fprintf(stderr, "error: %s\n", e.what()); std::exit(2); }
        } else if (a == "--b-dtype" || a == "--c-dtype") {
            const std::string spec = next();
            try { (a == "--b-dtype" ? o.bdt : o.cdt) = Dtype::parse(spec); }
            catch (const std::exception &e) { std::fprintf(stderr, "error: %s\n", e.what()); std::exit(2); }
            o.mixed = true;
            (a == "--b-dtype" ? have_b : have_c) = true;
        } else if (a == "--a-file") o.a_file = next();
        else if (a == "--b-file") o.b_file = next();
        else if (a == "--c-out") o.c_out = next();
        else if (a == "--value-range") {
            long long lo, hi;
            if (std::sscanf(next().c_str(), "%lld,%lld", &lo, &hi) != 2 || lo > hi) usage(argv[0]);
            o.have_range = true; o.lo = lo; o.hi = hi;
        } else if (a == "--dump-bad") o.dump_bad = next();
        else if (a == "--b-identity") o.b_identity = true;
        else if (a == "--a-identity") o.a_identity = true;
        else if (a == "--float-exact") o.float_exact = true;
        else if (a == "--dump-inputs") o.dump_inputs = next();
        else usage(argv[0]);
    }
    if (o.have_range && o.dt.is_float()) {
        const double vmax = double(std::max(std::llabs(o.lo), std::llabs(o.hi)));
        if (double(o.k) * vmax * vmax >= double(1 << 24)) {
            std::fprintf(stderr, "error: float32 --value-range: K * max|v|^2 must stay below 2^24 for an exact check\n");
            std::exit(2);
        }
    } else if (o.have_range && (o.lo < o.dt.min_code() || (o.dt.bits < 64 && o.hi > o.dt.max_code()))) {
        std::fprintf(stderr, "error: --value-range: codes must be in [%lld, %lld]\n",
                     (long long)o.dt.min_code(), (long long)o.dt.max_code());
        std::exit(2);
    }
    if (o.mixed) {
        if (!have_b) o.bdt = o.dt;
        if (!have_c) o.cdt = Dtype::parse("int32");
        const bool ok = o.dt.kind == Dtype::Kind::Int && o.dt.bits <= 16 &&
                        (o.bdt.kind == Dtype::Kind::Int || o.bdt.is_pow2()) && o.bdt.bits <= 16 &&
                        o.cdt.kind == Dtype::Kind::Int && o.cdt.is_signed && (o.cdt.bits == 16 || o.cdt.bits == 32);
        if (!ok) {
            std::fprintf(stderr, "error: mixed formats: A int/uint <= 16 bits, B int/uint <= 16 bits or dshift<n>, "
                                 "C int16/int32 (got %s)\n", o.spec().c_str());
            std::exit(2);
        }
        if (o.b_identity && o.bdt.is_pow2()) { std::fprintf(stderr, "error: --b-identity: DenseShift has no zero\n"); std::exit(2); }
    } else {
        o.bdt = o.cdt = o.dt;
    }
    if ((!o.a_file.empty() || !o.b_file.empty() || !o.c_out.empty()) && o.dt.is_float()) {
        std::fprintf(stderr, "error: --a-file / --b-file / --c-out: integer formats only\n");
        std::exit(2);
    }
    if (o.b_identity && o.k != o.m) { std::fprintf(stderr, "error: --b-identity needs K == M\n"); std::exit(2); }
    if (o.a_identity && o.n != o.k) { std::fprintf(stderr, "error: --a-identity needs N == K\n"); std::exit(2); }
    const uint32_t epw = o.dt.elems_per_word();
    if (o.mult[0] == 0) o.mult[0] = o.transposed_a ? epw : 1;
    if (o.mult[1] == 0) o.mult[1] = epw;
    if (o.mult[2] == 0) o.mult[2] = std::max(o.bdt.elems_per_word(), o.cdt.elems_per_word());  // powers of two: lcm
    return o;
}

// Sampled or full set of (i, j) entries to check.
std::vector<std::pair<uint32_t, uint32_t>> check_indices(const Options &o, uint64_t seed) {
    std::vector<std::pair<uint32_t, uint32_t>> idx;
    if (o.verify == "full") {
        for (uint32_t i = 0; i < o.n; i++) for (uint32_t j = 0; j < o.m; j++) idx.push_back({i, j});
    } else {
        std::mt19937_64 rng(seed ^ 0x5eed);
        for (int s = 0; s < 4096; s++) idx.push_back({uint32_t(rng() % o.n), uint32_t(rng() % o.m)});
        idx.push_back({0, 0}); idx.push_back({o.n - 1, o.m - 1});
    }
    return idx;
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
using BadList = std::vector<std::vector<std::array<int64_t, 4>>>;  // per thread: i, j, got, ref (--dump-bad)

void write_bad(const Options &o, const BadList &bad) {
    if (o.dump_bad.empty()) return;
    if (FILE *f = std::fopen(o.dump_bad.c_str(), "w")) {
        for (auto &b : bad) for (auto &e : b)
            std::fprintf(f, "%lld %lld %lld %lld\n", (long long)e[0], (long long)e[1], (long long)e[2], (long long)e[3]);
        std::fclose(f);
    }
}

Verify verify(const std::vector<float> &a, const std::vector<float> &bt, const std::vector<float> &c,
              const Options &o, uint64_t seed) {
    const double eps = std::ldexp(1.0, -23);
    const double tol_factor = 4.0 * o.k * eps;  // |c - ref| <= 4 K eps sum|a b|
    const auto idx = check_indices(o, seed);
    const unsigned nt = std::max(1u, std::thread::hardware_concurrency());
    std::vector<Verify> part(nt);
    BadList bad(nt);
    std::vector<std::thread> th;
    for (unsigned t = 0; t < nt; t++)
        th.emplace_back([&, t]() {
            for (size_t q = t; q < idx.size(); q += nt) {
                double ref, absr;
                ref_entry(a, bt, o.k, idx[q].first, idx[q].second, ref, absr);
                const double got = c[size_t(idx[q].first) * o.m + idx[q].second];
                if (o.float_exact) {  // kernel order in fp32
                    const float *ar = &a[size_t(idx[q].first) * o.k], *br = &bt[size_t(idx[q].second) * o.k];
                    float acc = 0.0f;
                    for (uint32_t x = 0; x < o.k; x++) { const float p = ar[x] * br[x]; acc = acc + p; }
                    ref = acc;
                }
                const double err = std::fabs(got - ref);
                const double rel = absr > 0 ? err / absr : err;
                part[t].checked++;
                part[t].max_rel = std::max(part[t].max_rel, rel);
                const bool wrong = (o.have_range || o.float_exact) ? !(got == ref)
                                                                   : (err > tol_factor * absr + 1e-30 || std::isnan(got));
                if (wrong) {
                    part[t].bad++;
                    if (!o.dump_bad.empty()) bad[t].push_back({int64_t(idx[q].first), int64_t(idx[q].second), int64_t(got), int64_t(ref)});
                }
            }
        });
    for (auto &x : th) x.join();
    Verify v;
    for (auto &p : part) { v.checked += p.checked; v.bad += p.bad; v.max_rel = std::max(v.max_rel, p.max_rel); }
    write_bad(o, bad);
    return v;
}

// Integer / fixed point: exact comparison with the kernel-arithmetic model. max_rel holds the largest |got - ref| in
// LSBs (codes), reported as max_abs_err.
Verify verify_codes(const std::vector<int64_t> &a, const std::vector<int64_t> &bt, const std::vector<int64_t> &c,
                    const Options &o, uint64_t seed) {
    const auto idx = check_indices(o, seed);
    const unsigned nt = std::max(1u, std::thread::hardware_concurrency());
    std::vector<Verify> part(nt);
    BadList bad(nt);
    std::vector<std::thread> th;
    for (unsigned t = 0; t < nt; t++)
        th.emplace_back([&, t]() {
            for (size_t q = t; q < idx.size(); q += nt) {
                const uint32_t i = idx[q].first, j = idx[q].second;
                const int64_t ref = o.mixed ? gemm::ref_entry_mixed(o.dt, o.bdt, o.cdt, &a[size_t(i) * o.k], &bt[size_t(j) * o.k], o.k)
                                            : gemm::ref_entry(o.dt, &a[size_t(i) * o.k], &bt[size_t(j) * o.k], o.k);
                const int64_t got = c[size_t(i) * o.m + j];
                part[t].checked++;
                if (got != ref) {
                    part[t].bad++;
                    const __int128 d = __int128(got) - __int128(ref);
                    part[t].max_rel = std::max(part[t].max_rel, double(d < 0 ? -d : d));
                    if (!o.dump_bad.empty()) bad[t].push_back({int64_t(i), int64_t(j), got, ref});
                }
            }
        });
    for (auto &x : th) x.join();
    Verify v;
    for (auto &p : part) { v.checked += p.checked; v.bad += p.bad; v.max_rel = std::max(v.max_rel, p.max_rel); }
    write_bad(o, bad);
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
        const size_t bytes_a = o.dt.bytes(size_t(o.n) * o.k), bytes_b = o.bdt.bytes(size_t(o.k) * o.m),
                     bytes_c = o.cdt.bytes(size_t(o.n) * o.m);
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

        // Host data and what goes to the card. A goes as A (N x K) or, for transposed-A builds, A^T (K x N); the
        // references always use A and B^T. Float: as before. Integer/fixed: raw codes, packed (src/numeric.hpp).
        const Dtype &dt = o.dt;
        std::mt19937_64 rng(o.seed);
        std::vector<float> af, bf, btf, cf;            // float32
        std::vector<int64_t> ai, bti, ci;              // integer / fixed codes
        std::vector<uint8_t> a_dev(round64(bytes_a), 0), b_dev(round64(bytes_b), 0);
        if (dt.is_float()) {
            std::uniform_real_distribution<float> dist(-1.0f, 1.0f);
            std::uniform_int_distribution<int64_t> idist(o.lo, o.hi);
            auto value = [&]() { return o.have_range ? float(idist(rng)) : dist(rng); };
            af.resize(size_t(o.n) * o.k); bf.resize(size_t(o.k) * o.m); btf.resize(size_t(o.m) * o.k);
            for (auto &x : af) x = value();
            for (auto &x : bf) x = value();
            if (o.b_identity) for (uint32_t r = 0; r < o.k; r++) for (uint32_t col = 0; col < o.m; col++) bf[size_t(r) * o.m + col] = r == col;
            if (o.a_identity) for (uint32_t r = 0; r < o.n; r++) for (uint32_t col = 0; col < o.k; col++) af[size_t(r) * o.k + col] = r == col;
            for (uint32_t r = 0; r < o.k; r++) for (uint32_t col = 0; col < o.m; col++) btf[size_t(col) * o.k + r] = bf[size_t(r) * o.m + col];
            std::vector<float> a_send;
            if (o.transposed_a) {
                a_send.resize(size_t(o.k) * o.n);
                for (uint32_t r = 0; r < o.n; r++)
                    for (uint32_t col = 0; col < o.k; col++) a_send[size_t(col) * o.n + r] = af[size_t(r) * o.k + col];
            }
            std::memcpy(a_dev.data(), o.transposed_a ? a_send.data() : af.data(), bytes_a);
            std::memcpy(b_dev.data(), bf.data(), bytes_b);
            cf.resize(size_t(o.n) * o.m);
        } else {
            std::uniform_int_distribution<int64_t> dist(o.lo, o.hi);
            auto code = [&]() { return o.have_range ? dist(rng) : dt.wrap(rng()); };
            auto bcode = [&]() { return (o.have_range && !o.mixed) ? dist(rng) : o.bdt.wrap(rng()); };
            ai.resize(size_t(o.n) * o.k); bti.resize(size_t(o.m) * o.k);
            std::vector<int64_t> bi(size_t(o.k) * o.m);
            for (auto &x : ai) x = code();
            for (auto &x : bi) x = bcode();
            // --a-file / --b-file: given codes instead of random ones (end-to-end runs, ml/denseshift).
            auto load = [&](const std::string &path, std::vector<int64_t> &v, const Dtype &d, const char *what) {
                FILE *f = std::fopen(path.c_str(), "rb");
                if (!f) throw std::runtime_error(std::string("cannot open ") + path);
                const size_t got = std::fread(v.data(), sizeof(int64_t), v.size(), f);
                const bool extra = std::fgetc(f) != EOF;
                std::fclose(f);
                if (got != v.size() || extra) throw std::runtime_error(path + ": expected " + std::to_string(v.size()) + " int64 codes for " + what);
                for (auto &x : v) if (x != d.wrap(uint64_t(x))) throw std::runtime_error(path + ": code out of range for " + d.spec());
            };
            if (!o.a_file.empty()) load(o.a_file, ai, dt, "A (N x K)");
            if (!o.b_file.empty()) load(o.b_file, bi, o.bdt, "B (K x M)");
            if (o.b_identity) for (uint32_t r = 0; r < o.k; r++) for (uint32_t col = 0; col < o.m; col++) bi[size_t(r) * o.m + col] = r == col;
            if (o.a_identity) for (uint32_t r = 0; r < o.n; r++) for (uint32_t col = 0; col < o.k; col++) ai[size_t(r) * o.k + col] = r == col;
            for (uint32_t r = 0; r < o.k; r++) for (uint32_t col = 0; col < o.m; col++) bti[size_t(col) * o.k + r] = bi[size_t(r) * o.m + col];
            if (o.transposed_a) {
                std::vector<int64_t> a_send(size_t(o.k) * o.n);
                for (uint32_t r = 0; r < o.n; r++)
                    for (uint32_t col = 0; col < o.k; col++) a_send[size_t(col) * o.n + r] = ai[size_t(r) * o.k + col];
                gemm::pack(dt, a_send.data(), a_send.size(), a_dev.data());
            } else {
                gemm::pack(dt, ai.data(), ai.size(), a_dev.data());
            }
            gemm::pack(o.bdt, bi.data(), bi.size(), b_dev.data());
            ci.resize(size_t(o.n) * o.m);
        }

        if (!o.dump_inputs.empty()) {
            auto dump = [&](const std::string &path, const void *p, size_t bytes) {
                if (FILE *f = std::fopen(path.c_str(), "wb")) { std::fwrite(p, 1, bytes, f); std::fclose(f); }
            };
            if (dt.is_float()) {
                dump(o.dump_inputs + ".a", af.data(), af.size() * sizeof(float));
                dump(o.dump_inputs + ".b", bf.data(), bf.size() * sizeof(float));
            } else {
                std::vector<int64_t> bdump(size_t(o.k) * o.m);  // bti is B^T: write B row-major
                for (uint32_t r = 0; r < o.k; r++) for (uint32_t col = 0; col < o.m; col++) bdump[size_t(r) * o.m + col] = bti[size_t(col) * o.k + r];
                dump(o.dump_inputs + ".a", ai.data(), ai.size() * sizeof(int64_t));
                dump(o.dump_inputs + ".b", bdump.data(), bdump.size() * sizeof(int64_t));
            }
        }

        int rc = 0;
        for (unsigned rep = 0; rep < o.warmup + o.reps; rep++) {
            const bool warm = rep < o.warmup;
            const auto t0 = std::chrono::steady_clock::now();
            const TransferStats ta = ddr.write(o.ch[0], addr[0], a_dev.data(), round64(bytes_a));
            const TransferStats tb = ddr.write(o.ch[1], addr[1], b_dev.data(), round64(bytes_b));
            const KernelRun kr = kern.run(addr[0], addr[1], addr[2], o.n, o.k, o.m);
            std::vector<uint8_t> cbuf(round64(bytes_c));
            const TransferStats tc = ddr.read(o.ch[2], addr[2], cbuf.data(), round64(bytes_c));
            const double e2e = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
            if (dt.is_float()) std::memcpy(cf.data(), cbuf.data(), bytes_c);
            else gemm::unpack(o.cdt, cbuf.data(), ci.size(), ci.data());
            if (!o.c_out.empty() && !warm) {
                FILE *f = std::fopen(o.c_out.c_str(), "wb");
                if (!f || std::fwrite(ci.data(), sizeof(int64_t), ci.size(), f) != ci.size()) throw std::runtime_error("cannot write " + o.c_out);
                std::fclose(f);
            }

            Verify v;
            if (o.verify != "off") v = dt.is_float() ? verify(af, btf, cf, o, o.seed + rep) : verify_codes(ai, bti, ci, o, o.seed + rep);
            const bool ok = v.bad == 0;
            if (!warm && !ok) rc = 1;
            const double ops = 2.0 * o.n * o.k * o.m;
            const double gops = kr.seconds > 0 ? ops / kr.seconds / 1e9 : 0;
            std::printf("%s rep %u: %ux%ux%u %s  kernel %" PRIu64 " cycles @ %.3f MHz = %.3f ms, %.2f %s  "
                        "H2C %.2f GB/s  C2H %.2f GB/s  e2e %.1f ms  verify %s (%zu checked, %zu bad, max %s %.2e)\n",
                        warm ? "warmup" : "run", rep, o.n, o.k, o.m, o.spec().c_str(), kr.cycles, kr.accel_hz / 1e6,
                        kr.seconds * 1e3, gops, dt.is_float() ? "GFLOP/s" : "GOP/s",
                        (ta.bytes + tb.bytes) / (ta.seconds + tb.seconds) / 1e9, tc.gbps(), e2e * 1e3,
                        o.verify == "off" ? "off" : ok ? "OK" : "FAIL", v.checked, v.bad,
                        dt.is_float() ? "rel" : "abs LSB", v.max_rel);
            if (o.json && !warm) {
                std::printf("{\"build_id\":\"%08x\",\"n\":%u,\"k\":%u,\"m\":%u,\"rep\":%u,\"seed\":%" PRIu64
                            ",\"map\":\"a=ch%u,b=ch%u,c=ch%u\",\"dtype\":\"%s\",\"dtype_kind\":\"%s\",\"dtype_bits\":%u,"
                            "\"dtype_signed\":%s,\"kernel_cycles\":%" PRIu64 ",\"accel_hz\":%.0f,"
                            "\"kernel_s\":%.9f,\"kernel_wall_s\":%.6f,\"gops\":%.4f,",
                            bid, o.n, o.k, o.m, rep - o.warmup, o.seed, o.ch[0], o.ch[1], o.ch[2], o.spec().c_str(),
                            (o.mixed && o.bdt.is_pow2()) ? "pow2" : dt.kind_name(), dt.bits,
                            dt.is_signed ? "true" : "false", kr.cycles, kr.accel_hz, kr.seconds, kr.wall_s, gops);
                if (o.mixed) std::printf("\"a_dtype\":\"%s\",\"b_dtype\":\"%s\",\"c_dtype\":\"%s\",", dt.spec().c_str(),
                                         o.bdt.spec().c_str(), o.cdt.spec().c_str());
                if (dt.is_float()) std::printf("\"gflops\":%.4f,", gops);
                if (o.have_range) std::printf("\"value_range\":[%lld,%lld],", (long long)o.lo, (long long)o.hi);
                std::printf("\"h2c_bytes\":%zu,\"h2c_s\":%.6f,\"c2h_bytes\":%zu,\"c2h_s\":%.6f,\"e2e_s\":%.6f,"
                            "\"verify\":\"%s\",\"verify_exact\":%s,\"verify_checked\":%zu,\"verify_bad\":%zu,",
                            ta.bytes + tb.bytes, ta.seconds + tb.seconds, tc.bytes, tc.seconds, e2e, o.verify.c_str(),
                            (!dt.is_float() || o.float_exact || o.have_range) ? "true" : "false", v.checked, v.bad);
                if (dt.is_float()) std::printf("\"max_rel_err\":%.3e,\"perf\":{", v.max_rel);
                else std::printf("\"max_abs_err\":%.0f,\"perf\":{", v.max_rel);
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
