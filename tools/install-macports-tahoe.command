#!/bin/zsh
set -euo pipefail

# Prepared official installer; this updates MacPorts base, not installed ports.
task_root="${0:A:h:h}"
task_pkg="$task_root/docs/macports-recovery-2026-10-19/MacPorts-2.12.7-26-Tahoe.pkg"
task_expected="588f14ccd3c6199559a070a80e6cddd6871721a08cc434775bf54378fd667910"
case "$(/usr/bin/sw_vers -productVersion)" in
  26.*) ;;
  *) print -u2 'This installer is prepared for macOS 26 only.'; exit 1 ;;
esac
task_actual="$(/usr/bin/shasum -a 256 "$task_pkg")"
[[ "${task_actual%% *}" == "$task_expected" ]] || { print -u2 'Installer checksum mismatch'; exit 1; }
/usr/sbin/pkgutil --check-signature "$task_pkg"
task_stage="$(/usr/bin/mktemp -d /private/tmp/brew-macports-XXXXXXXX)"
/bin/chmod 755 "$task_stage"
/bin/cp "$task_pkg" "$task_stage/MacPorts.pkg"
/bin/chmod 644 "$task_stage/MacPorts.pkg"
print 'Installing the verified MacPorts 2.12.7 base for macOS Tahoe.'
print 'macOS requires administrator authentication for /opt/local.'
sudo /usr/sbin/installer -pkg "$task_stage/MacPorts.pkg" -target /
/opt/local/bin/port version
/opt/local/bin/port installed
print 'MacPorts base installation finished. No port upgrade or migration was requested.'
