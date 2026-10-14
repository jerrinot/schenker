# Development

```sh
make                       # running kernel
make KDIR=/lib/modules/7.0.0-34-generic/build
make test                  # hardware-independent tests
make check-installed       # compare checkout with deployment
make clean
```

`driver/` is the self-contained DKMS source tree. The installer copies its build
inputs into `/usr/src/schenker-kbd-VERSION`. Edit this checkout and bump the version
in `driver/dkms.conf` and `driver/schenker_kbd.c` before installing changed driver
code. Userspace checks the USB-C interface and lease setting, not release numbers.

## Installation and upgrades

`sudo ./install.sh` builds for installed kernels with headers, loads the module,
installs helpers and systemd configuration, then refreshes initramfs images.
Module loading respects the selected configuration. Disabled features are not
required during validation; disabling USB-C policy also stops its service.
It refuses to replace a differing DKMS source snapshot under the same version.

Programs and the service unit are installer-owned and replaced during upgrades.
Use systemd drop-ins for service customizations. For the tracked module settings,
boot configuration and preference drop-ins, the installer updates files that
still match the last shipped default. Locally edited files are preserved, with
new defaults saved as `.dist` files. Defaults are recorded under
`/var/lib/schenker-kbd/config-defaults`. On the first upgrade, unknown existing
configuration is preserved; the original backlight and 0.6.0 module defaults are
recognized and migrated.

Before replacing installed modules, the installer saves the previous files,
per-kernel versions, loaded module parameters and service state. An installation
failure triggers rollback and an initramfs refresh. Backups remain under
`/var/tmp/schenker-rollback.*` after failure; an incomplete rollback is reported
explicitly. Module reload resets runtime fan-curve edits, as on a normal reload.

`sudo ./install.sh remove` checks that the checkout version matches the installed
modules and any running driver before stopping services or deleting shared files.
It refuses absent or mixed versions. Earlier built DKMS versions remain;
inspect `dkms status -m schenker-kbd` when retiring them.

The full installer tests require `bubblewrap` and unprivileged user namespaces.
They run the real script with a private filesystem and fake DKMS, module and
service commands. Host files and the checkout are mounted read-only.

## Manual tools

- `sudo python3 tests/hardware/test-brightness.py` cycles and restores the LED.
- `sudo tools/trial.sh probe` loads read-only backlight diagnostics.
- `sudo tools/trial.sh enable` loads the backlight-only writable module.
- `sudo tools/trial.sh disable` unloads it.

Build before using the trial loader. It refuses a claimed device and does not
activate USB-C priority or the fan curve.

`tools/power-experiment.py` retains the earlier short load/reselection experiment
for investigation. It is not installed. Its 3–30 second tests stop at 95°C and
cannot measure sustained cooling capacity; `--recover` merely restarts the policy
between measurements and is not a verified fix.

## Limitations and references

Secure Boot requires enrollment of the DKMS signing certificate. Fn+Space
brightness changes are readable through sysfs, but desktop change notifications
are not implemented. The intermittent roughly 35 W CPU behavior is unresolved.
See [validation](validation/README.md) for test scope and hardware results.

The driver derives from the
[Linux Uniwill protocol](https://github.com/torvalds/linux/blob/master/drivers/platform/x86/uniwill/uniwill-acpi.c)
and [TUXEDO backlight implementation](https://github.com/tuxedocomputers/tuxedo-drivers/blob/main/src/uniwill_leds.h).
