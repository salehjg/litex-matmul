open_checkpoint [lindex $argv 0]
set c [get_cells -hier -filter {NAME =~ *storage_43*}]
puts "N=[llength $c]"
foreach x [lrange [lsort -dictionary $c] 0 30] { puts "C $x [get_property REF_NAME $x]" }
set c2 [get_cells -hier -filter {NAME =~ *rdata_cdc*}]
puts "N2=[llength $c2]"
foreach x [lrange [lsort -dictionary $c2] 0 30] { puts "R $x [get_property REF_NAME $x]" }
