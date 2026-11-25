// numeric.hpp - element formats of the GEMM kernel on the host side (docs/notes2/int-dtype-plan.md 4.2).
//
// Spec strings as in flow/gemmflow/numeric.py: float32, int8, uint16, int4, fixed16.8, ufixed8.4.
// Integer and fixed-point values are handled as raw codes (two's complement for signed types) in int64_t.
//
// Memory layout: hlslib DataPack puts element i of a 512-bit word at bits [i*W, (i+1)*W), and 512 % W == 0, so a
// matrix is one dense little-endian bit stream: element e at bit e*W (pack / unpack).
//
// Kernel arithmetic (gemm_hls: one Data_t for A, B, C and the accumulation; hlslib Product/Sum return Data_t):
//   int:   each product and each partial sum is reduced to W bits (wrap-around), i.e. C = sum(a*b) mod 2^W.
//   fixed: ap_fixed<W, W-F> with AP_TRN, AP_WRAP: product = floor(a*b / 2^F) wrapped to W bits, sums wrap.
// Both reductions are compatible with arithmetic mod 2^64, so the reference accumulates in uint64_t and wraps once.
//
// Mixed formats (kernels/gemm_mixed, docs/notes3/denseshift-plan.md): A, B and C have their own Dtype. B may be a
// DenseShift code (spec dshift<n>, n = 2, 3, 4): code = sign << (n-1) | e, value (-1)^sign * 2^e, no zero; 3-bit codes
// are stored in 4-bit lanes. The kernel computes C = sum value(a) * value(b) wrapped to C's width (ref_entry_mixed).
//
// Header-only and independent of the card, so it is unit-tested on its own (tests/test_numeric.cpp, make test).
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

namespace gemm {

struct Dtype {
    enum class Kind { Float, Int, Fixed, Pow2 };
    Kind kind = Kind::Float;
    unsigned bits = 32;
    bool is_signed = true;
    unsigned frac = 0;  // fixed point: fractional bits
    unsigned lane = 0;  // storage bits when wider than bits (3-bit DenseShift codes in 4-bit lanes); 0 = bits

    static Dtype parse(const std::string &spec) {
        Dtype d;
        std::string s = spec;
        bool u = false;
        if (!s.empty() && s[0] == 'u') { u = true; s = s.substr(1); }
        auto number = [&](const std::string &t) -> unsigned {
            if (t.empty() || t.find_first_not_of("0123456789") != std::string::npos) throw std::invalid_argument("bad dtype spec " + spec);
            return unsigned(std::stoul(t));
        };
        if (s.rfind("dshift", 0) == 0 && !u) {
            d.kind = Kind::Pow2;
            d.bits = number(s.substr(6));
            d.is_signed = false;  // raw codes are unsigned bit patterns
            if (d.bits < 2 || d.bits > 4) throw std::invalid_argument("dtype " + spec + ": DenseShift widths 2, 3, 4");
            d.lane = d.bits == 2 ? 2 : 4;
        } else if (s.rfind("float", 0) == 0 && !u) {
            d.kind = Kind::Float;
            d.bits = number(s.substr(5));
            if (d.bits != 32) throw std::invalid_argument("dtype " + spec + ": only float32 is supported on the host");
        } else if (s.rfind("fixed", 0) == 0) {
            const std::string rest = s.substr(5);
            const auto dot = rest.find('.');
            if (dot == std::string::npos) throw std::invalid_argument("bad dtype spec " + spec);
            d.kind = Kind::Fixed;
            d.bits = number(rest.substr(0, dot));
            d.frac = number(rest.substr(dot + 1));
            d.is_signed = !u;
            if (d.bits != 8 && d.bits != 16 && d.bits != 32) throw std::invalid_argument("dtype " + spec + ": fixed widths 8, 16, 32");
            if (d.frac > d.bits - (d.is_signed ? 1 : 0)) throw std::invalid_argument("dtype " + spec + ": too many fractional bits");
        } else if (s.rfind("int", 0) == 0) {
            d.kind = Kind::Int;
            d.bits = number(s.substr(3));
            d.is_signed = !u;
            if (d.bits != 2 && d.bits != 4 && d.bits != 8 && d.bits != 16 && d.bits != 32 && d.bits != 64)
                throw std::invalid_argument("dtype " + spec + ": int widths 2, 4, 8, 16, 32, 64");
        } else {
            throw std::invalid_argument("bad dtype spec " + spec);
        }
        return d;
    }

    std::string spec() const {
        if (kind == Kind::Pow2) return "dshift" + std::to_string(bits);
        if (kind == Kind::Float) return "float" + std::to_string(bits);
        const std::string u = is_signed ? "" : "u";
        if (kind == Kind::Fixed) return u + "fixed" + std::to_string(bits) + "." + std::to_string(frac);
        return u + "int" + std::to_string(bits);
    }
    const char *kind_name() const {
        return kind == Kind::Float ? "float" : kind == Kind::Int ? "int" : kind == Kind::Pow2 ? "pow2" : "fixed";
    }
    bool is_float() const { return kind == Kind::Float; }
    bool is_pow2() const { return kind == Kind::Pow2; }
    unsigned storage() const { return lane ? lane : bits; }
    unsigned elems_per_word() const { return 512 / storage(); }
    size_t bytes(size_t count) const { return (count * storage() + 7) / 8; }

