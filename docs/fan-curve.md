# Firmware fan curve and hwmon

The driver exposes the device named `schenker`. Find it without relying on a
numeric hwmon ID:

```sh
for h in /sys/class/hwmon/hwmon*; do
    [ "$(cat "$h/name")" = schenker ] && printf '%s\n' "$h"
done
sensors 'schenker-*'
```

## Attributes

| Attribute | Meaning |
| --- | --- |
| `temp1_input`, `temp1_label` | EC CPU temperature in millidegrees Celsius |
| `fan1_input`, `fan2_input` | Fan RPM; labels identify shared CPU control |
| `pwm1`, `pwm2` | Current output, read-only, normalized to 0–255 |
| `pwm1_enable` | Shared controller mode, root-writable |
| `pwm2_enable` | Read-only mirror; both fans use the same curve |
| `pwm1_auto_channels_temp` | Read-only `1`, meaning `temp1` |
| `pwm1_auto_pointN_temp` | Rising threshold in millidegrees Celsius |
| `pwm1_auto_pointN_temp_hyst` | Absolute falling threshold in millidegrees Celsius |
| `pwm1_auto_pointN_pwm` | Output for that state, normalized to 0–255 |

There are 12 points. All attributes are world-readable; writable controls require
root. Point 1 is the fixed off baseline (0, 0, 0). Temperatures for points 2–12
are writable; output for point 12 is fixed at 255 (full speed).

`pwm1_enable` accepts:

- **0:** continuous full output, using a firmware table with all 16 outputs at
  raw 200. It does not use the self-clearing firmware boost bit or a fan lease.
- **2:** the editable firmware curve (boot default).
- **3:** the original stock ROM curve.

Mode 1 and direct PWM writes are unsupported. Curve attributes show the configured
editable curve even while stock or full-speed mode is active.

## Default custom curve

| Point | Rising °C | Falling °C | Output / 255 |
| --- | --- | --- | --- |
| 1 | 0 | 0 | 0 |
| 2 | 54 | 45 | 64 |
| 3 | 60 | 57 | 77 |
| 4 | 65 | 64 | 89 |
| 5 | 67 | 66 | 128 |
| 6 | 70 | 69 | 153 |
| 7 | 72 | 71 | 179 |
| 8 | 74 | 73 | 204 |
| 9 | 76 | 75 | 204 |
| 10 | 79 | 77 | 217 |
| 11 | 85 | 82 | 242 |
| 12 | 90 | 87 | 255 |

The firmware executes a stepwise curve, not linear interpolation. It advances
above rising thresholds and descends below falling thresholds, with strict `>`
and `<` comparisons. These are EC temperatures, not instantaneous CPU core or
hotspot readings. Firmware controls stage transitions and output ramping.

At idle, full-speed selection took about 77 seconds to reach maximum during the
recorded test (roughly two raw output units per second). Both fans then ran near
6,700 RPM. Reaching full speed is not instantaneous, even in mode 0.

## Validation and edits

Rising thresholds must be whole degrees in the range 20–95°C, strictly increasing.
Falling thresholds must be positive, below their corresponding rising thresholds,
and nondecreasing. Output must be nondecreasing; nonzero stages retain at least
raw 50/200 (64/255 at the interface), and the highest stage stays at full output.
PWM values are rounded to the EC's 0–200 resolution; read back after writing.
Invalid edits fail before changing committed configuration or EC RAM.

Replace `PATH` with the discovered hwmon directory:

```sh
cat PATH/pwm1_auto_point12_temp
# Request full speed above 88°C, retaining the 87°C falling threshold:
echo 88000 | sudo tee PATH/pwm1_auto_point12_temp
# Restore the default:
echo 90000 | sudo tee PATH/pwm1_auto_point12_temp
```

In mode 2, each edit validates the complete curve, temporarily selects stock
control, writes and verifies the replacement, then selects the custom table.
The transaction takes a few seconds.

For several edits, select mode 3 (stock) or 0 (full output), edit the inactive
configuration, then write 2 to apply it once. Each intermediate configuration
must still be valid, so choose an edit order that preserves the constraints.
A failed application attempts to restore the previous configuration. Persistent
I/O errors retain a valid selected table or fall back to stock and queue cleanup.
A stale cleanup callback cannot undo a later successful update.

## Persistence and implementation

The module loads the default custom curve at boot. Runtime edits and mode selection
are retained in driver memory across suspend/resume, but reboot or module reload
resets them to the default. Transaction reapplication has emulator coverage; see the validation notes for
hardware coverage.
No fan polling daemon or automatic persistent user-curve file is installed.

`driver/schenker_curve.h` holds curve validation and EC transactions; `driver/schenker_hwmon.h`
provides the sysfs interface. The firmware has 16 slots, with four unreachable
reserved slots hidden from hwmon. Rising threshold for state `s` maps to
`up[s-1]`, falling threshold to `down[s]`, and output to `PWM[s]`.
The active table is never overwritten: both selectors are cleared and verified
before restoring or replacing RAM. The original table and owned flags are
restored on removal; suspend/resume uses the same ownership-aware transactions.

## Evidence and scope

See [validation records](validation/README.md) for emulator and live checks.

Protocol evidence came from EC 1.47 disassembly: bank 2 addresses `8bac`/`8c31`
for rising comparisons, `d160`/`d640` for cooling, and `8c52` for output lookup,
plus the validated 16-stage RAM table.

Interface reference: [Linux hwmon sysfs ABI](https://docs.kernel.org/hwmon/sysfs-interface.html).
