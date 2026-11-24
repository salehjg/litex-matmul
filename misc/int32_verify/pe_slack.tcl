# pe_slack.tcl - per PE, the worst setup slack of paths into the aBuffer RAM (write data/address/enable) and out of
# it (read data to the multipliers), from a routed checkpoint.
#   vivado -mode batch -source pe_slack.tcl -tclargs <route.dcp> <out.txt> [num_pes]
lassign $argv dcp out npe
if {$npe eq ""} { set npe 32 }
open_checkpoint $dcp
set f [open $out w]
for {set p 0} {$p < $npe} {incr p} {
    set ram [get_cells -quiet -hier -filter "NAME =~ */ProcessingElement_${p}_U0/aBuffer_U/* && REF_NAME =~ RAMB*"]
    if {![llength $ram]} { continue }
    set into [get_timing_paths -quiet -to [get_pins -of_objects $ram -filter {DIRECTION == IN && IS_CLOCK == 0}] -max_paths 1 -setup]
    set from [get_timing_paths -quiet -from $ram -max_paths 1 -setup]
    set si [expr {[llength $into] ? [format %.3f [get_property SLACK $into]] : "-"}]
    set so [expr {[llength $from] ? [format %.3f [get_property SLACK $from]] : "-"}]
    set ep [expr {[llength $from] ? [get_property NAME [get_property ENDPOINT_PIN $from]] : "-"}]
    puts $f "PE $p into $si from $so to $ep"
}
close $f
