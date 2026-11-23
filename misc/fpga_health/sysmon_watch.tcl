# sysmon_watch.tcl - watch the FPGA's XADC over JTAG; if the die gets too hot, reboot the FPGA from its flash
# image (IPROG via boot_hw_device), which takes our design off the card.
#   vivado -mode batch -nojournal -nolog -source sysmon_watch.tcl -tclargs <samples> <interval_s> <limit_C>
set samples  [lindex $argv 0]
set interval [lindex $argv 1]
set limit    [lindex $argv 2]
open_hw_manager
connect_hw_server -allow_non_jtag
open_hw_target [lindex [get_hw_targets] 0]
set dev [lindex [get_hw_devices] 0]
refresh_hw_device -update_hw_probes false $dev
set sm [lindex [get_hw_sysmons -of_objects $dev] 0]
for {set i 0} {$i < $samples} {incr i} {
    refresh_hw_sysmon $sm
    set t [get_property TEMPERATURE $sm]
    puts [format "SYSMON %s temp_C=%.1f vccint_V=%.3f vccaux_V=%.3f vccbram_V=%.3f" \
        [clock format [clock seconds] -format %H:%M:%S] $t \
        [get_property VCCINT $sm] [get_property VCCAUX $sm] [get_property VCCBRAM $sm]]
    flush stdout
    if {$t > $limit} {
        puts "SYSMON LIMIT: $t C > $limit C, rebooting the FPGA from flash"
        boot_hw_device $dev
        break
    }
    if {$i < $samples - 1} { after [expr {int($interval * 1000)}] }
}
close_hw_target
disconnect_hw_server
