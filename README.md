# SCHENKER VISION E22 controls

Linux driver and power policy for the SCHENKER VISION (E22), board
`PHxARX1_PHxAQF1`, tested with BIOS `N.1.05A09` and EC 1.47.

Driver **0.7.0** provides keyboard backlight control, USB-C power priority,
charging profiles, fan monitoring and an editable firmware fan curve.
The default curve reaches 95% output above **85°C** and 100% above **90°C**,
using the EC temperature sensor. Fan control needs no userspace daemon.

## Install and use

Requires DKMS, a C compiler, Python 3, systemd, initramfs-tools and kernel headers.
On Ubuntu, keep `linux-headers-generic` installed for kernel updates.

```sh
sudo ./install.sh
schenker-backlight get      # also off, dim, bright; no sudo needed
sensors 'schenker-*'        # requires lm-sensors
sudo schenker-power        # power and cooling status
```

Installation enables boot loading, the custom fan curve, and a separate USB-C
policy service. The included preferences select firmware CPU limits (no added
power cap) and Trickle charging. Performance priority is gated on a reported
100 W USB-C supply, battery charge and discharge.

The default custom curve returns at boot. Runtime curve edits reset on reboot or
module reload. See the validation notes for hardware test coverage.

## Repository layout

| Directory | Contents |
| --- | --- |
| `driver/` | Kernel module and self-contained DKMS build inputs |
| `userspace/` | Backlight helper, power diagnostics and USB-C policy |
| `packaging/` | Boot configuration, systemd unit and preference drop-ins |
| `tests/` | Automated tests and deployment comparison; explicit hardware tests in `hardware/` |
| `tools/` | Manual developer tools |
| `docs/` | Interfaces, development instructions and historical validation |

This checkout is the source of truth. `/usr/src/schenker-kbd-VERSION` is an installed
snapshot; the healthcheck directories are historical archives.

```sh
make                       # build for the running kernel
make test                  # hardware-independent regression tests
make check-installed       # read-only comparison against deployed files
```

See [fan controls](docs/fan-curve.md), [power policy](docs/power-policy.md),
[development and installation details](docs/development.md), and
[validation records](docs/validation/README.md).

To remove this version and its service: `sudo ./install.sh remove`.
