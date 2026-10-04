# SCHENKER VISION E22 keyboard backlight

A model-specific Linux LED driver, installed with DKMS for automatic rebuilding
when Ubuntu installs a new kernel. It exposes three brightness levels while
preserving the firmware's Fn+Space control.

Supported identity (all three fields must match):

- Vendor: `SchenkerTechnologiesGmbH`
- Product: `SCHENKER VISION (E22)`
- Board: `PHxARX1_PHxAQF1`

## Use

After installation, from any directory and without sudo:

```sh
schenker-backlight get
schenker-backlight off
schenker-backlight dim
schenker-backlight bright
```

Numeric levels `0`, `1`, and `2` are also accepted. The helper uses UPower for
writes and reads actual controller brightness through sysfs. The LED is exposed
at `/sys/class/leds/schenker:white:kbd_backlight`.

## Install or reinstall

Required packages: `dkms`, a compiler, and kernel headers. This Ubuntu machine
already has `linux-generic` and `linux-headers-generic`, which keep kernel images
and headers updated together.

```sh
sudo ./install.sh
```

The installer builds for installed kernel images with available headers, loads
the installed module, verifies that its LED exists, and configures boot loading.
It installs these files:

| Location | Purpose |
| --- | --- |
| `/usr/src/schenker-kbd-0.1.0/` | Root-owned source snapshot and DKMS build configuration |
| `/var/lib/dkms/schenker-kbd/0.1.0/` | DKMS registration, build logs, and build state |
| `/lib/modules/<kernel>/updates/dkms/schenker_kbd.ko*` | Module built for each kernel |
| `/etc/modprobe.d/schenker-kbd.conf` | Enables writable LED control when the module loads |
| `/etc/modules-load.d/schenker-kbd.conf` | Loads the module at boot |
| `/usr/local/bin/schenker-backlight` | Brightness helper available on PATH |

The source snapshot is independent of this working directory. Moving or deleting
this checkout does not interrupt subsequent kernel rebuilds. Reinstalling an
identical version is supported; source changes require a new version in
`dkms.conf`, `install.sh`, and `MODULE_VERSION` in `schenker_kbd.c`.

`AUTOINSTALL="yes"` registers the module for DKMS autoinstall. Ubuntu calls DKMS
from its kernel-image and kernel-header post-installation hooks. The build uses
`${kernelver}`, the kernel being installed, rather than the currently running
kernel. systemd loads the corresponding rebuilt module on the next boot.

DKMS recompiles source; it cannot automatically repair incompatible kernel API
changes. A future incompatible kernel may require a source update. Build failures
are reported by DKMS during package installation; inspect `dkms status` and the
kernel-specific logs under `/var/lib/dkms/schenker-kbd/0.1.0/`.

Secure Boot is currently disabled. DKMS uses its normal module-signing mechanism.
If Secure Boot is enabled later, its signing certificate must be enrolled before
the kernel will accept the module.

## Check installation

```sh
dkms status -m schenker-kbd
modinfo schenker_kbd
modprobe --show-depends schenker_kbd
schenker-backlight get
cat /sys/bus/platform/devices/INOU0000:00/raw_status
```

## Disable or remove

To unload for this session only (boot configuration remains):

```sh
sudo modprobe -r schenker_kbd
```

To reload:

```sh
sudo modprobe schenker_kbd
```

To remove the module from DKMS, all its kernel installations, boot configuration,
source snapshot, and installed helper:

```sh
sudo ./install.sh remove
```

Unloading preserves the current light level. Fn+Space remains available.

## Diagnostic mode

For read-only diagnostics, unload the installed module and explicitly override
its writable option:

```sh
sudo modprobe -r schenker_kbd
sudo modprobe schenker_kbd writable=0
cat /sys/bus/platform/devices/INOU0000:00/raw_status
```

This mode does not register an LED or write to the controller. Reload without the
explicit override to restore normal operation. `trial.sh` remains available for
loading a locally built binary, but ordinary use should use the installed module.

## Hardware validation and limits

DKMS version `0.1.0` is installed for `7.0.0-29-generic` and
`7.0.0-31-generic`. To test automatic rebuilds, the older kernel's generated
module was removed and Ubuntu's actual kernel-header post-installation hook was
run; it rebuilt, signed, and reinstalled the module successfully. The current
kernel's module was then unloaded and loaded through `systemd-modules-load`
using the installed boot configuration. Writable LED control returned and level
1 was preserved. These paths were tested live; an actual reboot was not performed.

On 2026-10-04 the original diagnostic probe returned `kbd_status=0x21`,
`support2=0xa0`, and `bios_oem2=0x49`. Software control successfully wrote and read
back levels 0, 1, and 2. The user confirmed the physical backlight changed and
Fn+Space still worked. An unload/reload cycle preserved level 1. UPower discovered
the LED automatically and accepted unprivileged brightness requests.

- Exact DMI matching and exclusive binding to the `INOU0000` platform device.
- ACPI `ECRR`/`ECRW` interface; no direct EC port access.
- Only writable register: `0x078c` (keyboard brightness/status).
- Each write preserves bits 0, 2 and 3; updates brightness bits 7:5; sets apply
  bit 4; and clears power-off bit 1, following upstream's operation.
- Diagnostic reads include `0x0766` and `0x0782`; neither is written.
- No manual-control-mode, fan, thermal, battery, or power-policy writes.
- No driver-initiated controller writes at load, unload, suspend, or resume.
  Desktop software may request brightness changes once the LED is registered.
- Brightness is read from hardware on every sysfs read. WMI change notifications
  for desktop synchronization are not implemented.
- Suspend/resume remains untested. Persistent installation does not add new
  suspend/resume behavior or force a particular brightness at startup.
- This custom driver still marks the kernel as running out-of-tree code.

## Source evidence

Reviewed on 2026-10-04:

- [Linux v7.0 Uniwill driver](https://github.com/torvalds/linux/blob/v7.0/drivers/platform/x86/uniwill/uniwill-acpi.c):
  ACPI interface and transaction timing. This version lacks keyboard LED control.
- [Current upstream Uniwill driver](https://github.com/torvalds/linux/blob/master/drivers/platform/x86/uniwill/uniwill-acpi.c):
  `uniwill_kbd_led_write_brightness`, keyboard register layout, LED callbacks.
- [TUXEDO's board support patch](https://lists.openwall.net/linux-kernel/2026/08/22/31):
  this board family uses a single-color backlight with maximum brightness 2.
- [TUXEDO keyboard LED implementation](https://github.com/tuxedocomputers/tuxedo-drivers/blob/main/src/uniwill_leds.h):
  corroborates the brightness protocol and Gen7 hardware behavior.
- [Linux v7.0 LED lifecycle](https://github.com/torvalds/linux/blob/v7.0/drivers/leds/led-class.c):
  `LED_RETAIN_AT_SHUTDOWN` prevents switching the light off during removal.

Design review through the `consult-claude-design` skill supported a narrow,
read-only-first trial. Its suggestions on LED cleanup and firmware/software
read-modify-write races were considered. Its claim that mask `0xf2` preserves
bit 1 was incorrect: the mask includes that bit and upstream clears it. The
driver follows the source. Strict initial brightness validation is retained
for this experimental module rather than silently accepting unexpected values.
