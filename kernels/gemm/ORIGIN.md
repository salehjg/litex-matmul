# kernels/gemm: origin and changes

Adapted from [spcl/gemm_hls](https://github.com/spcl/gemm_hls) (Johannes de Fine Licht, ETH Zurich, BSD-3-Clause,
see `LICENSE.md`). The upstream repository is kept unmodified in `submodules/gemm_hls` as a reference.

| Item | Upstream |
|---|---|
| gemm_hls | `7c790eb660aca2754ea8026fb2e911c47565ef8c` (2022-03-15) |
| hlslib (used through `submodules/gemm_hls/hlslib/include`) | `a67e02f263d666e1c7ba649c6755025b5fdd38bf` (2022-01-21) |

## File map

| Here | Upstream |
|---|---|
| `src/Top.cpp`, `src/Memory.cpp`, `src/Compute.cpp` | `kernel/` |
| `include/MatrixMultiplication.h`, `Memory.h`, `Compute.h`, `Utility.h` | `include/` |
| `include/Config.h.in` | `include/Config.h.in` (filled by CMake upstream, by `flow/gemmflow` here) |
| `tb/test_gemm.cpp` | `test/TestSimulation.cpp` |

## Changes

The first commit of this directory is a verbatim copy. Every later change is listed here (newest first) and is a
separate commit, so `git log -p kernels/gemm` shows the full diff against upstream.

- Streams for Vitis HLS 2026.1: in synthesis (`HLSLIB_SYNTHESIS`) `Stream` is an alias of `hls::stream<T, depth>`
  (`include/MatrixMultiplication.h`), because 2026.1 fails on arrays of `hlslib::Stream` objects (HLS 214-177,
  unresolved Phi node on aSplit/aPipes/bPipes). Simulation still uses `hlslib::Stream`. Call sites use `write()` /
  `read()` (both types have them) instead of hlslib's `Push()` / `Pop()`; `src/Memory.cpp` no longer has its own
  `using hlslib::Stream`.
- Interface and knobs (plan §4.1): `src/Top.cpp` uses `ap_ctrl_hs`, `m_axi offset=direct` for A/B/C (burst length,
  outstanding requests and latency from the config through `MM_PRAGMA`) and `ap_none` sizes, no AXI-Lite. Stream
  depths `kPipeDepth`, `aSplit`, `bMemory`, `cMemory` come from the config (`include/Config.h.in`: `kPipeDepthCfg`,
  `k*DepthCfg`, 0 = upstream default; `include/MatrixMultiplication.h`: `kASplitDepth`, `kBMemoryDepth`,
  `kCMemoryDepth`, `MM_PRAGMA`).
- `include/Utility.h`: no `hlslib/xilinx/OpenCL.h` (needs `CL/cl2.hpp`; there is no OpenCL host here); `Pack()` uses
  `std::vector` with the default allocator instead of hlslib's 4 KB `AlignedAllocator`.
