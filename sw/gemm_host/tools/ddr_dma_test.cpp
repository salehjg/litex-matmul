// ddr_dma_test.cpp - write random data to DDR3 over PCIe, read it back, compare, report GB/s. GemmSoC (phase 2).
//
//   ddr_dma_test [--channel 0|1|both] [--size-mb N] [--addr BYTES] [--seed S] [--repeat R] [--json]
//
// Exit status 0 only if every byte matched on every channel and repetition.
#include <algorithm>
#include <chrono>
#include <cinttypes>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <random>
#include <string>
#include <vector>

#include "card.hpp"
#include "ddr_dma.hpp"

extern "C" {
#include "csr.h"
}

using namespace gemm;

namespace {

struct Options {
    std::vector<unsigned> channels = {0, 1};
    size_t   size  = 64ull << 20;
    uint64_t addr  = 0;
    uint64_t seed  = 1;
    unsigned repeat = 1;
    bool     json  = false;
    bool     counter = false;     // --pattern counter: word i = (channel << 56) | (rep << 48) | i
    bool     read_twice = false;  // --read-twice: read back twice (C2H stability)
    bool     read_only_bist = false;  // --read-only-bist: only read; check the LiteDRAM BIST counter pattern
    bool     c2h_pattern = false;     // --c2h-pattern: debug build only, C2H from the bridge's pattern generator
};

[[noreturn]] void usage(const char *argv0) {
    std::fprintf(stderr, "usage: %s [--channel 0|1|both] [--size-mb N] [--addr BYTES] [--seed S] [--repeat R] [--json]\n"
                         "          [--pattern random|counter] [--read-twice]\n", argv0);
    std::exit(2);
}

Options parse(int argc, char **argv) {
    Options o;
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        auto next = [&]() -> std::string { if (i + 1 >= argc) usage(argv[0]); return argv[++i]; };
        if (a == "--channel") {
            std::string v = next();
            o.channels = (v == "both") ? std::vector<unsigned>{0, 1} : std::vector<unsigned>{unsigned(std::stoul(v))};
        } else if (a == "--size-mb") o.size = std::stoull(next()) << 20;
        else if (a == "--addr")      o.addr = std::stoull(next(), nullptr, 0);
        else if (a == "--seed")      o.seed = std::stoull(next());
        else if (a == "--repeat")    o.repeat = unsigned(std::stoul(next()));
        else if (a == "--json")      o.json = true;
        else if (a == "--pattern")   { std::string v = next(); if (v == "counter") o.counter = true; else if (v != "random") usage(argv[0]); }
        else if (a == "--read-twice") o.read_twice = true;
        else if (a == "--read-only-bist") o.read_only_bist = true;
        else if (a == "--c2h-pattern") o.c2h_pattern = true;
        else usage(argv[0]);
    }
    return o;
}

