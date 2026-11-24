open_checkpoint [lindex $argv 0]
set c [get_cells -hier -filter {NAME =~ *ProcessingElement_12_U0/*aBuffer*}]
puts "N=[llength $c]"
foreach x [lrange $c 0 20] { puts "C $x [get_property REF_NAME $x] [get_property LOC $x]" }
