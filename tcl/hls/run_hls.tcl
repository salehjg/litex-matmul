# run_hls.tcl - Vitis HLS flow for the GEMM kernel: (csim) -> csynth -> export as an IP-catalog IP.
#   cd <hls build dir> && vitis-run --mode hls --tcl <repo>/tcl/hls/run_hls.tcl
# All inputs come from params.tcl in the current directory (written by flow/gemmflow, decision D7):
#   ::P(project) ::P(top) ::P(part) ::P(clock_period_ns) ::P(clock_uncertainty) ::P(cflags) ::P(sources)
#   ::P(testbench) ::P(run_csim) ::P(csim_argv) ::P(export_flow) ::P(ip_vendor) ::P(ip_library) ::P(ip_name)
#   ::P(ip_version)

source [file join [pwd] params.tcl]

open_project -reset $::P(project)
set_top $::P(top)
foreach f $::P(sources) {
    add_files -cflags $::P(cflags) $f
}
add_files -tb -cflags $::P(cflags) $::P(testbench)

open_solution -reset -flow_target vivado sol
set_part $::P(part)
create_clock -period $::P(clock_period_ns) -name default
set_clock_uncertainty $::P(clock_uncertainty)

# 32-bit m_axi addresses (each DDR3 channel is 2 GB), dataflow checks as in upstream gemm_hls.
config_interface -m_axi_addr64=false
config_dataflow -strict_mode warning

if {$::P(run_csim)} {
    csim_design -argv $::P(csim_argv)
}

csynth_design

set export_args [list -format ip_catalog -rtl verilog \
    -vendor $::P(ip_vendor) -library $::P(ip_library) -ipname $::P(ip_name) -version $::P(ip_version)]
if {$::P(export_flow) ne "none"} {
    lappend export_args -flow $::P(export_flow)
}
export_design {*}$export_args

exit
