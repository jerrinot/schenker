# USB-C desktop integration validation

Tested on 2026-10-14 with GNOME Shell 50.1, the VISION E22 and driver 0.7.0.
Driver source was unchanged by the desktop integration.

- `make test`: 70 Python tests passed, including private-bus authorization and
  persistence tests, plus the C curve and GJS presentation tests.
- Real service: selecting off wrote charging priority; restarting the service
  preserved it; selecting on restored performance priority with the 100 W supply.
- Installed Polkit action: an active local Shell process was authorized; a method
  call from the remote session was rejected without changing the preference.
- Isolated GNOME 50 Shell: extension loaded and rendered the full-width tile and
  status menu. A denied click displayed an error while retaining the preference.
  Stopping the real service cleared telemetry and disabled the control; restarting
  it restored the display. Disabling/re-enabling the extension reported no errors.
- Charging profiles: Standard, Long Life and Trickle each matched firmware
  readback. Long Life survived a service restart, and Trickle was restored after
  testing. The performance preference remained unchanged. These checks confirm
  profile selection and persistence, not charging speed or battery thresholds.
- The charging submenu rendered all three choices in an isolated Shell, with the
  saved profile marked. A remote-session click was denied and displayed an error.
- [USB-C reporting comparison](usb-c-reporting.md): both user-supplied cables
  reported 100 W. The current-field calculation and UI wording were corrected;
  neither cable's physical rating was verified.

The isolated Shell used a private session bus and temporary configuration. The
user confirmed that the original performance toggle works in the ordinary desktop
session. The updated charging selector requires a new login to load; it has not
yet been checked by the user. An actual reboot has not been tested.
