# cdc_delays.tcl - routed delays into the read register of a LiteDRAM native-port read-data CDC FIFO.
#   vivado -mode batch -source cdc_delays.tcl -tclargs <route.dcp> <storage name, e.g. storage_43> <out.txt>
# Writes one line per register bit: bit, net delay RAM->D (slow max, ns), driver pin, and the timing-report data
# path delay (or "-" when Vivado does not time the path: asynchronous clock groups).
lassign $argv dcp st out
open_checkpoint $dcp
set f [open $out w]
set regs [lsort -dictionary [get_cells -quiet ${st}_dat1_reg*]]
puts $f "# [llength $regs] register bits of ${st}_dat1"
foreach r $regs {
    set d [get_pins -quiet $r/D]
    set net [get_nets -quiet -of_objects $d]
    set drv [get_pins -quiet -leaf -of_objects $net -filter {DIRECTION == OUT}]
    set nd [get_net_delays -quiet -of_objects $net -to $d]
    set slow [expr {[llength $nd] ? [get_property SLOW_MAX [lindex $nd 0]] : "-"}]
    set p [get_timing_paths -quiet -to $d -max_paths 1 -delay_type max]
    set dp [expr {[llength $p] ? [get_property DATAPATH_DELAY $p] : "-"}]
    set sl [expr {[llength $p] ? [get_property SLACK $p] : "-"}]
    set sc [expr {[llength $p] ? [get_property STARTPOINT_CLOCK $p] : "-"}]
    puts $f "$r $slow $drv dp=$dp slack=$sl from=$sc"
}
close $f
