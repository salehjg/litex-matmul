// card.cpp - see card.hpp.
#include "card.hpp"

#include <cstring>

extern "C" {
#include "csr.h"
}

namespace gemm {

Card::Card(const std::string &device) {
    dma_.use_reader = 1;
    dma_.use_writer = 1;
    dma_.loopback   = 0;  // The DDR bridge sits on the external side of the DMA.
    if (litepcie_dma_init(&dma_, device.c_str(), /*zero_copy=*/1) != 0)
        throw CardError("cannot open " + device + " (driver loaded? DMA already in use?)");
    // Both directions start disabled.
    dma_reader_enable(false);
    dma_writer_enable(false);
}

Card::~Card() {
    litepcie_dma_cleanup(&dma_);  // Disables both directions, releases the DMA, unmaps, closes.
}

uint32_t Card::csr_read(uint32_t addr) const { return litepcie_readl(dma_.fds.fd, addr); }

void Card::csr_write(uint32_t addr, uint32_t value) const { litepcie_writel(dma_.fds.fd, addr, value); }

std::string Card::identifier() const {
    std::string id;
    for (unsigned i = 0; i < 256; i++) {
        char c = static_cast<char>(csr_read(CSR_IDENTIFIER_MEM_BASE + 4 * i) & 0xff);
        if (c == 0) break;
        id += c;
    }
    return id;
}

void Card::dma_reader_enable(bool enable) {
    dma_.reader_enable = enable;
    litepcie_dma_reader(dma_.fds.fd, enable, &dma_.reader_hw_count, &dma_.reader_sw_count);
}

void Card::dma_writer_enable(bool enable) {
    dma_.writer_enable = enable;
    litepcie_dma_writer(dma_.fds.fd, enable, &dma_.writer_hw_count, &dma_.writer_sw_count);
}

uint64_t Card::c2h_buffers_done_hint() const {
    const uint32_t status = csr_read(CSR_PCIE_DMA0_WRITER_TABLE_LOOP_STATUS_ADDR);
    const uint64_t index  = status & 0xffff;
    const uint64_t count  = status >> 16;
    return count * kBufferCount + index + 1;
}

}  // namespace gemm
