"""Desktop control for the USB-C policy; hardware access stays in the policy."""
import json
import logging
import os

from gi.repository import Gio, GLib

NAME = 'io.github.jerrinot.Schenker.Power'
PATH = '/io/github/jerrinot/Schenker/Power'
ACTION = 'io.github.jerrinot.schenker.usbc-performance'
CHARGING_ACTION = 'io.github.jerrinot.schenker.charging-profile'
CHARGING_PROFILES = ('trickle', 'standard', 'long_life')
INTERFACE = f'''<node><interface name="{NAME}">
  <method name="SetPerformancePreferred"><arg name="enabled" type="b" direction="in"/></method>
  <method name="SetChargingProfile"><arg name="profile" type="s" direction="in"/></method>
  <property name="PerformancePreferred" type="b" access="read"/>
  <property name="Priority" type="s" access="read"/>
  <property name="Reason" type="s" access="read"/>
  <property name="Description" type="s" access="read"/>
  <property name="SupplyWatts" type="d" access="read"/>
  <property name="BatteryPercent" type="u" access="read"/>
  <property name="Ready" type="b" access="read"/>
  <property name="ChargingProfile" type="s" access="read"/>
  <property name="DesiredChargingProfile" type="s" access="read"/>
  <property name="ChargingProfileError" type="s" access="read"/>
</interface></node>'''


