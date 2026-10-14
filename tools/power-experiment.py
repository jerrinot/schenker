#!/usr/bin/python3
"""Historical short load/reselection experiment; not a validated recovery tool."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import statistics
import subprocess
import time

DEVICE = Path('/sys/devices/platform/INOU0000:00')
RAPL = Path('/sys/class/powercap/intel-rapl:0')
SERVICE = 'schenker-usbc-policy.service'


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def temperatures():
    values = []
    for p in Path('/sys/class/hwmon').glob('hwmon*'):
        if read(p / 'name') == 'coretemp':
            values.extend(int(t.read_text()) / 1000 for t in p.glob('temp*_input'))
    if not values or any(t <= 0 or t >= 125 for t in values):
        raise RuntimeError('CPU temperature unavailable or implausible')
    return max(values)


def snapshot():
    policy_text = read('/run/schenker-usbc-policy/status.json')
    policy = json.loads(policy_text) if policy_text else None
    fan_text = read('/run/schenker-fan-policy/status.json')
    fan_policy = json.loads(fan_text) if fan_text else None
    supplies = {}
    for p in Path('/sys/class/power_supply').glob('*'):
        supplies[p.name] = {k: v for k, v in (
            (k, read(p / k)) for k in ('online', 'usb_type', 'voltage_now',
                                      'current_max', 'current_now', 'capacity', 'status'))
                            if v is not None}
    return dict(timestamp=time.time(), driver_version=read('/sys/module/schenker_kbd/version'),
                priority=read(DEVICE / 'usb_c_power_priority'),
                charging_profile=read(DEVICE / 'charging_profile'),
                fan_status=read(DEVICE / 'fan_status'), fan_policy=fan_policy,
                firmware_fan_curve=read(DEVICE / 'fan_curve_status'),
                fan_policy_age_s=round(time.time()-fan_policy['updated'], 1) if fan_policy else None,
                firmware_reported_targets=read(DEVICE / 'firmware_power_targets'),
                target_caveat='Firmware cache/targets, not actual CPU PECI limit readback',
                host_caps_uw={str(i): read(RAPL / f'constraint_{i}_power_limit_uw') for i in (0, 1)},
                cpu_temperature_c=temperatures(), supplies=supplies, policy=policy,
                policy_age_s=round(time.time()-policy['updated'], 1) if policy else None)


def active():
    return subprocess.run(['systemctl', 'is-active', '--quiet', SERVICE]).returncode == 0


def measurement(seconds):
    """CPU package energy sampling, not wall-socket/charger power measurement."""
    if not shutil.which('stress'):
        raise RuntimeError('The stress utility is required for a load test')
    if temperatures() >= 90:
        raise RuntimeError('CPU is already at least 90 C; wait for it to cool')
    energy = lambda: int((RAPL / 'energy_uj').read_text())
    modulus = int((RAPL / 'max_energy_range_uj').read_text())
    previous_energy, previous_time = energy(), time.monotonic()
    process = subprocess.Popen(['stress', '--cpu', str(len(os.sched_getaffinity(0))),
                                '--timeout', f'{seconds+2}s'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    samples, reason = [], None
    start = previous_time
    try:
        while time.monotonic() - start < seconds:
            time.sleep(0.5)
            now_energy, now = energy(), time.monotonic()
            watts = ((now_energy - previous_energy) % modulus) / 1e6 / (now - previous_time)
            temp = temperatures()
            samples.append(dict(elapsed_s=round(now-start, 3), package_w=round(watts, 3), temp_c=temp))
            previous_energy, previous_time = now_energy, now
            if temp >= 95:
                reason = 'Stopped at 95 C'
                break
            if process.poll() is not None:
                reason = f'Load exited early ({process.returncode})'
                break
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    return dict(duration_s=round(time.monotonic()-start, 2), requested_duration_s=seconds,
                median_package_w=round(statistics.median(s['package_w'] for s in samples), 2),
                peak_sample_package_w=max(s['package_w'] for s in samples),
                peak_temperature_c=max(s['temp_c'] for s in samples),
                stop_reason=reason, samples=samples,
                caveat='Short CPU package load sample; does not establish sustained PL1 or charger draw')


def show(s):
    if s.get('firmware_fan_curve'):
        print(f"Firmware fan curve: {s['firmware_fan_curve']}")
    if s.get('fan_status'):
        print(f"Fans: {s['fan_status']}")
    if s.get('fan_policy'):
        print(f"Fan policy: {s['fan_policy']['mode']} ({s['fan_policy']['reason']}; status {s['fan_policy_age_s']} s old)")
    print(f"USB-C priority: {s['priority']}; charging profile: {s['charging_profile']}")
    print(f"Firmware-reported targets: {s['firmware_reported_targets']} (not CPU readback)")
    print(f"Host caps (uW): {s['host_caps_uw']}; CPU: {s['cpu_temperature_c']} C")
    if s['policy']:
        print(f"Policy: {s['policy']['state']} (status {s['policy_age_s']} s old)")
        if s['policy'].get('charging_profile_error'):
            print(f"Charging profile error: {s['policy']['charging_profile_error']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--test', action='store_true', help='Run a brief CPU load test')
    group.add_argument('--recover', action='store_true',
                       help='Measure, restart policy once, then measure again; recovery is unproven')
    parser.add_argument('--seconds', type=int, default=10, help='Load duration, 3-30 seconds (default 10)')
    parser.add_argument('--json', action='store_true', help='Print JSON instead of a summary')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo to read the protected power controls and RAPL counters')
    if not 3 <= args.seconds <= 30:
        parser.error('--seconds must be between 3 and 30')
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    outdir = Path('/var/log/schenker-power')
    outdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.umask(0o077)
    with open('/run/lock/schenker-power.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Another diagnostic/recovery command is running')
        output = dict(before=snapshot(), action='recover' if args.recover else 'test' if args.test else 'status')
        log = outdir / f'{time.strftime("%Y%m%d-%H%M%S")}-{os.getpid()}.json'
        def save():
            temp = log.with_suffix('.tmp')
            temp.write_text(json.dumps(output, indent=2)+'\n')
            temp.replace(log)
        save()
        try:
            if args.recover and (not active() or output['before']['priority'] != 'performance'):
                raise RuntimeError('Recovery requires an active policy and performance priority; it will not override charging gates')
            if args.test or args.recover:
                output['before_load'] = measurement(args.seconds)
                save()
            if args.recover:
                if output['before_load']['stop_reason']:
                    raise RuntimeError('Load test stopped early; recovery skipped')
                # Existing service performs charging -> freshly gated performance.
                subprocess.run(['systemctl', 'restart', SERVICE], check=True, timeout=20)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if active() and read(DEVICE / 'usb_c_power_priority') == 'performance':
                        break
                    time.sleep(0.5)
                else:
                    raise RuntimeError('Policy did not return to performance; inspect its status and source gates')
                deadline = time.monotonic() + 30
                while temperatures() >= 80 and time.monotonic() < deadline:
                    time.sleep(0.5)
                output['after_load'] = measurement(args.seconds)
                output['recovery_note'] = 'One requested priority reselection; measurements do not prove an EC root cause or permanent cure'
            output['after'] = snapshot()
        except (OSError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            output['error'] = str(exc) or 'Interrupted'
            save()
            print(f'Incomplete: {output["error"]}. Evidence: {log}')
            return 1
        save()
        if args.json:
            print(json.dumps(output, indent=2))
        else:
            show(output.get('after', output['before']))
            for key in ('before_load', 'after_load'):
                if key in output:
                    m = output[key]
                    print(f"{key}: median {m['median_package_w']} W; peak {m['peak_sample_package_w']} W; max {m['peak_temperature_c']} C; {m['stop_reason'] or 'completed'}")
            print(f'Evidence: {log}')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
