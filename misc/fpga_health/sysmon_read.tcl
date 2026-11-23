# sysmon_read.tcl - read the FPGA's XADC (die temperature, supply voltages) over JTAG, N samples.
# Works with any design loaded (JTAG access to the XADC), does not reconfigure the FPGA.
#   vivado -mode batch -nojournal -nolog -source sysmon_read.tcl -tclargs <samples> <interval_s>
set samples  [expr {[llength $argv] > 0 ? [lindex $argv 0] : 1}]
set interval [expr {[llength $argv] > 1 ? [lindex $argv 1] : 5}]
open_hw_manager
connect_hw_server -allow_non_jtag
open_hw_target [lindex [get_hw_targets] 0]
set dev [lindex [get_hw_devices] 0]
refresh_hw_device -update_hw_probes false $dev
set sm [lindex [get_hw_sysmons -of_objects $dev] 0]
for {set i 0} {$i < $samples} {incr i} {
    refresh_hw_sysmon $sm
    puts [format "SYSMON %s temp_C=%.1f vccint_V=%.3f vccaux_V=%.3f vccbram_V=%.3f" \
        [clock format [clock seconds] -format %H:%M:%S] \
        [get_property TEMPERATURE $sm] [get_property VCCINT $sm] [get_property VCCAUX $sm] [get_property VCCBRAM $sm]]
    flush stdout
    if {$i < $samples - 1} { after [expr {int($interval * 1000)}] }
}
close_hw_target
disconnect_hw_server
