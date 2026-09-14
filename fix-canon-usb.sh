#!/usr/bin/env bash
# Run as your desktop login user on the host, not inside Docker or with sudo.
set -euo pipefail

if [[ "${1:-}" == "--undo" && $# == 1 ]]; then
    systemctl --user unmask gvfs-gphoto2-volume-monitor.service
    systemctl --user start gvfs-gphoto2-volume-monitor.service
    echo "Desktop photo-camera mounting restored. It may compete with time-lapse capture."
    exit 0
fi
if (( $# )); then
    echo "Usage: $0 [--undo]" >&2
    exit 2
fi

# GIO lists each camera twice (a shadow mount and the real mount); unmount once.
# Querying mounts may warn if the monitor was already masked, which is expected.
mounts=$(LC_ALL=C gio mount -l 2>/dev/null)
declare -A released=()
while IFS= read -r line; do
    if [[ "$line" =~ \-\>\ (gphoto2://Canon[^[:space:]]*) ]]; then
        uri=${BASH_REMATCH[1]}
        if [[ -z "${released[$uri]:-}" ]]; then
            gio mount -u "$uri"
            released["$uri"]=1
            echo "Released $uri"
        fi
    fi
done <<< "$mounts"

# Prevent D-Bus activation from restarting the monitor after reconnect or login.
systemctl --user mask --now gvfs-gphoto2-volume-monitor.service
echo "Desktop photo-camera auto-mounting disabled for this login (persists after reboot)."
echo "Running projects retry automatically. For an old USB-address selection, Refresh cameras and Save settings once to enable serial-number reconnect recovery."
