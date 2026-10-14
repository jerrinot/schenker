#!/usr/bin/python3
"""Model-specific USB-C policy. Uses only the narrow driver and Linux powercap."""
import fcntl
import json
import logging
import os
import signal
import time
from pathlib import Path

CAP_UW = 40_000_000
PROFILE = os.environ.get('SCHENKER_POWER_PROFILE', 'balanced')
if PROFILE not in ('balanced', 'firmware'):
    raise ValueError('SCHENKER_POWER_PROFILE must be balanced or firmware')
FIRMWARE_LIMITS = PROFILE == 'firmware'
RUNTIME = Path('/run/schenker-usbc-policy')
PRIORITY = Path('/sys/devices/platform/INOU0000:00/usb_c_power_priority')
MODULE = Path('/sys/module/schenker_kbd')
CHARGE_PROFILE = PRIORITY.with_name('charging_profile')
DESIRED_CHARGE_PROFILE = os.environ.get('SCHENKER_CHARGING_PROFILE', '')
if DESIRED_CHARGE_PROFILE not in ('', 'standard', 'long_life', 'trickle'):
    raise ValueError('Invalid SCHENKER_CHARGING_PROFILE')
RAPL = Path('/sys/class/powercap/intel-rapl:0')


class Conflict(RuntimeError):
    pass


def reported_usb_c_budget(sources):
    """UCSI's reported PD budget, not measured draw or verified cable capacity.

    In the UCSI power_supply driver, current_now is the RDO operating current;
    current_max is the largest current across the advertised source PDOs.
    """
    watts = 0
    for source in sources:
        if (source / 'online').read_text().strip() != '1':
            continue
        usb_type = (source / 'usb_type').read_text()
        if '[PD]' not in usb_type and '[PD_PPS]' not in usb_type:
            continue
        voltage = int((source / 'voltage_now').read_text())
        current = min(int((source / 'current_now').read_text()),
                      int((source / 'current_max').read_text()))
        if voltage >= 19_000_000 and current > 0:
            watts = max(watts, voltage * current / 1e12)
    return watts


def reported_supply(power_supply, targets):
    """Prefer UCSI; use the EC for a single supply invisible to UCSI.

    The VISION E22's left port can power the laptop while both UCSI ports
    report disconnected. EC port budgets are cached, so require AC present.
    Never replace an online UCSI source's lower or unknown budget with EC data.
    """
    online = [source for source in power_supply.glob('ucsi-source-psy-*')
              if (source / 'online').read_text().strip() == '1']
    if online:
        return dict(supply_w=reported_usb_c_budget(online), supply_source='ucsi')
    try:
        if (power_supply / 'AC0/online').read_text().strip() == '1':
            fields = dict(field.split('=') for field in targets.read_text().split())
            budgets = [int(fields[name]) for name in ('port1_w', 'port2_w')]
            # These are byte registers. With two nonzero reports we cannot
            # establish which port is supplying power from this interface.
            if all(0 <= value <= 255 for value in budgets):
                active = [value for value in budgets if value > 0]
                if len(active) == 1:
                    return dict(supply_w=active[0], supply_source='ec')
    except (OSError, ValueError, KeyError):
        pass  # Optional fallback unavailable; leave performance ineligible.
    return dict(supply_w=0, supply_source='none')


class Hardware:
    def validate_driver(self):
        driver = (PRIORITY.parent / 'driver').resolve().name
        if (driver != 'schenker-kbd' or
                not PRIORITY.is_file() or
                (MODULE / 'parameters/usb_c_policy').read_text().strip() != 'Y'):
            raise RuntimeError('Expected schenker-kbd USB-C interface with its performance lease enabled')

    def get_charge_profile(self):
        return CHARGE_PROFILE.read_text().strip()

    def set_charge_profile(self, value):
        self.validate_driver()
        if not CHARGE_PROFILE.is_file():
            raise RuntimeError('Driver does not expose charging profiles')
        CHARGE_PROFILE.write_text(value)
        if self.get_charge_profile() != value:
            raise RuntimeError('Charging profile readback mismatch')

    def get_priority(self):
        return PRIORITY.read_text().strip()

    def set_priority(self, value):
        if value == 'performance':
            self.validate_driver()
        PRIORITY.write_text(value)
        if self.get_priority() != value:
            raise RuntimeError('USB-C priority readback mismatch')

    def caps(self):
        return {str(i): int((RAPL / f'constraint_{i}_power_limit_uw').read_text())
                for i in (0, 1)}

    def set_cap(self, index, value):
        if index not in ('0', '1'):
            raise ValueError('Invalid constraint')
        (RAPL / f'constraint_{index}_power_limit_uw').write_text(str(value))

    def caps_enabled(self):
        return (RAPL / 'enabled').read_text().strip() == '1'

    def snapshot(self):
        supply = reported_supply(Path('/sys/class/power_supply'),
                                 PRIORITY.with_name('firmware_power_targets'))
        bat = Path('/sys/class/power_supply/BAT0')
        discharge = 0
        if (bat / 'status').read_text().strip() == 'Discharging':
            discharge = abs(int((bat / 'current_now').read_text()) *
                            int((bat / 'voltage_now').read_text())) / 1e12
        temps = []
        for p in Path('/sys/class/hwmon').glob('hwmon*'):
            if (p / 'name').read_text().strip() == 'coretemp':
                temps.extend(int(t.read_text()) / 1000 for t in p.glob('temp*_input'))
        if not temps or any(t <= 0 or t >= 125 for t in temps):
            raise RuntimeError('CPU temperature sensors unavailable or implausible')
        return dict(**supply, battery_pct=int((bat / 'capacity').read_text()),
                    discharge_w=discharge, temp_c=max(temps))


