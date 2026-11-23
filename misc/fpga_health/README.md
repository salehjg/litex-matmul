# misc/fpga_health: FPGA die temperature and supplies over JTAG

Reads the Kintex-7 XADC through JTAG (Vivado hw_server), with any design loaded and without reconfiguring the FPGA.
Used to rule out overheating / supply problems while chasing the host freezes (phase 1, see
[docs/notes1/gemm-hls-progress.md](../../docs/notes1/gemm-hls-progress.md)).

```
source scripts/env.sh
vivado -mode batch -nojournal -nolog -source misc/fpga_health/sysmon_read.tcl  -tclargs 3 5        # 3 samples, 5 s apart
vivado -mode batch -nojournal -nolog -source misc/fpga_health/sysmon_watch.tcl -tclargs 27 20 85   # 9 min, limit 85 C
```
`sysmon_watch.tcl` reboots the FPGA from its flash image (`boot_hw_device`) if the die passes the limit, which takes
our design off the card (and drops the PCIe link). Logs go to `logs/` (git-ignored) when you `tee` them there.

Reference values on unit 0265 in this PC (2026-11-23): flash image 47 C; LiteX base SoC (no DMA) 39 C, flat over
9 min; VCCINT 0.957 V, VCCAUX 1.80-1.81 V, VCCBRAM 0.957 V.
