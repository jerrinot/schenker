# USB-C and charging policy

`userspace/schenker-usbc-policy.py` runs as `schenker-usbc-policy.service`. Its configuration
is in the two tracked drop-ins under `packaging/systemd/schenker-usbc-policy.service.d/`.
The installer deploys those files to `/etc/systemd/system/` unchanged.

The included configuration selects `firmware` power limits and `trickle` charging.
Without the power-profile drop-in, the Python program defaults to `balanced`,
which adds a 40 W CPU cap and temperature gating. This is not the installed
preference. Keep the drop-ins with the service when reproducing this setup.

The policy polls every two seconds and requests performance priority when:

- The online USB-C PD/PD-PPS source reports at least 19 V and 100 W.
- Battery charge has not fallen below 30%; after doing so it must recover to 35%.
- Reported battery discharge is at most 3 W.

Otherwise it selects charging priority. Each performance write renews the driver's
20-second lease; stopping the service restores charging priority, and a crashed
service loses the lease. Firmware mode does not add CPU caps or a temperature
cutoff. The driver and hardware thermal protections continue to operate.

The charging preference accepts `standard`, `long_life`, `trickle`, or an empty
value to leave it unmanaged. It restores the selected preference after firmware
resets, rate-limits retries and avoids rewriting unchanged state. These profiles
are firmware choices, not Linux CPU performance profiles.

Inspect with `systemctl status schenker-usbc-policy`,
`journalctl -u schenker-usbc-policy`, or `sudo schenker-power`.
`schenker-power --json` prints machine-readable status. It does not change settings
or create log files. Permission errors produce a nonzero exit with a sudo hint;
missing optional attributes are reported as unavailable. Firmware-reported power targets are cached values, not CPU
limit readback.

Fan speed is controlled by the firmware curve independently of this service.
