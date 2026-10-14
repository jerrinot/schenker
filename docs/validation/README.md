# Validation

## Automated checks

```sh
make test
make
make KDIR=/lib/modules/7.0.0-34-generic/build
```

The curve tests compile the shared transaction code with ASan/UBSan and emulate
EC reads and writes. They cover validation, controller ownership, routing guards,
mode changes, reapplication, persistent I/O failure and writes that take effect
but report failure. The summary counts runs that actually injected a fault
separately from runs outside the failure window.

These tests do not exercise ACPI, kernel PM callbacks or workqueue scheduling.
Python tests cover USB-C policy decisions, interface checks and status read
errors. Installer tests use a private filesystem with simulated DKMS/systemd
commands to check successful upgrades, custom-disabled features, version-checked
removal, and rollback after module, service, DKMS and initramfs failures. They also
check failure reporting when rollback itself fails. They do not modify hardware.

## Hardware results: 0.6.0

The JSON files record checks on the VISION E22, BIOS N.1.05A09, EC 1.47:

- Curve edits/readback, rejected inputs and stock/full/custom modes.
- Full fan output reached after about 77 seconds at idle and held through
  about 87 seconds, with both fans near 6,700 RPM.
- Module removal restored the original curve RAM and selector flags; reload
  recreated the default curve and hwmon interface.
- The curve continued running with userspace policy services stopped.

Numeric hwmon paths and sensor readings are snapshots. Physical reboot and
suspend/resume were not tested in these records.

## Driver 0.7.0 and desktop integration

Version 0.7.0 is installed and running on the VISION E22. The firmware curve is
active, and the USB-C service restores performance priority with the 100 W supply.
See [desktop integration validation](desktop.md) for the service, authorization,
persistence and GNOME Shell checks. These checks do not replace a physical reboot
or suspend/resume test.
