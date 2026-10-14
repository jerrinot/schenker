# USB-C power reporting comparison

On 2026-10-14 the user compared two USB-C-to-USB-C cables, connected directly to
the same charger and laptop port. One had previously coincided with roughly
45 W CPU operation; changing cables had improved performance earlier. Neither
cable's e-marker status was independently established.

Readings before and after the swap were identical:

| Reading | Both cables |
| --- | --- |
| UCSI `voltage_now` | 20,000,000 microvolts |
| UCSI `current_now` / `current_max` | 5,000,000 / 5,000,000 microamps |
| Fresh GET_CONNECTOR_STATUS RDO | `0x5287d1f4`: PDO 5, operating/max current 5 A |
| Fresh source PDO 5 | `0x000641f4`: fixed 20 V, 5 A |
| Fresh GET_CABLE_PROPERTY | `0x0012002b` |
| Firmware cached targets | PL1 45 W, PL2 55 W, PL4 80 W, port 1 100 W |
| Linux RAPL PL1 / PL2 | 115 W / 115 W |
| Power priority / charging profile | performance / trickle |

The UCSI debugfs queries used only GET_CONNECTOR_STATUS, GET_CABLE_PROPERTY and
GET_PDOS. One initial connector-status query returned a disconnected result;
subsequent queries consistently returned the RDO above. Cable metadata exposed
`type-a` despite the physical C-to-C connection, with no identity directory.
These interfaces do not establish the physical cable's rating. No sustained
load or electrical measurement was performed during this comparison.

The policy previously multiplied `voltage_now` by `current_max`. In Linux's UCSI
driver the latter is the maximum current across advertised source PDOs. The policy
now also uses `current_now`, which comes from the RDO operating current, taking
the smaller current. A 20 V / 3 A request against a source advertising 5 A now
reports 60 W. This does not change the observed 100 W reading for either cable
above because both current fields report 5 A.

The menu now says **Reported USB-C power** and does not claim a verified cable
rating. See the [Linux UCSI power-supply implementation](https://github.com/torvalds/linux/blob/v7.0/drivers/usb/typec/ucsi/psy.c)
for the meanings of these fields. The earlier performance difference remains
unexplained by this comparison; a cable-controlled load test or PD analyzer would
be needed to investigate it further.
