// ddr_dma.hpp - host <-> DDR3 transfers through the GemmSoC bridge (hw/gemm/ddr_dma.py), in ring-sized chunks.
//
// H2C (write): fill the whole zero-copy ring first, start the bridge writer, then enable the PCIe DMA reader. The
//   driver runs the reader in loop mode, so the hardware streams the ring over and over; the bridge takes exactly
//   `length` bytes and drops the rest. Done when the channel's `drained` bit is set.
// C2H (read): enable the PCIe DMA writer, start the bridge reader, wait until the hardware loop status shows all
//   buffers retired (that CSR read also flushes the card's posted writes ahead of it), copy out of the ring.
// Addresses and lengths: multiples of 64 B (DDR word). C2H reads whole 8 KB buffers, at least 2 per chunk.
#pragma once

#include <cstddef>
#include <cstdint>

#include "card.hpp"

namespace gemm {

struct TransferStats {
    size_t bytes   = 0;
    double seconds = 0.0;  // Wall time of the whole call (copies + DMA + polling).
    // Where the time goes, summed over chunks: ring copy, DMA/bridge start (driver ioctl + CSR writes), waiting for
    // the data, stop (driver ioctl + CSR writes).
    double copy_s = 0.0, start_s = 0.0, wait_s = 0.0, stop_s = 0.0;
    unsigned chunks = 0;
    double gbps() const { return seconds > 0 ? bytes / seconds / 1e9 : 0.0; }  // GB/s
};

class DdrDma {
public:
    static constexpr unsigned kChannels   = 2;
    static constexpr uint64_t kChannelBytes = 2ull << 30;  // 2 GB per DDR3 channel.
    static constexpr size_t   kWordBytes  = 64;            // DDR native word.

    explicit DdrDma(Card &card, double timeout_s = 2.0) : card_(card), timeout_s_(timeout_s) {}

    TransferStats write(unsigned channel, uint64_t ddr_addr, const void *src, size_t length);
    TransferStats read(unsigned channel, uint64_t ddr_addr, void *dst, size_t length);

private:
    void write_chunk(unsigned channel, uint64_t ddr_addr, const char *src, size_t length, TransferStats &st);
    void read_chunk(unsigned channel, uint64_t ddr_addr, char *dst, size_t length, TransferStats &st);
    void check(unsigned channel, uint64_t ddr_addr, size_t length) const;

    Card  &card_;
    double timeout_s_;
};

}  // namespace gemm
