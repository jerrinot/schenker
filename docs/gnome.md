# GNOME USB-C Performance

Requires GNOME Shell 50. Install the system service with `sudo ./install.sh`, then
run `./gnome-extension/install.sh` as the desktop user. Log out and back in to
load the extension; its installer queues enablement without restarting Shell.
Updates to extension JavaScript also require a new login. The system installer
does not install extensions into root's home directory.

The tile's highlight represents the saved performance preference. Its subtitle
reports the active priority or why performance is waiting. The arrow shows the
preference, active priority, firmware-reported USB-C power and battery charge.
This is neither measured consumption nor a verified cable rating. The current policy requires 100 W;
a smaller supply leaves the preference enabled but selects charging priority.

The same menu contains a **Charging profile** selector: **Trickle**, **Standard**,
and **Long Life**. These are firmware profile names, not fixed wattage settings.
The selector title shows the active profile read from firmware, while the dot marks
your saved choice. If the firmware cannot apply it, the menu reports the failure
and the service retries. Changing this profile leaves the performance preference
unchanged, including when the performance toggle is off.

Off explicitly selects charging priority. On uses the existing source and battery
gates. GNOME's Power Mode, the battery charging profile and the fan curve are
independent. No additional CPU cap is introduced by the extension.

The preference is machine-wide, stored in the root-owned file
`/var/lib/schenker-usbc-policy/preference.json`. Missing means enabled, matching
the policy before desktop integration. Malformed state fails startup rather than
silently enabling performance. Service restart, logout and disabling the extension
preserve the choice. Stopping the service restores charging priority; the driver's
20-second lease covers service crashes. With the service unavailable, the tile
clears live telemetry and disables changes.

The charging choice is stored in the same file. Existing preference files remain
valid. Until you select a profile, the service uses `SCHENKER_CHARGING_PROFILE`
from its systemd drop-in (Trickle in the installed configuration). A menu selection
overrides that default across restarts. Firmware resets trigger restoration of
the saved choice. Actual charge rates and charge limits have not been measured
for each profile.

## Service interface

The system bus name and interface are `io.github.jerrinot.Schenker.Power`, at
`/io/github/jerrinot/Schenker/Power`. `SetPerformancePreferred(boolean)` checks
Polkit action `io.github.jerrinot.schenker.usbc-performance` asynchronously using
the actual bus sender. Active local users are allowed without a password prompt;
inactive and remote users are denied by the shipped policy. Root and administrator
Polkit rules can override this. All users may read status. Authorization never
blocks the two-second policy timer, and only one change request is pending at a time.

`SetChargingProfile(string)` accepts `trickle`, `standard`, or `long_life`. It uses
the separate Polkit action `io.github.jerrinot.schenker.charging-profile` with the
same active-local-user policy. Invalid profile names are rejected before writing
preferences or firmware.

Read-only D-Bus properties:

| Property | Meaning |
| --- | --- |
| `PerformancePreferred` | Persisted boolean preference |
| `Priority` | Verified firmware setting: `charging` or `performance` |
| `Reason` | `starting`, `user`, `ready`, `supply`, `battery`, `discharge`, or `temperature` |
| `Description` | Human-readable reason for the current decision |
| `SupplyWatts` | Firmware-reported eligible power budget from UCSI or the EC fallback, not a verified cable rating |
| `BatteryPercent` | Battery charge percentage |
| `Ready` | Whether an initial hardware update has completed |
| `ChargingProfile` | Verified active firmware profile; empty when unavailable |
| `DesiredChargingProfile` | Saved choice or configured default; empty if unmanaged |
| `ChargingProfileError` | Read/apply failure description; empty when healthy |

Changes are published together through `PropertiesChanged`. The extension follows
service ownership changes and does not launch a privileged helper or write sysfs.
Failed requests are shown in the menu without optimistically changing the saved
preference. A successful method reply means the choice was saved and one policy
update completed; it does not bypass the performance eligibility checks.
For a charging-profile request, success also requires matching firmware readback.
A failed apply retains the saved choice for retry and reports an error; it does
not stop the power policy.

```sh
gdbus call --system --dest io.github.jerrinot.Schenker.Power \
  --object-path /io/github/jerrinot/Schenker/Power \
  --method org.freedesktop.DBus.Properties.GetAll io.github.jerrinot.Schenker.Power
```

To remove the tile, disable it in Extensions and remove
`~/.local/share/gnome-shell/extensions/schenker-usbc@jerrinot`.
Removing the system deployment removes its D-Bus and Polkit definitions but retains
the saved preference for a later reinstall.

## Development checks

`make test` requires `python3-gi`, `dbus-daemon` and `gjs` in addition to the driver
test dependencies. Desktop tests use a private bus and fake authorization service;
they never call the host's policy or write firmware. They cover preference
persistence, denied requests, storage failure, asynchronous authorization, service
restart, read-only properties, source gating and status presentation.
