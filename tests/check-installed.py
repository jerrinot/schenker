#!/usr/bin/env python3
"""Read-only check that deployment copies match this maintained checkout."""
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]
version = re.search(r'^PACKAGE_VERSION="([^"]+)"$',
                    (root / 'driver/dkms.conf').read_text(), re.M).group(1)
if not Path(f'/usr/src/schenker-kbd-{version}').is_dir():
    raise SystemExit(f'Checkout version {version} is not installed.')
pairs = [(f'driver/{name}', f'/usr/src/schenker-kbd-{version}/{name}') for name in
         ('Makefile', 'dkms.conf', 'schenker_kbd.c', 'schenker_curve.h', 'schenker_hwmon.h')]
pairs += [
    ('userspace/backlight', '/usr/local/bin/schenker-backlight'),
    ('userspace/schenker-usbc-policy.py', '/usr/local/libexec/schenker-usbc-policy.py'),
    ('userspace/schenker-power', '/usr/local/sbin/schenker-power'),
    ('packaging/schenker-kbd.modprobe.conf', '/etc/modprobe.d/schenker-kbd.conf'),
    ('packaging/schenker-kbd.modules-load.conf', '/etc/modules-load.d/schenker-kbd.conf'),
    ('packaging/systemd/schenker-usbc-policy.service', '/etc/systemd/system/schenker-usbc-policy.service'),
]
for name in ('charging-profile.conf', 'firmware-profile.conf'):
    rel = f'schenker-usbc-policy.service.d/{name}'
    pairs.append((f'packaging/systemd/{rel}', f'/etc/systemd/system/{rel}'))
failures = []
for source, destination in pairs:
    try:
        if (root / source).read_bytes() != Path(destination).read_bytes():
            failures.append(f'DIFF: {source} -> {destination}')
    except OSError as error:
        failures.append(str(error))
try:
    running = Path('/sys/module/schenker_kbd/version').read_text().strip()
    if running != version:
        failures.append(f'Running version {running}, checkout version {version}')
except OSError as error:
    failures.append(str(error))
if failures:
    print('\n'.join(failures), file=sys.stderr)
    raise SystemExit(1)
print(f'PASS: {len(pairs)} deployed files match this checkout; running version {version}')
print('This checks source/configuration copies and the version, not runtime curve edits.')
