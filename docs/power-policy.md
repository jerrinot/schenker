# USB-C and charging policy

`userspace/schenker-usbc-policy.py` runs as `schenker-usbc-policy.service`. Its configuration
is in the two tracked drop-ins under `packaging/systemd/schenker-usbc-policy.service.d/`.
The installer deploys those files to `/etc/systemd/system/` unchanged.

The included configuration selects `firmware` power limits and `trickle` charging.
Without the power-profile drop-in, the Python program defaults to `balanced`,
which adds a 40 W CPU cap and temperature gating. This is not the installed
preference. Keep the drop-ins with the service when reproducing this setup.

The policy polls every two seconds and requests performance priority when the
saved desktop preference is enabled (the default) and:

- The online USB-C PD/PD-PPS source reports at least 19 V and 100 W, or the
  embedded-controller fallback below reports at least 100 W.
- Battery charge has not fallen below 30%; after doing so it must recover to 35%.
- Reported battery discharge is at most 3 W.

Otherwise it selects charging priority. Each performance write renews the driver's
20-second lease; stopping the service restores charging priority, and a crashed
service loses the lease. Firmware mode does not add CPU caps or a temperature
cutoff. The driver and hardware thermal protections continue to operate.

The power budget uses UCSI `voltage_now` multiplied by the smaller of `current_now`
and `current_max`. For this driver, `current_now` comes from the power request's
operating current; it is not a measured current draw. `current_max` is the largest
current across advertised source PDOs and cannot alone establish the selected
budget. Neither field independently verifies a cable's rating. Firmware reporting
errors can therefore make the policy overestimate available power. The GNOME menu
labels this value as reported power, and the service does not claim cable verification.

On the VISION E22, the left USB-C port can supply external power while both UCSI
ports report disconnected. When no UCSI source is online, the service falls back
to `firmware_power_targets` only if `AC0/online` is 1 and exactly one EC port budget
is nonzero. Missing, malformed or ambiguous readings leave performance ineligible.
An online UCSI source always takes precedence, including a lower or unknown budget.
The EC values are cached firmware reports, not measured input power; requiring AC
prevents a stale budget alone from enabling performance after disconnect. The
status JSON records `supply_source` as `ucsi`, `ec` or `none` alongside `supply_w`.

The charging default accepts `standard`, `long_life`, `trickle`, or an empty
value to leave it unmanaged. A saved desktop selection overrides this default.
The service restores the selected preference after firmware
resets, rate-limits retries and avoids rewriting unchanged state. These profiles
are firmware choices, not Linux CPU performance profiles.

Inspect with `systemctl status schenker-usbc-policy`,
`journalctl -u schenker-usbc-policy`, or `sudo schenker-power`.
`schenker-power --json` prints machine-readable status. It does not change settings
or create log files. Permission errors produce a nonzero exit with a sudo hint;
missing optional attributes are reported as unavailable. Firmware-reported power targets are cached values, not CPU
limit readback.

Fan speed is controlled by the firmware curve independently of this service.
The optional [GNOME Quick Settings tile](gnome.md) changes the saved preference
through D-Bus; the service owns the preference even when GNOME is not running.
