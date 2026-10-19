#!/opt/local/libexec/macports/bin/tclsh8.6
# Metadata only. No mportexec, fetch, archivefetch, install, or privileged changes.
# Usage: resolve.tcl OUTPUT_DIR PORT ... ; emits Tcl-list records to stdout.
if {[llength $argv] < 2} {error {usage: resolve.tcl OUTPUT_DIR PORT ...}}
set output_dir [file normalize [lindex $argv 0]]
set roots [lrange $argv 1 end]
file mkdir $output_dir
set nodes_fd [open [file join $output_dir nodes.tsv] w]
set roots_fd [open [file join $output_dir root-nodes.tsv] w]
set results_fd [open [file join $output_dir root-results.tsv] w]
puts $nodes_fd [join {name version revision variants arch archive requires porturl} \t]
puts $roots_fd [join {root name archive} \t]
puts $results_fd [join {root status error} \t]
set emitted {}
package require macports
set scratch [file normalize [file join $output_dir scratch]]
file mkdir $scratch
file mkdir [file join $scratch db registry]
set sources [open [file join $scratch sources.conf] w]
puts $sources {file:///opt/local/var/macports/sources/rsync.macports.org/macports/release/tarballs/ports [default,nosync]}
close $sources
set config [open [file join $scratch macports.conf] w]
puts $config "portdbpath $scratch/db"
puts $config "sources_conf $scratch/sources.conf"
puts $config {buildfromsource never}
close $config
set env(PORTSRC) [file join $scratch macports.conf]
array set ui_options {ports_quiet yes ports_no_old_index_warning yes}
array set global_options {ports_binary_only yes ports_no_migration_check yes}
array set global_variations {}
mportinit ui_options global_options global_variations
puts [list HOST $macports::os_platform $macports::os_major $macports::build_arch]
# mportdepends passes the installation hint to dependency mportopen calls. That
# hint normally starts asynchronous remote existence probes. Disable only that
# speculative prefetch; dependency resolution and variant evaluation stay intact.
rename mportopen mportopen_original
proc mportopen {url {options {}} {variations {}} {nocache {}}} {
    dict unset options mport_hint_install
    return [mportopen_original $url $options $variations $nocache]
}
proc emit_metadata {root mport} {
    global emitted nodes_fd roots_fd
    set worker [ditem_key $mport workername]
    set info [mportinfo $mport]
    set name [dict get $info name]
    set archive [$worker eval {get_portimage_name}]
    puts $roots_fd [join [list $root $name $archive] \t]
    if {[dict exists $emitted $archive]} {return}
    dict set emitted $archive 1
    set fields [list $name \
        [$worker eval {set version}] [$worker eval {set revision}] \
        [$worker eval {set portvariants}] \
        [join [$worker eval {get_canonical_archs}] ,] \
        $archive [join [ditem_key $mport requires] ,] [ditem_key $mport porturl]]
    puts [list PORT {*}$fields]
    puts $nodes_fd [join $fields \t]
}
foreach name $roots {
    puts [list ROOT $name]
    if {[catch {
        lassign [mportlookup $name] resolved info
        set options [dict create subport $resolved]
        set root [mportopen [dict get $info porturl] $options [array get global_variations]]
        set deps {}
        set status [mportdepends $root install 1 0 0 deps]
        puts [list RESOLUTION_STATUS $name $status]
        puts $results_fd [join [list $name $status {}] \t]
        emit_metadata $name $root
        foreach dep [lsort -unique $deps] {
            if {[ditem_key $dep] ne {}} {emit_metadata $name $dep}
        }
        # Keep workers open for official mportopen's options/variants cache.
    } message options]} {
        puts [list ERROR $name $message]
        puts $results_fd [join [list $name error [string map {\t { } \n { }} $message]] \t]
        puts stderr [dict get $options -errorinfo]
    }
}
close $nodes_fd
close $roots_fd
close $results_fd
puts stderr [list SCRATCH $scratch]

mportshutdown
file delete -force $scratch
