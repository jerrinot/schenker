#!/usr/bin/env python3
"""Cycle the trial LED once and always restore its starting brightness."""
import os
from pathlib import Path
import time

led = Path('/sys/class/leds/schenker:white:kbd_backlight')
brightness = led / 'brightness'
status = Path('/sys/bus/platform/devices/INOU0000:00/raw_status')

if os.geteuid() != 0:
    raise SystemExit('Run with sudo: changing LED brightness requires root.')
if (led / 'max_brightness').read_text().strip() != '2':
    raise SystemExit('Unexpected brightness range; aborting.')
original = int(brightness.read_text())
if original not in range(3):
    raise SystemExit('Unexpected starting level; aborting.')
try:
    for level in range(3):
        print(f'Setting {level} ({["off", "dim", "bright"][level]})', flush=True)
        brightness.write_text(f'{level}\n')
        time.sleep(0.25)
        actual = int(brightness.read_text())
        print(status.read_text().strip(), flush=True)
        if actual != level:
            raise RuntimeError(f'Readback mismatch: requested {level}, got {actual}')
        time.sleep(3)
finally:
    brightness.write_text(f'{original}\n')
    time.sleep(0.25)
    actual = int(brightness.read_text())
    if actual != original:
        raise RuntimeError(f'Failed to restore level {original}: read back {actual}')
    print(f'Restored starting level {original}', flush=True)
