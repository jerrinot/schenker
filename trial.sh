#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(realpath -- "$0")")"
device=/sys/bus/platform/devices/INOU0000:00
led=/sys/class/leds/schenker:white:kbd_backlight
action=${1:-status}

case "$action" in
    status)
        cat "$device/raw_status"
        if [[ -d "$led" ]]; then
            cat "$led/brightness" "$led/max_brightness"
        fi
        exit 0
        ;;
    probe|enable|disable) ;;
    *) echo "Usage: $0 {probe|enable|disable|status}" >&2; exit 2 ;;
esac
if [[ $EUID != 0 ]]; then
    echo "Run this action with sudo (kernel module loading requires root)." >&2
    exit 1
fi

if [[ $action == disable ]]; then
    /usr/sbin/rmmod schenker_kbd
    echo "Driver unloaded; current brightness retained."
    exit 0
fi
if [[ $action == enable && -e /sys/module/schenker_kbd &&
      $(cat /sys/module/schenker_kbd/parameters/writable) == N &&
      $(readlink "$device/driver") == */schenker-kbd ]]; then
    /usr/sbin/rmmod schenker_kbd
fi
if [[ -e /sys/module/schenker_kbd || -L "$device/driver" ]]; then
    echo "Device or trial module already in use; inspect before loading." >&2
    exit 1
fi
if [[ $(/usr/sbin/modinfo -F vermagic ./schenker_kbd.ko) != "$(uname -r) "* ]]; then
    echo "Module was built for a different kernel; run make again." >&2
    exit 1
fi

loaded=0
cleanup() {
    if [[ $loaded == 1 ]]; then /usr/sbin/rmmod schenker_kbd; fi
}
trap cleanup EXIT
mode=0
if [[ $action == enable ]]; then mode=1; fi
/usr/sbin/insmod ./schenker_kbd.ko writable="$mode"
loaded=1
if [[ ! -r "$device/raw_status" ]]; then
    echo "Driver probe failed; inspect journalctl -k -n 30." >&2
    exit 1
fi
cat "$device/raw_status"
if [[ $action == enable && ! -d "$led" ]]; then
    echo "LED registration failed." >&2
    exit 1
fi
loaded=0
echo "Driver left loaded for testing; unload with: sudo $PWD/trial.sh disable"
