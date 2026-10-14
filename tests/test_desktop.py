"""Exercise preference storage and real D-Bus messages on an isolated bus."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gi.repository import Gio, GLib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'userspace'))
import schenker_usbc_dbus as d
from test_usb_policy import FakeHardware, p


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'preference.json'

    def test_default_and_restart_persistence(self):
        pref = d.Preferences(self.path)
        self.assertTrue(pref.enabled)
        pref.set(False)
        self.assertFalse(d.Preferences(self.path).enabled)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        pref.set(True)
        self.assertTrue(d.Preferences(self.path).enabled)

    def test_invalid_preference_is_not_silently_reset(self):
        for value in ('invalid json', 'null', '{}', '{"performance_preferred": "false"}'):
            self.path.write_text(value)
            with self.assertRaises(ValueError):
                d.Preferences(self.path)

    def test_failed_write_keeps_previous_choice(self):
        pref = d.Preferences(self.path)
        pref.set(False)
        with patch.object(os, 'fsync', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                pref.set(True)
        self.assertFalse(pref.enabled)
        self.assertFalse(d.Preferences(self.path).enabled)
        self.assertFalse(self.path.with_suffix('.tmp').exists())

    def test_existing_preferences_use_configured_profile_until_user_selects(self):
        self.path.write_text('{"performance_preferred": false}')
        pref = d.Preferences(self.path, 'trickle')
        self.assertEqual(pref.charging_profile, 'trickle')
        pref.set(True)
        self.assertEqual(d.Preferences(self.path, 'standard').charging_profile, 'standard')
        pref.set_charging_profile('long_life')
        self.assertEqual(d.Preferences(self.path, 'trickle').charging_profile, 'long_life')
        pref.set(False)
        restored = d.Preferences(self.path, 'standard')
        self.assertFalse(restored.enabled)
        self.assertEqual(restored.charging_profile, 'long_life')

    def test_selecting_current_default_pins_profile(self):
        pref = d.Preferences(self.path, 'trickle')
        pref.set_charging_profile('trickle')
        self.assertEqual(d.Preferences(self.path, 'standard').charging_profile, 'trickle')

    def test_invalid_saved_charging_profile_fails_closed(self):
        self.path.write_text('{"performance_preferred": true, "charging_profile": "fast"}')
        with self.assertRaises(ValueError):
            d.Preferences(self.path)

    def test_charging_change_is_immediate_and_does_not_change_power_preference(self):
        pref = d.Preferences(self.path, 'trickle')
        pref.set(False)
        hw = FakeHardware()
        hw.snapshot = lambda: dict(supply_w=100, battery_pct=80, discharge_w=0, temp_c=50)
        hw.charging_profile = 'standard'
        hw.get_charge_profile = lambda: hw.charging_profile
        hw.set_charge_profile = lambda value: setattr(hw, 'charging_profile', value)
        controller = p.Controller(hw, p.Journal(Path(self.tmp.name) / 'caps.json'))
        policy = p.Policy(hw, controller, pref)
        self.assertEqual(policy.update()['charging_profile'], 'trickle')
        self.assertGreater(policy.charge.next_restore, 0)
        pref.set_charging_profile('long_life')
        status = policy.update()
        self.assertEqual(status['charging_profile'], 'long_life')
        self.assertEqual(status['desired_charging_profile'], 'long_life')
        self.assertFalse(status['performance_preferred'])
        self.assertEqual(hw.priority, 'charging')
        def fail_write(_value):
            raise OSError('firmware write failed')

        hw.set_charge_profile = fail_write
        pref.set_charging_profile('standard')
        status = policy.update()
        self.assertIn('firmware write failed', status['charging_profile_error'])
        self.assertIsNone(status['charging_profile'])
        self.assertEqual(hw.priority, 'charging')

    def test_switch_and_gate_remain_independent(self):
        pref = d.Preferences(self.path)
        hw = FakeHardware()
        sample = dict(supply_w=100, battery_pct=80, discharge_w=0, temp_c=50)
        hw.snapshot = lambda: sample
        hw.get_charge_profile = lambda: 'trickle'
        controller = p.Controller(hw, p.Journal(Path(self.tmp.name) / 'caps.json'))
        policy = p.Policy(hw, controller, pref)
        self.assertEqual(policy.update()['priority'], 'performance')
        pref.set(False)
        self.assertEqual(policy.update()['reason'], 'user')
        self.assertEqual(hw.priority, 'charging')
        sample['supply_w'] = 65
        pref.set(True)
        status = policy.update()
        self.assertTrue(status['performance_preferred'])
        self.assertEqual(status['reason'], 'supply')
        self.assertEqual(hw.priority, 'charging')
        sample['supply_w'] = 100
        self.assertEqual(policy.update()['priority'], 'performance')


AUTH_XML = '''<node><interface name="org.freedesktop.PolicyKit1.Authority">
<method name="CheckAuthorization">
<arg type="(sa{sv})" direction="in"/><arg type="s" direction="in"/>
<arg type="a{ss}" direction="in"/><arg type="u" direction="in"/>
<arg type="s" direction="in"/><arg type="(bba{ss})" direction="out"/>
</method></interface></node>'''


class BusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.daemon = subprocess.Popen(
            ['dbus-daemon', '--session', '--nofork', '--print-address=1'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self.stop_daemon)
        address = self.daemon.stdout.readline().strip()
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        self.connections = [Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
                            for _ in range(3)]
        self.addCleanup(self.close_connections)
        self.service, self.auth, self.client = self.connections
        self.auth.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                            'org.freedesktop.DBus', 'RequestName',
                            GLib.Variant('(su)', ('org.freedesktop.PolicyKit1', 4)),
                            None, Gio.DBusCallFlags.NONE, 1000, None)
        self.allow, self.hold = True, False
        self.held = None
        self.auth.register_object('/org/freedesktop/PolicyKit1/Authority',
                                  Gio.DBusNodeInfo.new_for_xml(AUTH_XML).interfaces[0],
                                  self.check_auth, None, None)
        self.pref = d.Preferences(Path(self.tmp.name) / 'preference.json')
        self.updates = 0
        self.expected_action = d.ACTION
        self.charging_failure = False
        self.bus = d.PowerBus(self.pref, self.update, lambda: None, self.service)
        self.addCleanup(lambda: self.bus.close())
        self.update()

    def stop_daemon(self):
        self.daemon.terminate()
        self.daemon.wait(timeout=5)
        self.daemon.stdout.close()

    def close_connections(self):
        for conn in self.connections:
            conn.close_sync(None)

    def update(self):
        self.updates += 1
        self.bus.publish(dict(performance_preferred=self.pref.enabled,
                              priority='performance' if self.pref.enabled else 'charging',
                              reason='ready' if self.pref.enabled else 'user',
                              state='test: selected', telemetry=dict(supply_w=100, battery_pct=80),
                              charging_profile=None if self.charging_failure else self.pref.charging_profile or 'standard',
                              charging_profile_error='write failed' if self.charging_failure else None))
        return True

    def check_auth(self, _c, _s, _p, _i, _m, params, invocation):
        subject, action, details, flags, cancellation = params.unpack()
        self.assertEqual(subject, ('system-bus-name', {'name': self.client.get_unique_name()}))
        self.assertEqual(action, self.expected_action)
        self.assertEqual((details, flags, cancellation), ({}, 0, ''))
        if self.hold:
            self.held = invocation
        else:
            invocation.return_value(GLib.Variant('((bba{ss}))', ((self.allow, False, {}),)))

    def call(self, method, params, interface=d.NAME):
        loop = GLib.MainLoop()
        outcome = []

        def finished(connection, result):
            try:
                outcome.append(connection.call_finish(result))
            except GLib.Error as exc:
                outcome.append(exc)
            loop.quit()

        self.client.call(d.NAME, d.PATH, interface, method, params, None,
                         Gio.DBusCallFlags.NONE, 1500, None, finished)
        loop.run()
        if isinstance(outcome[0], Exception):
            raise outcome[0]
        return outcome[0].unpack()

    def set_preference(self, enabled):
        return self.call('SetPerformancePreferred', GLib.Variant('(b)', (enabled,)))

    def set_charging_profile(self, profile):
        self.expected_action = d.CHARGING_ACTION
        return self.call('SetChargingProfile', GLib.Variant('(s)', (profile,)))

    def properties(self):
        return self.call('GetAll', GLib.Variant('(s)', (d.NAME,)), 'org.freedesktop.DBus.Properties')[0]

    def test_authorized_change_publishes_and_survives_service_restart(self):
        signals = []
        subscription = self.client.signal_subscribe(
            None, 'org.freedesktop.DBus.Properties', 'PropertiesChanged', d.PATH,
            None, Gio.DBusSignalFlags.NONE, lambda *args: signals.append(args[-1].unpack()))
        self.addCleanup(lambda: self.client.signal_unsubscribe(subscription))
        self.set_preference(False)
        self.assertFalse(self.properties()['PerformancePreferred'])
        self.assertEqual(self.properties()['Priority'], 'charging')
        self.assertTrue(signals)
        self.bus.close()
        self.pref = d.Preferences(self.pref.path)
        self.bus = d.PowerBus(self.pref, self.update, lambda: None, self.service)
        self.update()
        self.assertFalse(self.properties()['PerformancePreferred'])

    def test_denied_request_never_writes_or_reconciles(self):
        self.allow = False
        with self.assertRaisesRegex(GLib.Error, 'NotAuthorized'):
            self.set_preference(False)
        self.assertTrue(self.pref.enabled)
        self.assertFalse(self.pref.path.exists())
        self.assertEqual(self.updates, 1)

    def test_authorization_wait_does_not_block_policy_updates(self):
        self.hold = True

        def tick():
            self.update()
            if self.held:
                self.held.return_value(GLib.Variant('((bba{ss}))', ((True, False, {}),)))
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(20, tick)
        self.set_preference(False)
        self.assertGreaterEqual(self.updates, 3)

    def test_storage_failure_is_reported_without_false_success(self):
        with patch.object(self.pref, 'set', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(GLib.Error, 'disk full'):
                self.set_preference(False)
        self.assertTrue(self.properties()['PerformancePreferred'])
        self.assertEqual(self.updates, 1)

    def test_directory_sync_failure_reports_committed_preference(self):
        with patch.object(os, 'fsync', side_effect=[None, OSError('directory sync failed')]):
            with self.assertRaisesRegex(GLib.Error, 'directory sync failed'):
                self.set_preference(False)
        self.assertFalse(self.properties()['PerformancePreferred'])
        self.assertFalse(d.Preferences(self.pref.path).enabled)

    def test_properties_are_read_only(self):
        with self.assertRaises(GLib.Error):
            self.call('Set', GLib.Variant('(ssv)', (d.NAME, 'PerformancePreferred',
                                                   GLib.Variant('b', False))),
                      'org.freedesktop.DBus.Properties')
        self.assertTrue(self.pref.enabled)

    def test_charging_selection_is_authorized_and_persisted(self):
        self.set_preference(False)
        for profile in d.CHARGING_PROFILES:
            self.set_charging_profile(profile)
            status = self.properties()
            self.assertEqual(status['ChargingProfile'], profile)
            self.assertEqual(status['DesiredChargingProfile'], profile)
            self.assertFalse(status['PerformancePreferred'])
        self.assertEqual(d.Preferences(self.pref.path).charging_profile, 'long_life')

    def test_denied_charging_selection_never_writes(self):
        self.allow = False
        with self.assertRaisesRegex(GLib.Error, 'NotAuthorized'):
            self.set_charging_profile('standard')
        self.assertFalse(self.pref.path.exists())
        self.assertEqual(self.updates, 1)

    def test_invalid_charging_selection_never_writes(self):
        with self.assertRaisesRegex(GLib.Error, 'InvalidProfile'):
            self.set_charging_profile('fast')
        self.assertFalse(self.pref.path.exists())
        self.assertEqual(self.updates, 1)

    def test_failed_firmware_profile_write_keeps_desired_and_actual_distinct(self):
        self.charging_failure = True
        with self.assertRaisesRegex(GLib.Error, 'not yet applied'):
            self.set_charging_profile('long_life')
        status = self.properties()
        self.assertEqual(status['DesiredChargingProfile'], 'long_life')
        self.assertEqual(status['ChargingProfile'], '')
        self.assertEqual(status['ChargingProfileError'], 'write failed')
        self.assertTrue(status['PerformancePreferred'])
        self.charging_failure = False
        self.update()
        self.assertEqual(self.properties()['ChargingProfile'], 'long_life')