class ChargePreference:
    """Restore a configured preference after reset; never write unchanged state.

    Polling also covers resume and missed adapter notifications. Rate-limit
    restoration if firmware or another controller repeatedly changes it.
    """
    def __init__(self, desired):
        self.desired = desired
        self.next_restore = 0
        self.state = 'unmanaged'

    def reconcile(self, hw, now):
        actual = hw.get_charge_profile()
        if not self.desired:
            return actual
        if actual == self.desired:
            self.state = 'verified'
        elif now < self.next_restore:
            self.state = 'waiting before retry'
        else:
            self.next_restore = now + 30
            hw.set_charge_profile(self.desired)
            logging.info('Restored charging profile: %s -> %s', actual, self.desired)
            actual = hw.get_charge_profile()
            self.state = 'verified'
        return actual


class Gate:
    def __init__(self):
        self.hot = False
        self.cool_until = 0
        self.low_battery = False
        self.reason = 'starting'

    def evaluate(self, s, now):
        if not FIRMWARE_LIMITS and s['temp_c'] >= 94 and not self.hot:
            self.hot, self.cool_until = True, now + 60
        if self.hot and s['temp_c'] <= 80 and now >= self.cool_until:
            self.hot = False
        if s['battery_pct'] < 30:
            self.low_battery = True
        elif s['battery_pct'] >= 35:
            self.low_battery = False
        if s['supply_w'] < 100:
            self.reason = 'supply'
            return 'charging: supply below 100 W or disconnected'
        if self.low_battery:
            self.reason = 'battery'
            return 'charging: low battery (resume at 35%)'
        if s['discharge_w'] > 3:
            self.reason = 'discharge'
            return 'charging: battery discharge exceeds 3 W'
        if self.hot:
            self.reason = 'temperature'
            return 'charging: thermal cooldown (60 s and <=80 C)'
        self.reason = 'ready'
        if FIRMWARE_LIMITS:
            return 'performance: firmware reports at least 100 W, firmware CPU limits (no added cap)'
        return 'performance: firmware reports at least 100 W, CPU capped at 40 W'


class Journal:
    def __init__(self, path):
        self.path = path

    def load(self):
        if not self.path.exists():
            return None
        value = json.loads(self.path.read_text())
        if set(value) != {'original', 'applied'}:
            raise RuntimeError('Invalid recovery journal')
        for key in value:
            if set(value[key]) != {'0', '1'}:
                raise RuntimeError('Invalid recovery constraints')
            if any(type(v) is not int or v <= 0 for v in value[key].values()):
                raise RuntimeError('Invalid recovery values')
        if any(v > CAP_UW for v in value['applied'].values()):
            raise RuntimeError('Invalid applied cap')
        return value

    def save(self, value):
        if value is None:
            self.path.unlink(missing_ok=True)
            return
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(value) + '\n')
        tmp.chmod(0o600)
        tmp.replace(self.path)


