# abuf_sites.tcl - placement of each PE's aBuffer (and cBuffer) RAM in a routed checkpoint.
#   vivado -mode batch -source abuf_sites.tcl -tclargs <route.dcp> <out.txt>
lassign $argv dcp out
open_checkpoint $dcp
set f [open $out w]
for {set p 0} {$p < 64} {incr p} {
    set a [get_cells -quiet -hier -filter "NAME =~ */ProcessingElement_${p}_U0/aBuffer_U/* && REF_NAME =~ RAMB*"]
    if {![llength $a]} { set a [get_cells -quiet -hier -filter "NAME =~ */ProcessingElement_${p}_U0/aBuffer_U/* && REF_NAME =~ RAM*"] }
    if {![llength $a]} { continue }
    set locs {}
    foreach c $a { lappend locs "[get_property REF_NAME $c]@[get_property LOC $c]" }
    puts $f "PE $p aBuffer [lsort -unique $locs]"
}
close $f
