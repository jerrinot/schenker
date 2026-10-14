import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('policy', Path(__file__).resolve().parents[1] / 'userspace' / 'schenker-usbc-policy.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class FakeHardware:
    def __init__(self):
        self.priority = 'charging'
        self.values = {'0': 115_000_000, '1': 115_000_000}
        self.events = []
        self.enabled = True
        self.locked = False
        self.fail_restore = False
        self.fail_cap = None

    def get_priority(self):
        return self.priority

    def set_priority(self, value):
        self.events.append(('priority', value))
        if value == 'charging' and self.fail_restore:
            raise OSError('firmware write failed')
        if value == 'performance':
            assert self.enabled and all(n > 0 for n in self.values.values())
            if not p.FIRMWARE_LIMITS:
                assert all(n <= p.CAP_UW for n in self.values.values())
        self.priority = value

    def caps(self):
        return self.values.copy()

    def set_cap(self, index, value):
        self.events.append(('cap', index, value))
        if index == self.fail_cap:
            self.fail_cap = None
            raise OSError('cap write failed')
        if not self.locked:
            self.values[index] = value

    def caps_enabled(self):
        return self.enabled


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = p.Journal(Path(self.tmp.name) / 'caps.json')
        self.hw = FakeHardware()
        self.c = p.Controller(self.hw, self.journal)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cap_before_performance_and_charging_before_release(self):
        self.c.activate()
        self.assertEqual(self.hw.events[-1], ('priority', 'performance'))
        self.hw.events.clear()
        self.c.deactivate()
        self.assertEqual(self.hw.events[0], ('priority', 'charging'))
        self.assertEqual(self.hw.values, {'0': 115_000_000, '1': 115_000_000})
        self.assertIsNone(self.journal.load())

    def test_locked_caps_never_enable_performance(self):
        self.hw.locked = True
        with self.assertRaises(p.Conflict):
            self.c.activate()
        self.assertNotIn(('priority', 'performance'), self.hw.events)
        self.assertEqual(self.hw.priority, 'charging')

    def test_partial_cap_failure_recovers(self):
        self.hw.fail_cap = '1'
        with self.assertRaises(OSError):
            self.c.activate()
        self.c.deactivate()
        self.assertEqual(self.hw.values, {'0': 115_000_000, '1': 115_000_000})
        self.assertNotIn(('priority', 'performance'), self.hw.events)

    def test_disabled_caps_refuse(self):
        self.hw.enabled = False
        with self.assertRaises(RuntimeError):
            self.c.activate()
        self.assertEqual(self.hw.priority, 'charging')

    def test_respects_preexisting_lower_limit(self):
        self.hw.values = {'0': 30_000_000, '1': 35_000_000}
        self.c.activate()
        self.assertEqual(self.hw.values, {'0': 30_000_000, '1': 35_000_000})
        self.c.deactivate()
        self.assertEqual(self.hw.values, {'0': 30_000_000, '1': 35_000_000})

    def test_external_cap_change_backs_off_without_overwriting_it(self):
        self.c.activate()
        self.hw.values['0'] = 25_000_000
        with self.assertRaises(p.Conflict):
            self.c.activate()
        self.assertEqual(self.hw.priority, 'charging')
        self.assertEqual(self.hw.values['0'], 25_000_000)

    def test_failed_priority_restore_does_not_raise_caps(self):
        self.c.activate()
        self.hw.fail_restore = True
        with self.assertRaises(OSError):
            self.c.deactivate()
        self.assertEqual(self.hw.values, {'0': p.CAP_UW, '1': p.CAP_UW})
        self.assertIsNotNone(self.journal.load())

    def test_crash_journal_recovery(self):
        self.c.activate()
        replacement = p.Controller(self.hw, self.journal)
        replacement.deactivate()
        self.assertEqual(self.hw.priority, 'charging')
        self.assertEqual(self.hw.values, {'0': 115_000_000, '1': 115_000_000})

    def test_resume_after_firmware_reset_rechecks_caps(self):
        self.c.activate()
        self.hw.priority = 'charging'
        self.hw.values = {'0': 115_000_000, '1': 115_000_000}
        self.c.activate()
        self.assertEqual(self.hw.values, {'0': p.CAP_UW, '1': p.CAP_UW})
        self.assertEqual(self.hw.priority, 'performance')

    def test_bad_journal_rejected(self):
        self.journal.path.write_text('{"original": {"evil": 2}, "applied": {}}')
        with self.assertRaises(RuntimeError):
            p.Controller(self.hw, self.journal)

    def test_source_battery_and_discharge_gates(self):
        base = dict(supply_w=100, battery_pct=100, discharge_w=0, temp_c=50)
        for change in ({'supply_w': 65}, {'supply_w': 0}, {'battery_pct': 29}, {'discharge_w': 4}):
            self.assertTrue(p.Gate().evaluate(base | change, 0).startswith('charging:'))
        self.assertTrue(p.Gate().evaluate(base, 0).startswith('performance:'))

    def test_thermal_hysteresis_and_minimum_cooldown(self):
        g = p.Gate()
        s = dict(supply_w=100, battery_pct=100, discharge_w=0, temp_c=94)
        self.assertIn('cooldown', g.evaluate(s, 0))
        self.assertIn('cooldown', g.evaluate(s | {'temp_c': 79}, 30))
        self.assertIn('cooldown', g.evaluate(s | {'temp_c': 85}, 70))
        self.assertTrue(g.evaluate(s | {'temp_c': 79}, 71).startswith('performance:'))

    def test_battery_hysteresis(self):
        g = p.Gate()
        s = dict(supply_w=100, battery_pct=29, discharge_w=0, temp_c=50)
        self.assertIn('low battery', g.evaluate(s, 0))
        self.assertIn('low battery', g.evaluate(s | {'battery_pct': 33}, 5))
        self.assertTrue(g.evaluate(s | {'battery_pct': 35}, 10).startswith('performance:'))

    def test_firmware_limits_never_written_and_external_changes_preserved(self):
        with patch.object(p, 'FIRMWARE_LIMITS', True):
            self.hw.locked = True
            self.c.activate()
            self.assertIsNone(self.journal.load())
            self.hw.values['0'] = 32_000_000
            self.c.activate()
            self.c.deactivate()
            self.assertEqual(self.hw.values, {'0': 32_000_000, '1': 115_000_000})
            self.assertFalse(any(e[0] == 'cap' for e in self.hw.events))

    def test_firmware_profile_recovers_old_capped_session(self):
        self.c.activate()
        with patch.object(p, 'FIRMWARE_LIMITS', True):
            replacement = p.Controller(self.hw, self.journal)
            replacement.activate()
            self.assertEqual(self.hw.values, {'0': 115_000_000, '1': 115_000_000})
            self.assertEqual(self.hw.priority, 'performance')
            self.assertIsNone(self.journal.load())

    def test_firmware_recovery_preserves_external_limit(self):
        self.c.activate()
        self.hw.values['0'] = 32_000_000
        with patch.object(p, 'FIRMWARE_LIMITS', True):
            p.Controller(self.hw, self.journal).activate()
            self.assertEqual(self.hw.values, {'0': 32_000_000, '1': 115_000_000})

    def test_firmware_profile_refuses_disabled_limits(self):
        with patch.object(p, 'FIRMWARE_LIMITS', True):
            self.hw.enabled = False
            with self.assertRaises(RuntimeError):
                self.c.activate()
            self.assertEqual(self.hw.priority, 'charging')

    def test_firmware_profile_uses_native_thermal_control_but_keeps_source_gates(self):
        with patch.object(p, 'FIRMWARE_LIMITS', True):
            s = dict(supply_w=100, battery_pct=100, discharge_w=0, temp_c=98)
            self.assertTrue(p.Gate().evaluate(s, 0).startswith('performance:'))
            for change in ({'supply_w':65}, {'battery_pct':29}, {'discharge_w':4}):
                self.assertTrue(p.Gate().evaluate(s | change, 0).startswith('charging:'))

    def test_performance_write_requires_valid_leased_driver(self):
        with patch.object(p.Hardware, 'validate_driver', side_effect=RuntimeError('wrong driver')):
            with patch.object(Path, 'write_text') as write:
                with self.assertRaises(RuntimeError):
                    p.Hardware().set_priority('performance')
                write.assert_not_called()

    def test_charge_preference_restores_once_and_after_reset(self):
        self.hw.charge = 'standard'
        self.hw.get_charge_profile = lambda: self.hw.charge
        writes = []
        def set_charge(value):
            writes.append(value)
            self.hw.charge = value
        self.hw.set_charge_profile = set_charge
        c = p.ChargePreference('trickle')
        self.assertEqual(c.reconcile(self.hw, 0), 'trickle')
        for now in range(1, 60):
            c.reconcile(self.hw, now)
        self.assertEqual(writes, ['trickle'])
        self.hw.charge = 'standard'
        self.assertEqual(c.reconcile(self.hw, 61), 'trickle')
        self.assertEqual(writes, ['trickle', 'trickle'])
        self.assertFalse(self.hw.events)  # No CPU cap or USB priority writes.

    def test_charge_restore_failure_is_rate_limited(self):
        self.hw.get_charge_profile = lambda: 'standard'
        calls = []
        def fail(value):
            calls.append(value)
            raise OSError('simulated firmware error')
        self.hw.set_charge_profile = fail
        c = p.ChargePreference('trickle')
        with self.assertRaises(OSError):
            c.reconcile(self.hw, 0)
        for now in range(1, 30):
            c.reconcile(self.hw, now)
        self.assertEqual(calls, ['trickle'])
        with self.assertRaises(OSError):
            c.reconcile(self.hw, 30)
        self.assertEqual(calls, ['trickle', 'trickle'])

    def test_unmanaged_charge_profile_never_writes(self):
        self.hw.get_charge_profile = lambda: 'standard'
        self.hw.set_charge_profile = lambda value: self.fail('Unexpected write')
        self.assertEqual(p.ChargePreference('').reconcile(self.hw, 0), 'standard')


class DriverInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        device = root / 'device'
        device.mkdir()
        (device / 'driver').symlink_to(root / 'drivers' / 'schenker-kbd')
        self.priority = device / 'usb_c_power_priority'
        self.priority.write_text('charging')
        self.module = root / 'module'
        (self.module / 'parameters').mkdir(parents=True)
        (self.module / 'parameters/usb_c_policy').write_text('Y')
        for name, value in [('PRIORITY', self.priority), ('MODULE', self.module),
                            ('CHARGE_PROFILE', device / 'charging_profile')]:
            patcher = patch.object(p, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_compatible_driver_does_not_need_version_allowlist(self):
        (self.module / 'version').write_text('99.0.0')
        p.Hardware().validate_driver()

    def test_missing_interface_rejected(self):
        self.priority.unlink()
        with self.assertRaises(RuntimeError):
            p.Hardware().validate_driver()

    def test_disabled_lease_rejected(self):
        (self.module / 'parameters/usb_c_policy').write_text('N')
        with self.assertRaises(RuntimeError):
            p.Hardware().validate_driver()

    def test_different_driver_rejected(self):
        link = self.priority.parent / 'driver'
        link.unlink()
        link.symlink_to('/drivers/different-driver')
        with self.assertRaises(RuntimeError):
            p.Hardware().validate_driver()

    def test_missing_charge_profile_rejected_before_write(self):
        with self.assertRaisesRegex(RuntimeError, 'charging profiles'):
            p.Hardware().set_charge_profile('trickle')
        self.assertFalse(p.CHARGE_PROFILE.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
