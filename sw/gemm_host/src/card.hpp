// card.hpp - the LitePCIe card from user space: CSRs (driver ioctl) and the two DMA rings (zero-copy mmap).
//
// Built per SoC build: CSR addresses come from that build's generated csr.h (see Makefile).
#pragma once

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>

extern "C" {
#include "liblitepcie.h"
}

namespace gemm {

class CardError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

class Card {
public:
    static constexpr size_t kBufferBytes = DMA_BUFFER_SIZE;   // One DMA descriptor / ring buffer.
    static constexpr size_t kBufferCount = DMA_BUFFER_COUNT;  // Ring length.
    static constexpr size_t kRingBytes   = DMA_BUFFER_TOTAL_SIZE;

    explicit Card(const std::string &device = "/dev/litepcie0");
    ~Card();
    Card(const Card &) = delete;
    Card &operator=(const Card &) = delete;

    uint32_t csr_read(uint32_t addr) const;
    void     csr_write(uint32_t addr, uint32_t value) const;
    std::string identifier() const;

    // Zero-copy rings. h2c: host -> card (PCIe DMA reader), write-only mapping. c2h: card -> host (PCIe DMA writer).
    char       *h2c_ring() { return dma_.buf_wr; }
    const char *c2h_ring() const { return dma_.buf_rd; }

    // Enabling a direction (re)programs its descriptor table from buffer 0 in loop mode (driver behaviour).
    void dma_reader_enable(bool enable);  // host -> card
    void dma_writer_enable(bool enable);  // card -> host

    // Number of card -> host descriptors retired since dma_writer_enable(true), from the hardware loop status.
    // Valid once at least one descriptor is retired (the status reads (0, 0) before and after the first one).
    uint64_t c2h_buffers_done_hint() const;

private:
    struct litepcie_dma_ctrl dma_ {};
};

}  // namespace gemm
