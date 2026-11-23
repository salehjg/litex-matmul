# import_hls_ip.tcl - add the exported HLS GEMM kernel IP to the current Vivado project (LiteX pre-synthesis hook).
# Inputs, set by hw/gemm/gemm_soc.py right before sourcing this file:
#   ::P(hls_ip_repo)    directory with component.xml (HLS export_design -format ip_catalog: <hls>/prj/sol/impl/ip)
#   ::P(hls_ip_vlnv)    e.g. litex-matmul:hls:gemm_kernel:1.0
#   ::P(hls_ip_module)  module name the SoC instantiates (hw/gemm/hls_kernel.py), e.g. gemm_kernel_ip
# The IP is synthesized out of context (synth_ip), including the floating-point subcores HLS generated.

set_property ip_repo_paths [list $::P(hls_ip_repo)] [current_project]
update_ip_catalog
create_ip -vlnv $::P(hls_ip_vlnv) -module_name $::P(hls_ip_module)
generate_target all [get_ips $::P(hls_ip_module)]
synth_ip [get_ips $::P(hls_ip_module)]
