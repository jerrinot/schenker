#!/usr/bin/env bash
# Keep administrator edits; update files that still match the last shipped default.
install_config() {
    local source=$1 destination=$2 baseline=$3 old_options=""
    if [[ ${source##*/} == schenker-kbd.modprobe.conf && -f $destination && ! -f $baseline ]]; then
        old_options=$(sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' "$destination")
    fi
    if [[ ! -e $destination ]] || cmp -s -- "$source" "$destination" ||
       { [[ -f $baseline ]] && cmp -s -- "$baseline" "$destination"; } ||
       [[ $old_options == 'options schenker_kbd writable=1' ||
          $old_options == 'options schenker_kbd writable=1 usb_c_policy=1 fan_policy=0 firmware_curve=1' ]]; then
        install -D -m 0644 "$source" "$destination"
    else
        install -D -m 0644 "$source" "$destination.dist"
        echo "Preserved $destination; new default: $destination.dist" >&2
    fi
    install -D -m 0644 "$source" "$baseline"
}

loaded_version() {
    if [[ -r /sys/module/schenker_kbd/version ]]; then
        cat /sys/module/schenker_kbd/version
    fi
}

validate_loaded() {
    [[ $(loaded_version) == "$version" ]] || {
        echo "Expected loaded driver $version; inspect journalctl -k." >&2
        return 1
    }
    local parameters=/sys/module/schenker_kbd/parameters
    if [[ $(cat "$parameters/writable") == Y ]]; then
        [[ -r /sys/class/leds/schenker:white:kbd_backlight/brightness ]] || return 1
    fi
    if [[ $(cat "$parameters/firmware_curve") == Y ]]; then
        grep -q 'active=1 error=0' /sys/devices/platform/INOU0000:00/fan_curve_status || return 1
    fi
    if [[ $(cat "$parameters/usb_c_policy") == Y ]]; then
        [[ -r /sys/devices/platform/INOU0000:00/usb_c_power_priority ]] || return 1
    fi
}

save_deployment() {
    backup=$(mktemp -d /var/tmp/schenker-rollback.XXXXXX)
    chmod 0700 "$backup"
    mkdir "$backup/files"
    saved_paths=("$options_file" "$boot_file" "$helper" "$policy" "$power_helper" "$unit")
    local name path
    for name in charging-profile.conf firmware-profile.conf; do
        saved_paths+=("$dropins/$name")
    done
    for path in "$options_file" "$boot_file" "$dropins/charging-profile.conf" "$dropins/firmware-profile.conf"; do
        saved_paths+=("$path.dist")
    done
    for name in modprobe.conf modules-load.conf charging-profile.conf firmware-profile.conf; do
        saved_paths+=("$config_state/$name")
    done
    for path in "${saved_paths[@]}"; do
        if [[ -e $path || -L $path ]]; then
            mkdir -p -- "$backup/files${path%/*}"
            cp -a -- "$path" "$backup/files$path"
        fi
    done
    printf '%s\n' "$previous_running" > "$backup/running-version"
    printf '%s\n' "${previous_parameters[@]}" > "$backup/module-parameters"
    printf 'active=%s enabled=%s\n' "$was_active" "$was_enabled" > "$backup/service-state"
    for name in "${kernels[@]}"; do
        printf '%s %s\n' "$name" "${previous_versions[$name]:-none}"
    done > "$backup/kernel-versions"
}

rollback_install() {
    local failed=0 kernel path
    echo "Installation failed; restoring the previous deployment." >&2
    # Do not unload the old module again if the initial unload failed.
    if (( runtime_changed )); then
        if systemctl is-active --quiet "$service"; then
            systemctl stop "$service" || failed=1
        fi
        if [[ -d /sys/module/schenker_kbd ]]; then
            modprobe -r schenker_kbd || failed=1
        fi
    fi
    for kernel in "${touched_kernels[@]}"; do
        if [[ -n ${previous_versions[$kernel]} ]]; then
            dkms install --force -m "$package" -v "${previous_versions[$kernel]}" -k "$kernel" || failed=1
        else
            dkms remove -m "$package" -v "$version" -k "$kernel" || failed=1
        fi
    done
    # Disable a newly enabled unit while its file still exists.
    if (( ! was_enabled )) && [[ -e $unit ]]; then
        systemctl disable "$service" || failed=1
    fi
    for path in "${saved_paths[@]}"; do
        rm -f -- "$path" || failed=1
        if [[ -e $backup/files$path || -L $backup/files$path ]]; then
            mkdir -p -- "${path%/*}" || failed=1
            cp -a -- "$backup/files$path" "$path" || failed=1
        fi
    done
    systemctl daemon-reload || failed=1
    if [[ -e $unit ]]; then
        if (( was_enabled )); then
            systemctl enable "$service" || failed=1
        else
            systemctl disable "$service" || failed=1
        fi
    else
        systemctl disable "$service" 2>/dev/null || true
    fi
    if (( runtime_changed )) && [[ -n $previous_running ]]; then
        modprobe schenker_kbd "${previous_parameters[@]}" || failed=1
        [[ $(loaded_version) == "$previous_running" ]] || failed=1
    fi
    if (( was_active )); then
        systemctl start "$service" || failed=1
    fi
    update-initramfs -u -k all || failed=1
    if (( failed )); then
        echo "Rollback incomplete. Backup and previous versions: $backup" >&2
    else
        echo "Previous deployment restored. Backup: $backup" >&2
    fi
    return "$failed"
}

install_exit() {
    local status=$?
    trap - EXIT INT TERM
    if (( status != 0 )); then
        set +e
        rollback_install
    else
        rm -rf -- "$backup"
    fi
    exit "$status"
}

check_removal_version() {
    local kernel current found=0
    # Check before stopping services or touching shared files.
    for kernel in /lib/modules/*; do
        kernel=${kernel##*/}
        current=$(modinfo -k "$kernel" -F version schenker_kbd 2>/dev/null || true)
        [[ -n $current ]] || continue
        if [[ $current != "$version" ]]; then
            echo "Refusing removal: kernel $kernel has $current, checkout is $version." >&2
            return 1
        fi
        found=1
    done
    if (( ! found )) || ! dkms status -m "$package" -v "$version" | grep -q ': installed'; then
        echo "Refusing removal: checkout version $version is not installed." >&2
        return 1
    fi
    current=$(loaded_version)
    if [[ -n $current && $current != "$version" ]]; then
        echo "Refusing removal: running driver is $current, checkout is $version." >&2
        return 1
    fi
}

remove_deployment() {
    check_removal_version
    local was_active=0 file
    if systemctl is-active --quiet "$service"; then
        was_active=1
        systemctl stop "$service"
    fi
    if [[ -d /sys/module/schenker_kbd ]] && ! modprobe -r schenker_kbd; then
        if (( was_active )); then systemctl start "$service"; fi
        return 1
    fi
    dkms remove -m "$package" -v "$version" --all
    systemctl disable "$service" 2>/dev/null || true
    rm -f -- "$boot_file" "$options_file" "$helper" "$policy" "$power_helper" "$unit"
    rm -f -- "$dropins/charging-profile.conf" "$dropins/firmware-profile.conf"
    if [[ -d $dropins ]]; then rmdir --ignore-fail-on-non-empty -- "$dropins"; fi
    systemctl daemon-reload
    for file in "${source_files[@]}"; do rm -f -- "$source_dir/$file"; done
    if [[ -d $source_dir ]]; then rmdir -- "$source_dir"; fi
    update-initramfs -u -k all
    echo "Removed schenker-kbd $version and its service and boot configuration."
}

main() {
    set -euo pipefail
    export PATH=/usr/sbin:/usr/bin:/sbin:/bin
    cd -- "$(dirname -- "$(realpath -- "$0")")"
    package=schenker-kbd
    version=$(sed -n 's/^PACKAGE_VERSION="\([^"]*\)"$/\1/p' driver/dkms.conf)
    [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Invalid DKMS version" >&2; exit 1; }
    source_files=(Makefile dkms.conf schenker_kbd.c schenker_curve.h schenker_hwmon.h)
    source_dir=/usr/src/$package-$version
    options_file=/etc/modprobe.d/schenker-kbd.conf
    boot_file=/etc/modules-load.d/schenker-kbd.conf
    helper=/usr/local/bin/schenker-backlight
    service=schenker-usbc-policy.service
    unit=/etc/systemd/system/$service
    policy=/usr/local/libexec/schenker-usbc-policy.py
    power_helper=/usr/local/sbin/schenker-power
    dropins=$unit.d
    config_state=/var/lib/schenker-kbd/config-defaults
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
        remove_deployment
        return
    fi

    [[ $(cat /sys/class/dmi/id/sys_vendor) == SchenkerTechnologiesGmbH &&
       $(cat /sys/class/dmi/id/product_name) == 'SCHENKER VISION (E22)' &&
       $(cat /sys/class/dmi/id/board_name) == PHxARX1_PHxAQF1 &&
       $(cat /sys/class/dmi/id/bios_version) == N.1.05A09 ]] || {
        echo "This installer requires the tested model and BIOS N.1.05A09." >&2; exit 1;
    }
    for command in dkms python3 systemctl update-initramfs modinfo; do command -v "$command" >/dev/null; done
    running_kernel=$(uname -r)
    [[ -f /lib/modules/$running_kernel/build/Makefile ]] || {
        echo "Install headers for the running kernel first." >&2; exit 1;
    }
    declare -gA previous_versions=()
    kernels=()
    for build in /lib/modules/*/build; do
        [[ -f $build/Makefile ]] || continue
        kernel=${build%/build}; kernel=${kernel##*/}
        [[ -f /boot/vmlinuz-$kernel ]] || continue
        kernels+=("$kernel")
        previous_versions[$kernel]=$(modinfo -k "$kernel" -F version schenker_kbd 2>/dev/null || true)
        if [[ -n ${previous_versions[$kernel]} &&
              ! -d /var/lib/dkms/$package/${previous_versions[$kernel]} ]]; then
            echo "Cannot restore unmanaged driver ${previous_versions[$kernel]} for $kernel." >&2
            exit 1
        fi
    done
    [[ -v previous_versions[$running_kernel] ]] || { echo "Running kernel image is missing." >&2; exit 1; }
    previous_running=$(loaded_version)
    if [[ -n $previous_running && $previous_running != "${previous_versions[$running_kernel]}" ]]; then
        echo "Running and installed driver versions differ; resolve this before upgrading." >&2
        exit 1
    fi
    previous_parameters=()
    if [[ -n $previous_running ]]; then
        for parameter in /sys/module/schenker_kbd/parameters/*; do
            previous_parameters+=("${parameter##*/}=$(cat "$parameter")")
        done
    fi
    was_active=0; was_enabled=0
    if systemctl is-active --quiet "$service"; then was_active=1; fi
    if systemctl is-enabled --quiet "$service"; then was_enabled=1; fi
    if [[ -e $source_dir ]]; then
        for file in "${source_files[@]}"; do
            cmp -s -- "driver/$file" "$source_dir/$file" || {
                echo "Installed source differs: bump the DKMS version before upgrading." >&2; exit 1;
            }
        done
    else
        install -d -m 0755 "$source_dir"
        install -m 0644 "${source_files[@]/#/driver/}" "$source_dir/"
    fi
    if [[ ! -d /var/lib/dkms/$package/$version ]]; then dkms add -m "$package" -v "$version"; fi
    # Build every kernel before changing the installed modules or active service.
    for kernel in "${kernels[@]}"; do dkms build -m "$package" -v "$version" -k "$kernel"; done
    save_deployment
    touched_kernels=(); runtime_changed=0
    trap install_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    for kernel in "${kernels[@]}"; do
        touched_kernels+=("$kernel")
        dkms install -m "$package" -v "$version" -k "$kernel"
    done
    install_config packaging/schenker-kbd.modprobe.conf "$options_file" "$config_state/modprobe.conf"
    install_config packaging/schenker-kbd.modules-load.conf "$boot_file" "$config_state/modules-load.conf"
    if (( was_active )); then systemctl stop "$service"; fi
    if [[ -d /sys/module/schenker_kbd ]]; then modprobe -r schenker_kbd; fi
    runtime_changed=1
    # modprobe reads the preserved configuration; no command-line overrides.
    modprobe schenker_kbd
    validate_loaded
    install -D -m 0755 userspace/backlight "$helper"
    install -D -m 0755 userspace/schenker-usbc-policy.py "$policy"
    install -D -m 0755 userspace/schenker-power "$power_helper"
    install -D -m 0644 "packaging/systemd/$service" "$unit"
    for config in "packaging/systemd/$service.d/"*.conf; do
        name=${config##*/}
        install_config "$config" "$dropins/$name" "$config_state/$name"
    done
    systemctl daemon-reload
    if [[ $(cat /sys/module/schenker_kbd/parameters/usb_c_policy) == Y ]]; then
        systemctl enable "$service"
        systemctl restart "$service"
    else
        systemctl stop "$service"
        systemctl disable "$service"
    fi
    update-initramfs -u -k all
    dkms status -m "$package" -v "$version"
    echo "Installed schenker-kbd $version with the selected module configuration."
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