// On a mismatch: show the first words, and where read-back words occur in the written data (shift/permutation).
void diagnose(const std::vector<uint64_t> &src, const std::vector<uint64_t> &dst) {
    std::printf("  first words  wrote:");
    for (size_t i = 0; i < 4 && i < src.size(); i++) std::printf(" %016" PRIx64, src[i]);
    std::printf("\n               read: ");
    for (size_t i = 0; i < 4 && i < dst.size(); i++) std::printf(" %016" PRIx64, dst[i]);
    std::printf("\n");
    auto find = [](const std::vector<uint64_t> &v, uint64_t x) -> long {
        for (size_t i = 0; i < v.size(); i++) if (v[i] == x) return long(i);
        return -1;
    };
    for (size_t i : {size_t(0), size_t(1), size_t(2), size_t(3), size_t(4), size_t(7), size_t(8), size_t(9),
                     size_t(1024), size_t(1025)}) {
        if (i >= dst.size()) break;
        const long at = find(src, dst[i]);
        std::printf("  read word %5zu = %016" PRIx64 " -> written at word %ld\n", i, dst[i], at);
    }
    const long src0 = find(dst, src[0]);
    std::printf("  written word 0 -> read at word %ld\n", src0);
    // Counter pattern: each word carries its written index in the low 48 bits -> local shift histogram.
    if ((src[0] & 0xffffffffffffull) == 0 && src.size() > 1 && (src[1] & 0xffffffffffffull) == 1) {
        std::map<long, size_t> shifts;
        std::map<size_t, size_t> bad_pos;  // position of mismatching words within an 8 KB DMA buffer (in 256 B units)
        long expect = long(dst[0] & 0xffffffffffffull);  // shift of word 0
        size_t runs = 0;
        long prev = 0;
        for (size_t i = 0; i < dst.size(); i++) {
            long sh = long(dst[i] & 0xffffffffffffull) - long(i);
            if ((dst[i] >> 48) != (src[0] >> 48) || sh < -100000 || sh > 100000) sh = 999999;  // foreign word
            shifts[sh]++;
            if (sh != expect) bad_pos[(i * 8 % 8192) / 256]++;
            if (i && sh != prev) runs++;
            prev = sh;
        }
        std::printf("  shift histogram (read index - written index, 999999 = foreign word):");
        for (auto &kv : shifts) std::printf(" %ld:%zu", kv.first, kv.second);
        size_t first_dev = 0;
        while (first_dev < dst.size() && long(dst[first_dev] & 0xffffffffffffull) == long(first_dev) + expect) first_dev++;
        const size_t from = first_dev >= 16 ? (first_dev - 16) & ~size_t(7) : 0;
        std::printf("\n  first deviation at read word %zu (byte 0x%zx); written index of read words %zu.. "
                    "(8 per 64-byte DDR word, '.' = expected i+shift0):\n", first_dev, first_dev * 8, from);
        for (size_t i = from; i < from + 96 && i < dst.size(); i++) {
            long idx = long(dst[i] & 0xffffffffffffull);
            if (i % 8 == 0) std::printf("   %4zu:", i);
            if (idx == long(i) + expect) std::printf("      .");
            else std::printf(" %6ld", idx);
            if (i % 8 == 7) std::printf("\n");
        }
        std::printf("  shift changes along the buffer: %zu\n  mismatches by 256 B slot in the 8 KB buffer:", runs);
        for (auto &kv : bad_pos) std::printf(" %zu:%zu", kv.first, kv.second);
        std::printf("\n");
    }
    size_t zeros = 0;
    for (uint64_t w : dst) zeros += (w == 0);
    std::printf("  read words that are 0: %zu of %zu\n", zeros, dst.size());
}

// LiteDRAM BIST generator with random=0 writes DDR word k (from its base) as the 31-bit counter k replicated across
// the whole word (bit j = bit (j % 31) of k). Read `size` bytes and check that pattern; report which 8-byte positions
// of the 64-byte DDR word are wrong.
int check_bist_counter(DdrDma &ddr, unsigned ch, uint64_t addr, size_t size) {
    std::vector<uint8_t> buf(size), buf2(size);
    const TransferStats r = ddr.read(ch, addr, buf.data(), size);
    ddr.read(ch, addr, buf2.data(), size);
    size_t diff8 = 0, first_diff = SIZE_MAX;
    for (size_t i = 0; i < size; i += 8)
        if (std::memcmp(&buf[i], &buf2[i], 8)) { diff8++; if (first_diff == SIZE_MAX) first_diff = i; }
    std::printf("  two reads of the same (static) DDR content differ in %zu of %zu 8-byte words (first at byte 0x%zx)\n",
                diff8, size / 8, first_diff == SIZE_MAX ? 0 : first_diff);
    size_t bad_words = 0;
    size_t bad_by_pos[8] = {0};
    size_t first_bad = SIZE_MAX;
    for (size_t k = 0; k < size / 64; k++) {
        bool bad = false;
        for (int q = 0; q < 8; q++) {  // 8-byte position inside the DDR word
            uint64_t exp = 0, got;
            for (int b = 0; b < 64; b++) {
                const int j = q * 64 + b;
                exp |= uint64_t((k >> (j % 31)) & 1) << b;
            }
            std::memcpy(&got, &buf[k * 64 + q * 8], 8);
            if (got != exp) { bad_by_pos[q]++; bad = true; }
        }
        if (bad) { bad_words++; if (first_bad == SIZE_MAX) first_bad = k; }
    }
    for (size_t k = 0; k < 4; k++) {
        std::printf("  read DDR word %zu:", k);
        for (int q = 0; q < 8; q++) { uint64_t w; std::memcpy(&w, &buf[k * 64 + q * 8], 8); std::printf(" %016" PRIx64, w); }
        uint64_t e0 = 0;
        for (int b = 0; b < 64; b++) e0 |= uint64_t((k >> (b % 31)) & 1) << b;
        std::printf("\n      expected q0: %016" PRIx64 "\n", e0);
    }
    std::printf("ch%u: C2H of BIST counter pattern, %zu DDR words, C2H %.3f GB/s: %zu bad words", ch, size / 64,
                r.gbps(), bad_words);
    if (bad_words) std::printf(" (first at word %zu)", first_bad);
    std::printf("\n  bad by 8-byte position in the 64-byte word:");
    for (int q = 0; q < 8; q++) std::printf(" %d:%zu", q, bad_by_pos[q]);
    std::printf("\n");
    return bad_words == 0 ? 0 : 1;
}

