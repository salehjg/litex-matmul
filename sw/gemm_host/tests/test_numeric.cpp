// test_numeric.cpp - unit tests for src/numeric.hpp (no card needed): make -C sw/gemm_host test
//
// Hand-computed cases for spec parsing, wrap-around, fixed-point truncation and packing. When the Vitis headers are
// available (the Makefile adds them from config.sh), the model is also checked against ap_int / ap_fixed arithmetic,
// which is what the HLS kernel computes.
#include <cstdio>
#include <random>

#include "numeric.hpp"

#if __has_include(<ap_fixed.h>)
#include <ap_fixed.h>
#define HAVE_AP_TYPES 1
#endif

using gemm::Dtype;

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); failures++; } } while (0)

static int64_t dot(const Dtype &d, std::vector<int64_t> a, std::vector<int64_t> b) {
    return gemm::ref_entry(d, a.data(), b.data(), uint32_t(a.size()));
}

static void test_specs() {
    for (const char *s : {"float32", "int8", "uint8", "int16", "uint32", "int64", "int4", "uint2", "fixed16.8",
                          "ufixed8.4", "fixed32.16"})
        CHECK(Dtype::parse(s).spec() == s);
    for (const char *s : {"float16", "int12", "fixed16", "fixed12.4", "fixed8.8", "ufloat32", "int", "x8", "int8.2"}) {
        bool threw = false;
        try { Dtype::parse(s); } catch (const std::invalid_argument &) { threw = true; }
        CHECK(threw);
    }
    CHECK(Dtype::parse("int8").elems_per_word() == 64);
    CHECK(Dtype::parse("int4").elems_per_word() == 128);
    CHECK(Dtype::parse("int4").bytes(256) == 128);
    CHECK(Dtype::parse("uint8").max_code() == 255);
    CHECK(Dtype::parse("int4").min_code() == -8);
}

static void test_wrap_and_dot() {
    const Dtype i8 = Dtype::parse("int8"), u8 = Dtype::parse("uint8"), i4 = Dtype::parse("int4");
    CHECK(i8.wrap(200) == -56);
    CHECK(i8.wrap(uint64_t(-129)) == 127);
    CHECK(u8.wrap(300) == 44);
    CHECK(i4.wrap(9) == -7);
    CHECK(Dtype::parse("uint2").wrap(5) == 1);
    CHECK(dot(i8, {100}, {100}) == 16);            // 10000 mod 256
    CHECK(dot(i8, {127, 127}, {127, 127}) == 2);   // 32258 mod 256 = 2
    CHECK(dot(i8, {-128}, {-1}) == -128);          // 128 wraps to -128
    CHECK(dot(Dtype::parse("int64"), {INT64_MAX}, {2}) == -2);
    const Dtype f16 = Dtype::parse("fixed16.8");
    CHECK(dot(f16, {384}, {-576}) == -864);        // 1.5 * -2.25 = -3.375
    CHECK(dot(f16, {1}, {128}) == 0);              // 2^-8 * 0.5: truncated toward -inf
    CHECK(dot(f16, {-1}, {128}) == -1);            // -2^-9 -> -2^-8 (AP_TRN = floor)
    CHECK(dot(Dtype::parse("ufixed8.4"), {255}, {255}) == 0xE0);  // 65025 >> 4 = 4064, mod 256 = 224
}

static void test_pack() {
    for (const char *s : {"int2", "uint2", "int4", "uint4", "int8", "uint16", "int32", "int64", "fixed16.8"}) {
        const Dtype d = Dtype::parse(s);
        std::mt19937_64 rng(7);
        std::vector<int64_t> v(1000), back(1000);
        for (auto &x : v) x = d.wrap(rng());
        std::vector<uint8_t> buf(d.bytes(v.size()));
        gemm::pack(d, v.data(), v.size(), buf.data());
        gemm::unpack(d, buf.data(), v.size(), back.data());
        CHECK(v == back);
    }
    const Dtype i4 = Dtype::parse("int4");
    const int64_t codes[4] = {1, -1, 7, -8};
    uint8_t buf[2];
    gemm::pack(i4, codes, 4, buf);
    CHECK(buf[0] == 0xF1 && buf[1] == 0x87);       // element 0 in the low nibble (DataPack bit order)
}

static int64_t dot_mixed(const char *a, const char *b, const char *c, std::vector<int64_t> av, std::vector<int64_t> bv) {
    return gemm::ref_entry_mixed(Dtype::parse(a), Dtype::parse(b), Dtype::parse(c), av.data(), bv.data(),
                                 uint32_t(av.size()));
}