class Preferences:
    def __init__(self, path, default_charging_profile=''):
        self.path = path
        if default_charging_profile not in ('', *CHARGING_PROFILES):
            raise ValueError('Invalid default charging profile')
        self.default_charging_profile = default_charging_profile
        try:
            value = json.loads(path.read_text())
        except FileNotFoundError:
            value = {'performance_preferred': True}
        if (not isinstance(value, dict) or 'performance_preferred' not in value or
                set(value) - {'performance_preferred', 'charging_profile'} or
                type(value['performance_preferred']) is not bool or
                ('charging_profile' in value and value['charging_profile'] not in CHARGING_PROFILES)):
            raise ValueError('Invalid power preferences')
        self.enabled = value['performance_preferred']
        self.charging_override = value.get('charging_profile')

    @property
    def charging_profile(self):
        return self.charging_override or self.default_charging_profile

    def set(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Performance preference must be a boolean')
        if enabled == self.enabled:
            return
        self.save(enabled, self.charging_override)

    def set_charging_profile(self, profile):
        if profile not in CHARGING_PROFILES:
            raise ValueError('Unknown charging profile')
        if profile != self.charging_override:
            self.save(self.enabled, profile)

    def save(self, enabled, charging_override):
        value = {'performance_preferred': enabled}
        if charging_override is not None:
            value['charging_profile'] = charging_override
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        try:
            with tmp.open('w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(value, stream)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            tmp.replace(self.path)
            # The rename has committed the choice. Keep memory consistent even
            # if syncing directory metadata subsequently fails.
            self.enabled = enabled
            self.charging_override = charging_override
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            tmp.unlink(missing_ok=True)


class PowerBus:
    def __init__(self, preferred, reconcile, disconnected, connection=None):
        self.preferred, self.reconcile = preferred, reconcile
        self.pending = False
        self.closed = False
        self.cancel = Gio.Cancellable()
        self.properties = {
            'PerformancePreferred': GLib.Variant('b', preferred.enabled),
            'Priority': GLib.Variant('s', ''),
            'Reason': GLib.Variant('s', 'starting'),
            'Description': GLib.Variant('s', 'Starting USB-C policy'),
            'SupplyWatts': GLib.Variant('d', 0),
            'BatteryPercent': GLib.Variant('u', 0),
            'Ready': GLib.Variant('b', False),
            'ChargingProfile': GLib.Variant('s', ''),
            'DesiredChargingProfile': GLib.Variant('s', preferred.charging_profile),
            'ChargingProfileError': GLib.Variant('s', ''),
        }
        self.connection = connection or Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.connection.set_exit_on_close(False)
        self.closed_signal = self.connection.connect('closed', lambda *_: disconnected())
        info = Gio.DBusNodeInfo.new_for_xml(INTERFACE).interfaces[0]
        self.registration = self.connection.register_object(
            PATH, info, self.call, lambda _c, _s, _p, _i, prop: self.properties[prop], None)
        result = self.connection.call_sync(
            'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
            'RequestName', GLib.Variant('(su)', (NAME, 4)), GLib.VariantType('(u)'),
            Gio.DBusCallFlags.NONE, 5000, None)
        if result.unpack()[0] != 1:
            self.connection.unregister_object(self.registration)
            self.connection.disconnect(self.closed_signal)
            raise RuntimeError('USB-C D-Bus name already owned')

    def publish(self, status):
        values = {
            'PerformancePreferred': status['performance_preferred'],
            'Priority': status['priority'], 'Reason': status['reason'],
            'Description': status['state'].partition(': ')[2],
            'SupplyWatts': float(status['telemetry']['supply_w']),
            'BatteryPercent': status['telemetry']['battery_pct'], 'Ready': True,
            'ChargingProfile': status['charging_profile'] or '',
            'DesiredChargingProfile': self.preferred.charging_profile,
            'ChargingProfileError': status['charging_profile_error'] or '',
        }
        changed = {name: GLib.Variant(self.properties[name].get_type_string(), value)
                   for name, value in values.items() if self.properties[name].unpack() != value}
        if changed:
            self.properties.update(changed)
            self.connection.emit_signal(None, PATH, 'org.freedesktop.DBus.Properties',
                                        'PropertiesChanged', GLib.Variant('(sa{sv}as)', (NAME, changed, [])))

    def call(self, _connection, sender, _path, _interface, method, parameters, invocation):
        if method not in ('SetPerformancePreferred', 'SetChargingProfile'):
            invocation.return_dbus_error(NAME + '.UnknownMethod', 'Unknown method')
            return
        if self.closed or self.pending:
            invocation.return_dbus_error(NAME + '.Busy', 'Another request is being checked; try again')
            return
        value, = parameters.unpack()
        if method == 'SetChargingProfile' and value not in CHARGING_PROFILES:
            invocation.return_dbus_error(NAME + '.InvalidProfile', 'Choose trickle, standard, or long_life')
            return
        self.pending = True
        # Use the bus-provided unique sender, never a caller-supplied PID or UID.
        subject = ('system-bus-name', {'name': GLib.Variant('s', sender)})
        action = CHARGING_ACTION if method == 'SetChargingProfile' else ACTION
        args = GLib.Variant('((sa{sv})sa{ss}us)', (subject, action, {}, 0, ''))
        self.connection.call(
            'org.freedesktop.PolicyKit1', '/org/freedesktop/PolicyKit1/Authority',
            'org.freedesktop.PolicyKit1.Authority', 'CheckAuthorization', args,
            GLib.VariantType('((bba{ss}))'), Gio.DBusCallFlags.NONE, 5000,
            self.cancel, self.authorized, (method, value, invocation))

    def authorized(self, connection, result, request):
        method, value, invocation = request
        self.pending = False
        try:
            allowed = connection.call_finish(result).unpack()[0][0]
            if self.closed:
                return
            if not allowed:
                invocation.return_dbus_error(NAME + '.NotAuthorized',
                                             'Only the active local user may change power preferences')
                return
            try:
                if method == 'SetChargingProfile':
                    self.preferred.set_charging_profile(value)
                else:
                    self.preferred.set(value)
            except OSError:
                # A directory-sync failure can happen after the atomic rename.
                # Publish the committed choice even though durability failed.
                committed = (self.preferred.charging_profile if method == 'SetChargingProfile'
                             else self.preferred.enabled)
                if committed == value:
                    self.reconcile()
                raise
            if not self.reconcile():
                raise RuntimeError('Preference saved, but hardware update failed')
            if method == 'SetChargingProfile' and self.properties['ChargingProfile'].unpack() != value:
                raise RuntimeError('Charging preference saved, but not yet applied by firmware')
            invocation.return_value(GLib.Variant('()', ()))
        except Exception as exc:
            if not self.closed:
                logging.warning('USB-C preference request failed: %s', exc)
                invocation.return_dbus_error(NAME + '.Failed', str(exc))

    def close(self):
        self.closed = True
        self.cancel.cancel()
        self.connection.unregister_object(self.registration)
        self.connection.disconnect(self.closed_signal)
        if not self.connection.is_closed():
            self.connection.call_sync(
                'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                'ReleaseName', GLib.Variant('(s)', (NAME,)), None,
                Gio.DBusCallFlags.NONE, 1000, None)