#ifdef CSR_DDR_DMA_DBG_PATTERN_ADDR
// Debug build: print the bridge's protocol watcher counters.
void print_watchers(Card &card) {
    struct { const char *name; uint32_t addr; } w[] = {
        {"rdata_overflow", CSR_DDR_DMA_DBG_RDATA_OVERFLOW_ADDR},
        {"reader_change", CSR_DDR_DMA_DBG_READER_CHANGE_ADDR}, {"reader_vdrop", CSR_DDR_DMA_DBG_READER_VDROP_ADDR},
        {"reader_beats", CSR_DDR_DMA_DBG_READER_BEATS_ADDR},
        {"down_change", CSR_DDR_DMA_DBG_DOWN_CHANGE_ADDR}, {"down_vdrop", CSR_DDR_DMA_DBG_DOWN_VDROP_ADDR},
        {"down_beats", CSR_DDR_DMA_DBG_DOWN_BEATS_ADDR},
        {"sink_change", CSR_DDR_DMA_DBG_SINK_CHANGE_ADDR}, {"sink_vdrop", CSR_DDR_DMA_DBG_SINK_VDROP_ADDR},
        {"sink_beats", CSR_DDR_DMA_DBG_SINK_BEATS_ADDR},
    };
    std::printf("  watchers:");
    for (auto &x : w) std::printf(" %s=%u", x.name, card.csr_read(x.addr));
    std::printf("\n");
}

// Debug build: C2H from the pattern generator (beat k = k in every 32-bit lane), straight into the PCIe DMA writer.
int c2h_pattern(Card &card, size_t size) {
    const size_t buffers = std::max<size_t>(2, (size + Card::kBufferBytes - 1) / Card::kBufferBytes);
    const size_t bytes   = std::min(buffers * Card::kBufferBytes, Card::kRingBytes);
    card.csr_write(CSR_DDR_DMA_DBG_CLEAR_ADDR, 1);
    card.csr_write(CSR_DDR_DMA_DBG_PATTERN_ADDR, 0);
    card.dma_writer_enable(true);
    card.csr_write(CSR_DDR_DMA_DBG_PATTERN_LEN_ADDR, uint32_t(bytes));
    card.csr_write(CSR_DDR_DMA_DBG_PATTERN_ADDR, 1);
    const auto t0 = std::chrono::steady_clock::now();
    while (card.c2h_buffers_done_hint() < bytes / Card::kBufferBytes) {
        if (std::chrono::steady_clock::now() - t0 > std::chrono::seconds(2)) {
            std::printf("C2H pattern: timeout (%" PRIu64 " buffers done)\n", card.c2h_buffers_done_hint());
            break;
        }
    }
    std::vector<uint32_t> buf(bytes / 4);
    std::memcpy(buf.data(), card.c2h_ring(), bytes);
    card.csr_write(CSR_DDR_DMA_DBG_PATTERN_ADDR, 0);
    card.dma_writer_enable(false);
    size_t bad = 0, first = SIZE_MAX;
    for (size_t k = 0; k < bytes / 16; k++)
        for (int l = 0; l < 4; l++)
            if (buf[4 * k + l] != uint32_t(k)) { bad++; if (first == SIZE_MAX) first = k; }
    std::printf("C2H pattern: %zu bytes, %zu bad 32-bit lanes", bytes, bad);
    if (bad)
        std::printf(" (first at beat %zu: %08x %08x %08x %08x)", first, buf[4 * first], buf[4 * first + 1],
                    buf[4 * first + 2], buf[4 * first + 3]);
    std::printf("\n");
    print_watchers(card);
    return bad ? 1 : 0;
}
#endif

}  // namespace

