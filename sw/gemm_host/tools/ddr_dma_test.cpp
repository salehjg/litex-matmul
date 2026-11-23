// ddr_dma_test.cpp - write random data to DDR3 over PCIe, read it back, compare, report GB/s. GemmSoC (phase 2).
//
//   ddr_dma_test [--channel 0|1|both] [--size-mb N] [--addr BYTES] [--seed S] [--repeat R] [--json]
//
// Exit status 0 only if every byte matched on every channel and repetition.
#include <chrono>
#include <cinttypes>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <string>
#include <vector>

#include "card.hpp"
#include "ddr_dma.hpp"

using namespace gemm;

namespace {

struct Options {
    std::vector<unsigned> channels = {0, 1};
    size_t   size  = 64ull << 20;
    uint64_t addr  = 0;
    uint64_t seed  = 1;
    unsigned repeat = 1;
    bool     json  = false;
};

[[noreturn]] void usage(const char *argv0) {
    std::fprintf(stderr, "usage: %s [--channel 0|1|both] [--size-mb N] [--addr BYTES] [--seed S] [--repeat R] [--json]\n",
                 argv0);
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
        else usage(argv[0]);
    }
    return o;
}

}  // namespace

int main(int argc, char **argv) {
    const Options opt = parse(argc, argv);
    try {
        Card   card;
        DdrDma ddr(card);
        std::printf("card: %s\n", card.identifier().c_str());

        std::vector<uint64_t> src(opt.size / 8), dst(opt.size / 8);
        bool all_ok = true;
        for (unsigned rep = 0; rep < opt.repeat; rep++) {
            for (unsigned ch : opt.channels) {
                std::mt19937_64 rng(opt.seed + 1000 * rep + ch);
                for (auto &w : src) w = rng();
                std::memset(dst.data(), 0, opt.size);

                const TransferStats w = ddr.write(ch, opt.addr, src.data(), opt.size);
                const TransferStats r = ddr.read(ch, opt.addr, dst.data(), opt.size);

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
