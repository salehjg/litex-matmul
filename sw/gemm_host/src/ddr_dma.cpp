// ddr_dma.cpp - see ddr_dma.hpp.
#include "ddr_dma.hpp"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <string>

extern "C" {
#include "csr.h"
}

namespace gemm {

namespace {

using Clock = std::chrono::steady_clock;

double since(Clock::time_point t0) { return std::chrono::duration<double>(Clock::now() - t0).count(); }

struct ChannelCsrs {
    uint32_t wr_base, wr_length, wr_enable, rd_base, rd_length, rd_enable, rd_done;
};

const ChannelCsrs kCsrs[DdrDma::kChannels] = {
    {CSR_DDR_DMA_CH0_WRITER_BASE_ADDR, CSR_DDR_DMA_CH0_WRITER_LENGTH_ADDR, CSR_DDR_DMA_CH0_WRITER_ENABLE_ADDR,
     CSR_DDR_DMA_CH0_READER_BASE_ADDR, CSR_DDR_DMA_CH0_READER_LENGTH_ADDR, CSR_DDR_DMA_CH0_READER_ENABLE_ADDR,
     CSR_DDR_DMA_CH0_READER_DONE_ADDR},
    {CSR_DDR_DMA_CH1_WRITER_BASE_ADDR, CSR_DDR_DMA_CH1_WRITER_LENGTH_ADDR, CSR_DDR_DMA_CH1_WRITER_ENABLE_ADDR,
     CSR_DDR_DMA_CH1_READER_BASE_ADDR, CSR_DDR_DMA_CH1_READER_LENGTH_ADDR, CSR_DDR_DMA_CH1_READER_ENABLE_ADDR,
     CSR_DDR_DMA_CH1_READER_DONE_ADDR},
};

}  // namespace

void DdrDma::check(unsigned channel, uint64_t ddr_addr, size_t length) const {
    if (channel >= kChannels) throw CardError("DDR channel must be 0 or 1");
    if (ddr_addr % kWordBytes || length % kWordBytes)
        throw CardError("DDR address and length must be multiples of 64 B");
    if (ddr_addr + length > kChannelBytes) throw CardError("transfer past the end of the 2 GB channel");
}

TransferStats DdrDma::write(unsigned channel, uint64_t ddr_addr, const void *src, size_t length) {
    check(channel, ddr_addr, length);
    TransferStats st;
    const auto t0 = Clock::now();
    const char *p = static_cast<const char *>(src);
    for (size_t done = 0; done < length;) {
        const size_t n = std::min(length - done, Card::kRingBytes);
        write_chunk(channel, ddr_addr + done, p + done, n, st);
        done += n;
    }
    st.bytes   = length;
    st.seconds = since(t0);
    return st;
}

TransferStats DdrDma::read(unsigned channel, uint64_t ddr_addr, void *dst, size_t length) {
    check(channel, ddr_addr, length);
    TransferStats st;
    const auto t0 = Clock::now();
    char *p = static_cast<char *>(dst);
    for (size_t done = 0; done < length;) {
        const size_t n = std::min(length - done, Card::kRingBytes);
        read_chunk(channel, ddr_addr + done, p + done, n, st);
        done += n;
    }
    st.bytes   = length;
    st.seconds = since(t0);
    return st;
}

void DdrDma::write_chunk(unsigned channel, uint64_t ddr_addr, const char *src, size_t length, TransferStats &st) {
    const ChannelCsrs &c = kCsrs[channel];
    st.chunks++;

    // 1. Whole payload in the ring before the hardware may read it.
    auto tp = Clock::now();
    std::memcpy(card_.h2c_ring(), src, length);
    st.copy_s += since(tp);
    tp = Clock::now();

    // 2. Bridge writer ready first, so the first beat it sees is ring buffer 0.
    card_.csr_write(CSR_DDR_DMA_SEL_ADDR, channel);
    card_.csr_write(c.wr_enable, 0);
    card_.csr_write(c.wr_base, static_cast<uint32_t>(ddr_addr));
    card_.csr_write(c.wr_length, static_cast<uint32_t>(length));
    card_.csr_write(c.wr_enable, 1);

    // 3. Stream the ring; wait until all `length` bytes are in the DDR controller.
    card_.dma_reader_enable(true);
    st.start_s += since(tp);
    const auto t0 = Clock::now();
    while (!((card_.csr_read(CSR_DDR_DMA_DRAINED_ADDR) >> channel) & 1)) {
        if (since(t0) > timeout_s_) {
            card_.dma_reader_enable(false);
            card_.csr_write(c.wr_enable, 0);
            throw CardError("H2C timeout: channel " + std::to_string(channel) + " not drained");
        }
    }
    st.wait_s += since(t0);
    tp = Clock::now();
    card_.dma_reader_enable(false);
    card_.csr_write(c.wr_enable, 0);
    st.stop_s += since(tp);
}

void DdrDma::read_chunk(unsigned channel, uint64_t ddr_addr, char *dst, size_t length, TransferStats &st) {
    const ChannelCsrs &c = kCsrs[channel];
    st.chunks++;

    // Whole DMA buffers, at least 2 (the loop status is ambiguous for 1). Extra bytes are read and not copied.
    size_t buffers = (length + Card::kBufferBytes - 1) / Card::kBufferBytes;
    buffers        = std::max<size_t>(buffers, 2);
    const size_t dma_bytes = buffers * Card::kBufferBytes;
    if (ddr_addr + dma_bytes > kChannelBytes) throw CardError("C2H padding would read past the end of the channel");

    // 1. PCIe DMA writer first (table reset, waits for data), then the bridge reader.
    auto tp = Clock::now();
    card_.dma_writer_enable(true);
    card_.csr_write(CSR_DDR_DMA_SEL_ADDR, channel);
    card_.csr_write(c.rd_enable, 0);
    card_.csr_write(c.rd_base, static_cast<uint32_t>(ddr_addr));
    card_.csr_write(c.rd_length, static_cast<uint32_t>(dma_bytes));
    card_.csr_write(c.rd_enable, 1);
    st.start_s += since(tp);

    // 2. Wait until the bridge has sent everything and the last descriptor is retired.
    const auto t0 = Clock::now();
    while (!(card_.csr_read(c.rd_done) & 1) || card_.c2h_buffers_done_hint() < buffers) {
        if (since(t0) > timeout_s_) {
            card_.csr_write(c.rd_enable, 0);
            card_.dma_writer_enable(false);
            throw CardError("C2H timeout: channel " + std::to_string(channel));
        }
    }

    st.wait_s += since(t0);

    // 3. Copy out, stop.
    tp = Clock::now();
    std::memcpy(dst, card_.c2h_ring(), length);
    st.copy_s += since(tp);
    tp = Clock::now();
    card_.csr_write(c.rd_enable, 0);
    card_.dma_writer_enable(false);
    st.stop_s += since(tp);
}

}  // namespace gemm