int main(int argc, char **argv) {
    const Options opt = parse(argc, argv);
    try {
        Card   card;
        DdrDma ddr(card);
        std::printf("card: %s\n", card.identifier().c_str());

        if (opt.c2h_pattern) {
#ifdef CSR_DDR_DMA_DBG_PATTERN_ADDR
            return c2h_pattern(card, opt.size);
#else
            std::fprintf(stderr, "--c2h-pattern needs a debug build (gemm_soc.py --ddr-dma-debug)\n");
            return 2;
#endif
        }
        if (opt.read_only_bist) {
            int rc = 0;
            for (unsigned ch : opt.channels) rc |= check_bist_counter(ddr, ch, opt.addr, opt.size);
            return rc;
        }
        std::vector<uint64_t> src(opt.size / 8), dst(opt.size / 8);
        bool all_ok = true;
        for (unsigned rep = 0; rep < opt.repeat; rep++) {
            for (unsigned ch : opt.channels) {
                std::mt19937_64 rng(opt.seed + 1000 * rep + ch);
                for (size_t i = 0; i < src.size(); i++)
                    src[i] = opt.counter ? ((uint64_t(ch) << 56) | (uint64_t(rep) << 48) | i) : rng();
                std::memset(dst.data(), 0, opt.size);

                const TransferStats w = ddr.write(ch, opt.addr, src.data(), opt.size);
                const TransferStats r = ddr.read(ch, opt.addr, dst.data(), opt.size);
                if (opt.read_twice) {
                    std::vector<uint64_t> dst2(dst.size());
                    for (int k = 0; k < 3; k++) {
                        ddr.read(ch, opt.addr, dst2.data(), opt.size);
                        size_t diff = 0;
                        for (size_t i = 0; i < dst.size(); i++) diff += (dst[i] != dst2[i]);
                        // With the counter pattern the low bits give the written index directly.
                        std::printf("  extra read %d: differs from the first in %zu words; word0 %016" PRIx64
                                    " word1 %016" PRIx64 " last %016" PRIx64 "\n",
                                    k, diff, dst2[0], dst2[1], dst2.back());
                    }
                }

                size_t bad = 0, first_bad = SIZE_MAX;
                for (size_t i = 0; i < src.size(); i++)
                    if (src[i] != dst[i]) { if (first_bad == SIZE_MAX) first_bad = i; bad++; }
                const bool ok = bad == 0;
                all_ok &= ok;

                std::printf("ch%u rep%u: %zu MB @ 0x%" PRIx64 "  H2C %.3f GB/s (%.1f ms)  C2H %.3f GB/s (%.1f ms)  %s",
                            ch, rep, opt.size >> 20, opt.addr, w.gbps(), w.seconds * 1e3, r.gbps(), r.seconds * 1e3,
                            ok ? "OK" : "MISMATCH");
                if (!ok)
                    std::printf(" (%zu bad 64-bit words, first at byte 0x%zx: wrote %016" PRIx64 " read %016" PRIx64 ")",
                                bad, first_bad * 8, src[first_bad], dst[first_bad]);
                std::printf("\n");
                if (!ok) diagnose(src, dst);
#ifdef CSR_DDR_DMA_DBG_PATTERN_ADDR
                print_watchers(card);
#endif
                if (opt.json)
                    std::printf("{\"test\":\"ddr_dma\",\"channel\":%u,\"rep\":%u,\"bytes\":%zu,\"addr\":%" PRIu64
                                ",\"seed\":%" PRIu64 ",\"h2c_s\":%.6f,\"h2c_gbps\":%.4f,\"c2h_s\":%.6f,\"c2h_gbps\":%.4f,"
                                "\"bad_words\":%zu,\"ok\":%s}\n",
                                ch, rep, opt.size, opt.addr, opt.seed, w.seconds, w.gbps(), r.seconds, r.gbps(), bad,
                                ok ? "true" : "false");
            }
        }
        return all_ok ? 0 : 1;
    } catch (const std::exception &e) {
        std::fprintf(stderr, "error: %s\n", e.what());
        return 3;
    }
}
