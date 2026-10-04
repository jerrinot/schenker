# SCHENKER VISION E22 keyboard backlight

Linux driver for the SCHENKER VISION E22 with board `PHxARX1_PHxAQF1`.
Adds software brightness control while preserving Fn+Space.

## Install

Requires DKMS, a C compiler, and kernel headers. Keep `linux-headers-generic`
installed so Ubuntu supplies headers for kernel updates.

```sh
sudo ./install.sh
```

Loads at boot and rebuilds through DKMS when kernels are updated. The installed
source is independent of this checkout.

## Use

No sudo required:

```sh
schenker-backlight get
schenker-backlight off
schenker-backlight dim
schenker-backlight bright
```

Numeric levels `0`, `1`, and `2` also work. The helper uses UPower; the kernel
interface is `/sys/class/leds/schenker:white:kbd_backlight`.

## Check or remove

```sh
dkms status -m schenker-kbd
sudo ./install.sh remove
```

Removal preserves the current brightness and Fn+Space control.

## Limitations

Tested on Ubuntu kernels `7.0.0-29` and `7.0.0-31`, including automatic rebuilds
and the boot-loading mechanism. Actual reboot and suspend/resume remain untested.

- Future kernel API changes may require driver updates despite DKMS.
- Secure Boot requires enrollment of the DKMS signing certificate.
- Fn+Space changes are readable through sysfs, but desktop change notifications
  are not implemented.

Based on the [Linux Uniwill driver](https://github.com/torvalds/linux/blob/master/drivers/platform/x86/uniwill/uniwill-acpi.c)
and [TUXEDO's backlight implementation](https://github.com/tuxedocomputers/tuxedo-drivers/blob/main/src/uniwill_leds.h).
