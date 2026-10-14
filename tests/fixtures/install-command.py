#!/usr/bin/python3
"""DKMS/systemd/module stand-ins used only inside the install tests' namespace."""
import json
import os
from pathlib import Path
import shutil
import sys

state_file = Path('/tmp/mock-state.json')
state = json.loads(state_file.read_text())
command = Path(sys.argv[0]).name
args = sys.argv[1:]
state['events'].append([command, *args])
code = 0


def argument(flag):
    return args[args.index(flag) + 1]


def fail_once(point):
    if point in state.get('failures', []):
        state['failures'].remove(point)
        return True
    if state.get('failure') == point:
        state['failure'] = None
        return True
    return False


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def load_module(version, parameters):
    module = Path('/sys/module/schenker_kbd')
    module.mkdir(parents=True, exist_ok=True)
    write(module / 'version', version)
    for name, value in parameters.items():
        write(module / 'parameters' / name, value)
    device = Path('/sys/devices/platform/INOU0000:00')
    device.mkdir(parents=True, exist_ok=True)
    if parameters.get('writable') == 'Y':
        write('/sys/class/leds/schenker:white:kbd_backlight/brightness', '1')
    if parameters.get('firmware_curve') == 'Y':
        write(device / 'fan_curve_status', 'configured=1 active=1 error=0 requested_mode=2')
    if parameters.get('usb_c_policy') == 'Y':
        write(device / 'usb_c_power_priority', 'charging')
    state['loaded'] = version
    state['parameters'] = parameters


if command == 'modinfo':
    version = state['installed'].get(argument('-k'))
    if version:
        print(version)
    else:
        code = 1
elif command == 'dkms':
    action, version = args[0], argument('-v')
    if action == 'status':
        for kernel, installed in state['installed'].items():
            if installed == version:
                print(f'schenker-kbd/{version}, {kernel}, x86_64: installed')
    elif action in ('add', 'build'):
        Path(f'/var/lib/dkms/schenker-kbd/{version}').mkdir(parents=True, exist_ok=True)
    elif action == 'install':
        kernel = argument('-k')
        # Model a failure after DKMS has already replaced the module on disk.
        if '--force' in args and fail_once('rollback_dkms'):
            code = 1
        else:
            state['installed'][kernel] = version
        if version == '0.7.0' and fail_once('dkms_install'):
            code = 1
    elif action == 'remove':
        kernels = list(state['installed']) if '--all' in args else [argument('-k')]
        for kernel in kernels:
            if state['installed'].get(kernel) == version:
                del state['installed'][kernel]
        if '--all' in args:
            shutil.rmtree(f'/var/lib/dkms/schenker-kbd/{version}', ignore_errors=True)
    else:
        raise AssertionError(args)
elif command == 'modprobe':
    if '-r' in args:
        if fail_once('unload'):
            code = 1
        else:
            shutil.rmtree('/sys/module/schenker_kbd', ignore_errors=True)
            shutil.rmtree('/sys/devices/platform/INOU0000:00', ignore_errors=True)
            shutil.rmtree('/sys/class/leds/schenker:white:kbd_backlight', ignore_errors=True)
            state['loaded'] = None
            state['parameters'] = {}
    else:
        version = state['installed'].get(os.uname().release)
        if not version or (version == '0.7.0' and fail_once('module_load')):
            code = 1
        else:
            parameters = dict(writable='N', usb_c_policy='N', firmware_curve='N')
            config = Path('/etc/modprobe.d/schenker-kbd.conf')
            tokens = []
            if config.exists():
                for line in config.read_text().splitlines():
                    if line.startswith('options schenker_kbd '):
                        tokens.extend(line.split()[2:])
            tokens.extend(args[1:])
            for token in tokens:
                name, value = token.split('=', 1)
                parameters[name] = 'Y' if value.lower() in ('1', 'y', 'yes') else 'N'
            load_module(version, parameters)
elif command == 'systemctl':
    action = args[0]
    if action == 'is-active':
        code = 0 if state['active'] else 3
    elif action == 'is-enabled':
        code = 0 if state['enabled'] else 1
    elif action in ('start', 'restart'):
        if state['loaded'] == '0.7.0' and fail_once('service_start'):
            state['active'] = False
            code = 1
        else:
            state['active'] = True
    elif action == 'stop':
        state['active'] = False
    elif action == 'enable':
        state['enabled'] = True
    elif action == 'disable':
        if Path('/etc/systemd/system/schenker-usbc-policy.service').exists():
            state['enabled'] = False
        else:
            code = 1
    elif action != 'daemon-reload':
        raise AssertionError(args)
elif command == 'update-initramfs':
    if fail_once('initramfs'):
        code = 1
else:
    raise AssertionError(command)
state_file.write_text(json.dumps(state))
sys.exit(code)