    // Value of a code in the kernel's arithmetic (integers and DenseShift; fixed point uses product()).
    int64_t value(int64_t code) const {
        if (kind != Kind::Pow2) return code;
        const unsigned e = unsigned(code) & ((1u << (bits - 1)) - 1);
        const int64_t mag = int64_t(1) << e;
        return ((code >> (bits - 1)) & 1) ? -mag : mag;
    }
    // DenseShift code of (-1)^negative * 2^e.
    int64_t pow2_code(bool negative, unsigned e) const { return (int64_t(negative) << (bits - 1)) | int64_t(e); }

    int64_t min_code() const { return is_signed ? (bits == 64 ? INT64_MIN : -(int64_t(1) << (bits - 1))) : 0; }
    int64_t max_code() const {
        if (bits == 64) return is_signed ? INT64_MAX : -1;  // uint64: all ones (handled as raw bits)
        return is_signed ? (int64_t(1) << (bits - 1)) - 1 : (int64_t(1) << bits) - 1;
    }

    // Reduce a value mod 2^bits into the code range (sign-extended for signed types).
    int64_t wrap(uint64_t x) const {
        if (bits == 64) return int64_t(x);  // (DenseShift: the low n bits are the code)
        const uint64_t mask = (uint64_t(1) << bits) - 1;
        uint64_t m = x & mask;
        if (is_signed && (m >> (bits - 1))) m |= ~mask;
        return int64_t(m);
    }

    // One product as the kernel stores it, before the final wrap (exact mod 2^64).
    uint64_t product(int64_t a, int64_t b) const {
        if (kind == Kind::Fixed) {
            if (!is_signed) return (uint64_t(a) * uint64_t(b)) >> frac;  // codes < 2^32: product < 2^64, exact
            const int64_t p = a * b;  // |a|, |b| <= 2^31: |p| <= 2^62, exact
            return uint64_t(floor_shift(p, frac));
        }
        return uint64_t(a) * uint64_t(b);  // mod 2^64, well defined
    }

    static int64_t floor_shift(int64_t p, unsigned f) {
        if (f == 0) return p;
        if (p >= 0) return p >> f;
        return -int64_t((uint64_t(-p) + (uint64_t(1) << f) - 1) >> f);
    }
};

// Dense little-endian bit packing (element e at bit e*storage bits).
inline void pack(const Dtype &d, const int64_t *codes, size_t count, uint8_t *out) {
    const unsigned w = d.storage();
    if (w % 8 == 0) {
        const unsigned nb = w / 8;
        for (size_t e = 0; e < count; e++) {
            const uint64_t v = uint64_t(codes[e]);
            for (unsigned b = 0; b < nb; b++) out[e * nb + b] = uint8_t(v >> (8 * b));
        }
        return;
    }
    std::memset(out, 0, d.bytes(count));
    const uint64_t mask = (uint64_t(1) << w) - 1;
    for (size_t e = 0; e < count; e++) {
        const size_t bit = e * w;
        out[bit / 8] |= uint8_t((uint64_t(codes[e]) & mask) << (bit % 8));  // w in {2, 4}: never crosses a byte
    }
}

inline void unpack(const Dtype &d, const uint8_t *in, size_t count, int64_t *codes) {
    const unsigned w = d.storage();
    if (w % 8 == 0) {
        const unsigned nb = w / 8;
        for (size_t e = 0; e < count; e++) {
            uint64_t v = 0;
            for (unsigned b = 0; b < nb; b++) v |= uint64_t(in[e * nb + b]) << (8 * b);
            codes[e] = d.wrap(v);
        }
        return;
    }
    for (size_t e = 0; e < count; e++) {
        const size_t bit = e * w;
        codes[e] = d.wrap(uint64_t(in[bit / 8] >> (bit % 8)));
    }
}

// Reference C[i][j] = sum_k A[i][k] * B[k][j] in the kernel's arithmetic. bt is B transposed (M x K).
inline int64_t ref_entry(const Dtype &d, const int64_t *a_row, const int64_t *bt_row, uint32_t k) {
    uint64_t acc = 0;
    for (uint32_t x = 0; x < k; x++) acc += d.product(a_row[x], bt_row[x]);
    return d.wrap(acc);
}

// Mixed formats: C[i][j] = sum_k value_A(a) * value_B(b), wrapped once to C's width (the kernel wraps every step:
// the same result mod 2^bits). Values are below 2^16 * 2^7, so int64 products are exact; the sum wraps mod 2^64.
inline int64_t ref_entry_mixed(const Dtype &a, const Dtype &b, const Dtype &c, const int64_t *a_row,
                               const int64_t *bt_row, uint32_t k) {
    uint64_t acc = 0;
    for (uint32_t x = 0; x < k; x++) acc += uint64_t(a.value(a_row[x]) * b.value(bt_row[x]));
    return c.wrap(acc);
}

inline std::string mixed_spec(const Dtype &a, const Dtype &b, const Dtype &c) {
    return a.spec() + "x" + b.spec() + "-" + c.spec();
}

}  // namespace gemm
