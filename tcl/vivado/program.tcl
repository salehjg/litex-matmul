# program.tcl - load a bitstream into the FPGA over JTAG (volatile), used by flow/gemmflow/board.py.
#   vivado -mode batch -nojournal -nolog -source tcl/vivado/program.tcl -tclargs <file.bit>
# After loading, the card drops off the PCIe bus: rescan (scripts/pcie_rescan.sh).

set bit [lindex $argv 0]
open_hw_manager
connect_hw_server -allow_non_jtag
open_hw_target [lindex [get_hw_targets] 0]
set dev [lindex [get_hw_devices] 0]
current_hw_device $dev
refresh_hw_device -update_hw_probes false $dev
set_property PROBES.FILE {} $dev
set_property PROGRAM.FILE $bit $dev
program_hw_devices $dev
refresh_hw_device -update_hw_probes false $dev
puts "PROGRAM_DONE_PIN=[get_property REGISTER.CONFIG_STATUS.BIT14_DONE_PIN $dev]"
close_hw_target
disconnect_hw_server