class Controller:
    def __init__(self, hw, journal):
        self.hw = hw
        self.journal = journal
        self.saved = journal.load()

    def deactivate(self):
        # Never raise/release caps until charging priority is verified.
        self.hw.set_priority('charging')
        if self.saved:
            actual = self.hw.caps()
            for index, applied in self.saved['applied'].items():
                if actual[index] == applied:
                    self.hw.set_cap(index, self.saved['original'][index])
                    if self.hw.caps()[index] != self.saved['original'][index]:
                        raise RuntimeError('Power-cap restore readback mismatch')
            self.journal.save(None)
            self.saved = None

    def activate(self):
        if FIRMWARE_LIMITS:
            # Recover older capped sessions once, then leave all limits alone.
            if self.saved:
                self.deactivate()
            if not self.hw.caps_enabled() or any(v <= 0 for v in self.hw.caps().values()):
                raise RuntimeError('RAPL caps not enabled or invalid')
            self.hw.set_priority('performance')
            return
        # Suspend/lease expiry can clear priority and reset firmware limits.
        if self.saved and self.hw.get_priority() != 'performance':
            self.deactivate()
        if not self.saved:
            self.hw.set_priority('charging')
            original = self.hw.caps()
            if not self.hw.caps_enabled() or any(v <= 0 for v in original.values()):
                raise RuntimeError('RAPL caps not enabled or invalid')
            self.saved = dict(original=original,
                              applied={i: min(v, CAP_UW) for i, v in original.items()})
            # Recovery intent is saved before the first cap mutation.
            self.journal.save(self.saved)
            for index, value in self.saved['applied'].items():
                self.hw.set_cap(index, value)
        if not self.hw.caps_enabled() or self.hw.caps() != self.saved['applied']:
            self.deactivate()
            raise Conflict('CPU power limits changed externally; refusing to fight another controller')
        # Each successful write renews the driver's 20-second lease.
        self.hw.set_priority('performance')


class Policy:
    def __init__(self, hw, controller, preferred):
        self.hw, self.controller, self.preferred = hw, controller, preferred
        self.gate = Gate()
        self.charge = ChargePreference(preferred.charging_profile)
        self.last_state = None

    def update(self):
        hw, controller, charge = self.hw, self.controller, self.charge
        snapshot = hw.snapshot()
        state = self.gate.evaluate(snapshot, time.monotonic())
        reason = self.gate.reason
        if not self.preferred.enabled:
            state, reason = 'charging: selected by user', 'user'
        if state.startswith('performance:'):
            controller.activate()
        elif controller.saved or hw.get_priority() != 'charging':
            controller.deactivate()
        if state != self.last_state:
            logging.info('%s; telemetry=%s', state, snapshot)
            self.last_state = state
        # A charging-preference failure must not restart/toggle the power policy.
        if charge.desired != self.preferred.charging_profile:
            charge.desired = self.preferred.charging_profile
            charge.next_restore = 0  # An explicit new choice takes effect immediately.
        try:
            actual_charge = charge.reconcile(hw, time.monotonic())
            charge_error = None
        except (OSError, RuntimeError) as exc:
            actual_charge, charge_error = None, str(exc)
            if charge.state != 'error':
                logging.error('Charging profile restore failed: %s', exc)
            charge.state = 'error'
        return dict(state=state, reason=reason,
                    performance_preferred=self.preferred.enabled,
                    telemetry=snapshot, caps_uw=hw.caps(),
                    charging_profile=actual_charge,
                    desired_charging_profile=charge.desired or None,
                    charging_profile_state=charge.state,
                    charging_profile_error=charge_error,
                    priority=hw.get_priority(), updated=time.time())


def main():
    from gi.repository import GLib, GLibUnix
    from schenker_usbc_dbus import Preferences, PowerBus

    logging.basicConfig(level=logging.INFO, format='%(message)s')
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    with (RUNTIME / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        loop = GLib.MainLoop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, sig, loop.quit)
        hw = Hardware()
        hw.validate_driver()
        logging.info('Power profile: %s', PROFILE)
        # Even a corrupt journal must not leave performance priority active.
        hw.set_priority('charging')
        controller = Controller(hw, Journal(RUNTIME / 'caps.json'))
        bus, failure = None, None

        def disconnected():
            nonlocal failure
            failure = RuntimeError('System D-Bus connection lost')
            loop.quit()

        def update():
            nonlocal failure
            try:
                status = policy.update()
                tmp = RUNTIME / 'status.tmp'
                tmp.write_text(json.dumps(status, indent=2) + '\n')
                tmp.replace(RUNTIME / 'status.json')
                bus.publish(status)
                return GLib.SOURCE_CONTINUE
            except Exception as exc:
                failure = exc
                loop.quit()
                return GLib.SOURCE_REMOVE

        try:
            controller.deactivate()  # Recover any interrupted earlier activation.
            preferred = Preferences(Path('/var/lib/schenker-usbc-policy/preference.json'),
                                    DESIRED_CHARGE_PROFILE)
            policy = Policy(hw, controller, preferred)
            bus = PowerBus(preferred, update, disconnected)
            if update():
                GLib.timeout_add_seconds(2, update)
                loop.run()
            if failure:
                raise failure
        finally:
            try:
                if bus:
                    bus.close()
            finally:
                controller.deactivate()
            (RUNTIME / 'status.json').write_text(json.dumps(dict(
                state='stopped', priority=hw.get_priority(), caps_uw=hw.caps(),
                updated=time.time()), indent=2) + '\n')
            logging.info('Stopped: charging priority verified; owned CPU caps restored')


if __name__ == '__main__':
    try:
        main()
    except Conflict:
        logging.exception('Policy conflict; restart service after resolving competing controller')
        raise SystemExit(78)
