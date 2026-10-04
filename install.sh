#!/usr/bin/env bash
# Install a self-contained DKMS source snapshot and enable boot loading.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
cd -- "$(dirname -- "$(realpath -- "$0")")"
package=schenker-kbd
version=0.1.0
source_dir=/usr/src/$package-$version
options_file=/etc/modprobe.d/schenker-kbd.conf
boot_file=/etc/modules-load.d/schenker-kbd.conf
helper=/usr/local/bin/schenker-backlight
action=${1:-install}
if [[ $# -gt 1 || ( $action != install && $action != remove ) ]]; then
    echo "Usage: $0 [install|remove]" >&2
    exit 2
fi
if [[ $EUID != 0 ]]; then
    echo "Run with sudo; DKMS installation requires administrator access." >&2
    exit 1
fi

if [[ $action == remove ]]; then
    # Leave the installed setup intact if the module cannot be unloaded.
    if [[ -d /sys/module/schenker_kbd ]]; then modprobe -r schenker_kbd; fi
    rm -f -- "$boot_file" "$options_file"
    if [[ -d /var/lib/dkms/$package/$version ]]; then
        dkms remove -m "$package" -v "$version" --all
    fi
    rm -f -- "$helper"
    # Delete only the known source snapshot files, not arbitrary directory contents.
    if [[ -d $source_dir ]]; then
        rm -f -- "$source_dir/Makefile" "$source_dir/dkms.conf" "$source_dir/schenker_kbd.c"
        rmdir -- "$source_dir"
    fi
    echo "Removed DKMS module and boot configuration. Fn+Space remains available."
    exit 0
fi

[[ $(cat /sys/class/dmi/id/sys_vendor) == SchenkerTechnologiesGmbH &&
   $(cat /sys/class/dmi/id/product_name) == 'SCHENKER VISION (E22)' &&
   $(cat /sys/class/dmi/id/board_name) == PHxARX1_PHxAQF1 ]] || {
    echo "This installer is restricted to the tested laptop model." >&2; exit 1;
}
command -v dkms >/dev/null
[[ -f /lib/modules/$(uname -r)/build/Makefile ]] || {
    echo "Install headers for the running kernel first." >&2; exit 1;
}
for spec in "schenker-kbd.modprobe.conf:$options_file" \
            "schenker-kbd.modules-load.conf:$boot_file" "backlight:$helper"; do
    local_file=${spec%%:*}
    destination=${spec#*:}
    if [[ -e $destination ]] && ! cmp -s -- "$local_file" "$destination"; then
        echo "Refusing to overwrite differing file: $destination" >&2
        exit 1
    fi
done

if [[ -e $source_dir ]]; then
    for file in Makefile dkms.conf schenker_kbd.c; do
        cmp -s -- "$file" "$source_dir/$file" || {
            echo "Installed source differs: bump the DKMS version before upgrading." >&2
            exit 1
        }
    done
else
    install -d -m 0755 "$source_dir"
    install -m 0644 Makefile dkms.conf schenker_kbd.c "$source_dir/"
fi
if [[ ! -d /var/lib/dkms/$package/$version ]]; then
    dkms add -m "$package" -v "$version"
fi

# Old, removed kernels can leave directories in /lib/modules. Only build for
# kernels with both an installed image and prepared headers.
for build in /lib/modules/*/build; do
    [[ -f $build/Makefile ]] || continue
    kernel=${build%/build}
    kernel=${kernel##*/}
    [[ -f /boot/vmlinuz-$kernel ]] || continue
    dkms install -m "$package" -v "$version" -k "$kernel"
done

# Load the installed binary before making boot loading persistent.
if [[ -d /sys/module/schenker_kbd ]]; then modprobe -r schenker_kbd; fi
modprobe schenker_kbd writable=1
[[ -r /sys/class/leds/schenker:white:kbd_backlight/brightness ]] || {
    echo "Installed module did not expose the LED; boot loading was not enabled." >&2
    modprobe -r schenker_kbd
    exit 1
}
install -m 0644 schenker-kbd.modprobe.conf "$options_file"
install -m 0644 schenker-kbd.modules-load.conf "$boot_file"
install -m 0755 backlight "$helper"
dkms status -m "$package" -v "$version"
echo "Installed. Boot loading and DKMS kernel-update rebuilds are enabled."
