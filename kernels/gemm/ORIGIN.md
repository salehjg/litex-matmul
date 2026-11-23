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

- `include/Utility.h`: no `hlslib/xilinx/OpenCL.h` (needs `CL/cl2.hpp`; there is no OpenCL host here); `Pack()` uses
  `std::vector` with the default allocator instead of hlslib's 4 KB `AlignedAllocator`.