static void test_dshift() {
    for (const char *s : {"dshift2", "dshift3", "dshift4"}) CHECK(Dtype::parse(s).spec() == s);
    for (const char *s : {"dshift1", "dshift5", "udshift3"}) {
        bool threw = false;
        try { Dtype::parse(s); } catch (const std::invalid_argument &) { threw = true; }
        CHECK(threw);
    }
    const Dtype d2 = Dtype::parse("dshift2"), d3 = Dtype::parse("dshift3"), d4 = Dtype::parse("dshift4");
    CHECK(d2.storage() == 2 && d3.storage() == 4 && d4.storage() == 4);
    CHECK(d2.elems_per_word() == 256 && d3.elems_per_word() == 128);
    CHECK(d3.min_code() == 0 && d3.max_code() == 7 && d3.wrap(0xF) == 7);
    // {+-1, +-2} and {+-1, +-2, +-4, +-8}; no zero.
    CHECK(d2.value(0) == 1 && d2.value(1) == 2 && d2.value(2) == -1 && d2.value(3) == -2);
    CHECK(d3.value(3) == 8 && d3.value(4) == -1 && d3.value(7) == -8);
    CHECK(d4.value(7) == 128 && d4.value(15) == -128 && d4.pow2_code(true, 5) == 13);
    // 3*4 + (-2)*(-2) = 16;  int16 C wraps: 127*128*3 = 48768 -> 48768 - 65536 = -16768.
    CHECK(dot_mixed("int8", "dshift3", "int32", {3, -2}, {d3.pow2_code(false, 2), d3.pow2_code(true, 1)}) == 16);
    CHECK(dot_mixed("int8", "dshift4", "int16", {127, 127, 127}, {7, 7, 7}) == -16768);
    CHECK(dot_mixed("uint8", "dshift2", "int32", {255}, {3}) == -510);
    CHECK(dot_mixed("int8", "int8", "int32", {-128, 127}, {-128, 127}) == 16384 + 16129);  // no wrap at 8 bits
    // 3-bit codes in 4-bit lanes: pack / unpack round trip, the high lane bit stays 0.
    std::vector<int64_t> v(999), back(999);
    std::mt19937_64 rng(3);
    for (auto &x : v) x = d3.wrap(rng());
    std::vector<uint8_t> buf(d3.bytes(v.size()));
    gemm::pack(d3, v.data(), v.size(), buf.data());
    gemm::unpack(d3, buf.data(), v.size(), back.data());
    CHECK(v == back);
    bool high_clear = true;
    for (uint8_t byte : buf) high_clear = high_clear && !(byte & 0x88);
    CHECK(high_clear);
}

#ifdef HAVE_AP_TYPES
// The DenseShift MAC as kernels/gemm_mixed/include/Mac.h computes it, against ref_entry_mixed.
template <typename A, int N, typename C>
static void cross_ds(const char *a_spec, const char *b_spec, const char *c_spec) {
    const Dtype da = Dtype::parse(a_spec), db = Dtype::parse(b_spec);
    std::mt19937_64 rng(13);
    for (int t = 0; t < 2000; t++) {
        std::vector<int64_t> a(64), b(64);
        C acc = 0;
        for (int x = 0; x < 64; x++) {
            a[x] = da.wrap(rng()); b[x] = db.wrap(rng());
            A ta = 0;
            ta.range() = a[x];
            const ap_uint<N> code = b[x];
            const ap_uint<N - 1> e = code(N - 2, 0);
            const ap_int<A::width + (1 << (N - 1))> shifted = ap_int<A::width + (1 << (N - 1))>(ta) << e;
            acc = code[N - 1] ? C(acc - shifted) : C(acc + shifted);
        }
        if (acc.to_int64() != dot_mixed(a_spec, b_spec, c_spec, a, b)) {
            std::printf("FAIL cross_ds %s x %s -> %s case %d\n", a_spec, b_spec, c_spec, t); failures++; return;
        }
    }
}

template <typename T>
static void cross_int(const char *spec) {
    const Dtype d = Dtype::parse(spec);
    std::mt19937_64 rng(11);
    for (int t = 0; t < 2000; t++) {
        std::vector<int64_t> a(16), b(16);
        T acc = 0;
        for (int x = 0; x < 16; x++) {
            a[x] = d.wrap(rng()); b[x] = d.wrap(rng());
            T ta = 0, tb = 0;
            ta.range() = a[x]; tb.range() = b[x];
            const T p = ta * tb;   // hlslib Product: const T res = a * b
            acc = acc + p;         // hlslib Sum:     const T res = a + b
        }
        const int64_t got = d.wrap(uint64_t(acc.range().to_uint64()));
        if (got != dot(d, a, b)) { std::printf("FAIL cross %s case %d\n", spec, t); failures++; return; }
    }
}
#endif

int main() {
    test_specs();
    test_wrap_and_dot();
    test_pack();
    test_dshift();
#ifdef HAVE_AP_TYPES
    cross_ds<ap_int<8>, 3, ap_int<32>>("int8", "dshift3", "int32");
    cross_ds<ap_int<8>, 4, ap_int<16>>("int8", "dshift4", "int16");
    cross_ds<ap_uint<8>, 2, ap_int<32>>("uint8", "dshift2", "int32");
    cross_ds<ap_int<4>, 3, ap_int<16>>("int4", "dshift3", "int16");
    cross_int<ap_int<2>>("int2");
    cross_int<ap_uint<4>>("uint4");
    cross_int<ap_int<8>>("int8");
    cross_int<ap_uint<16>>("uint16");
    cross_int<ap_int<32>>("int32");
    cross_int<ap_int<64>>("int64");
    cross_int<ap_fixed<8, 4>>("fixed8.4");
    cross_int<ap_fixed<16, 8>>("fixed16.8");
    cross_int<ap_ufixed<16, 8>>("ufixed16.8");
    cross_int<ap_fixed<32, 16>>("fixed32.16");
    cross_int<ap_ufixed<32, 20>>("ufixed32.12");
    std::printf("cross-checked against ap_int / ap_fixed\n");
#else
    std::printf("Vitis headers not found: ap_int / ap_fixed cross-check skipped\n");
#endif
    std::printf("%s (%d failure%s)\n", failures ? "FAILED" : "OK", failures, failures == 1 ? "" : "s");
    return failures ? 1 : 0;
}
