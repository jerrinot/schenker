import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';

import {presentation, chargingPresentation, chargingProfiles} from './presentation.js';

const BUS_NAME = 'io.github.jerrinot.Schenker.Power';
const BUS_PATH = '/io/github/jerrinot/Schenker/Power';
const PROPERTIES = ['PerformancePreferred', 'Priority', 'Reason', 'Description',
    'SupplyWatts', 'BatteryPercent', 'Ready', 'ChargingProfile',
    'DesiredChargingProfile', 'ChargingProfileError'];

const PowerToggle = GObject.registerClass(class PowerToggle extends QuickMenuToggle {
    constructor() {
        super({
            title: 'USB-C Performance',
            subtitle: 'Connecting…',
            iconName: 'power-profile-performance-symbolic',
            toggleMode: false,
        });
        this._cancel = new Gio.Cancellable();
        this._proxy = null;
        this._pending = false;
        this._error = null;
        this._signals = [];
        this.menu.setHeader('power-profile-performance-symbolic', 'USB-C Performance',
            'CPU performance or battery charging');
        this._rows = {};
        for (const name of ['preference', 'priority', 'supply', 'battery', 'detail']) {
            const row = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
            row.set_style('padding-top: 4px; padding-bottom: 4px;');
            this.menu.addMenuItem(row);
            this._rows[name] = row;
        }
        this._rows.detail.label.clutter_text.line_wrap = true;
        this._rows.detail.label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        this._rows.detail.label.set_style('max-width: 24em;');
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._charging = new PopupMenu.PopupSubMenuMenuItem('Charging profile');
        this.menu.addMenuItem(this._charging);
        this._chargingItems = {};
        for (const [profile, label] of Object.entries(chargingProfiles)) {
            this._chargingItems[profile] = this._charging.menu.addAction(label, () =>
                this._request('SetChargingProfile', new GLib.Variant('(s)', [profile])));
        }
        this._chargingStatus = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
        this._chargingStatus.label.clutter_text.line_wrap = true;
        this._chargingStatus.label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        this._chargingStatus.label.set_style('max-width: 24em;');
        this.menu.addMenuItem(this._chargingStatus);
        this.connect('clicked', () => this._toggle());
        this._refresh();
        Gio.DBusProxy.new_for_bus(Gio.BusType.SYSTEM,
            Gio.DBusProxyFlags.DO_NOT_AUTO_START, null, BUS_NAME, BUS_PATH, BUS_NAME,
            this._cancel, (_source, result) => {
                try {
                    const proxy = Gio.DBusProxy.new_for_bus_finish(result);
                    if (this._cancel.is_cancelled())
                        return;
                    this._proxy = proxy;
                    this._signals.push(proxy.connect('g-properties-changed', (_proxy, changes) => {
                        if (Object.hasOwn(changes.deepUnpack(), 'ChargingProfile'))
                            this._error = null;
                        this._refresh();
                    }));
                    this._signals.push(proxy.connect('notify::g-name-owner', () => {
                        this._error = null;
                        this._refresh();
                    }));
                    this._refresh();
                } catch (error) {
                    if (!this._cancel.is_cancelled()) {
                        console.error('Schenker USB-C connection failed', error);
                        this._error = 'Cannot connect to USB-C service';
                        this._refresh();
                    }
                }
            });
    }

    _status() {
        if (!this._proxy?.g_name_owner)
            return null;
        const status = {};
        for (const name of PROPERTIES) {
            const value = this._proxy.get_cached_property(name);
            if (value === null)
                return null;
            status[name] = value.deepUnpack();
        }
        return status;
    }

    _refresh() {
        const status = this._status();
        const view = presentation(status);
        // Highlight represents the saved choice; the subtitle shows actual state.
        this.checked = status?.PerformancePreferred ?? false;
        this.reactive = Boolean(status?.Ready) && !this._pending;
        this.subtitle = this._pending ? 'Applying…' : this._error ? 'Change failed' : view.subtitle;
        this.accessible_name = `${this.title}: ${this.subtitle}`;
        this._rows.preference.label.text = `Preference: ${status?.Ready
            ? status.PerformancePreferred ? 'Enabled' : 'Disabled' : 'Unavailable'}`;
        this._rows.priority.label.text = `Active priority: ${view.priority}`;
        this._rows.supply.label.text = status?.Ready
            ? `Reported USB-C power: ${Math.round(status.SupplyWatts)} W` : '';
        this._rows.battery.label.text = status?.Ready ? `Battery: ${status.BatteryPercent}%` : '';
        this._rows.detail.label.text = this._error ?? view.detail;
        for (const row of Object.values(this._rows))
            row.visible = row.label.text.length > 0;
        const charging = chargingPresentation(status);
        this._charging.label.text = charging.title;
        this._charging.setSensitive(Boolean(status?.Ready) && !this._pending);
        this._chargingStatus.label.text = charging.detail;
        this._chargingStatus.visible = charging.detail.length > 0;
        for (const [profile, item] of Object.entries(this._chargingItems))
            item.setOrnament(profile === charging.selected ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
    }

    _toggle() {
        const status = this._status();
        if (status?.Ready)
            this._request('SetPerformancePreferred', new GLib.Variant('(b)', [!status.PerformancePreferred]));
    }

    _request(method, parameters) {
        const status = this._status();
        if (!status?.Ready || this._pending)
            return;
        this._pending = true;
        this._error = null;
        this._refresh();
        // Address the unique owner so a restart cannot redirect an old request.
        this._proxy.get_connection().call(this._proxy.g_name_owner, BUS_PATH, BUS_NAME,
            method, parameters,
            null, Gio.DBusCallFlags.NO_AUTO_START, 10000, this._cancel,
            (connection, result) => {
                try {
                    connection.call_finish(result);
                } catch (error) {
                    if (!this._cancel.is_cancelled()) {
                        this._error = error.matches(Gio.DBusError, Gio.DBusError.ACCESS_DENIED) ||
                            Gio.DBusError.get_remote_error(error)?.endsWith('.NotAuthorized')
                            ? 'Only the active local user can change this setting'
                            : 'Could not change preference; check the USB-C service';
                        console.warn('Schenker USB-C preference failed', error.message);
                    }
                }
                if (!this._cancel.is_cancelled()) {
                    this._pending = false;
                    this._refresh();
                }
            });
    }

    destroy() {
        this._cancel.cancel();
        for (const id of this._signals)
            this._proxy.disconnect(id);
        this._signals = [];
        this._proxy = null;
        super.destroy();
    }
});

export default class SchenkerPowerExtension extends Extension {
    enable() {
        this._indicator = new SystemIndicator();
        this._toggle = new PowerToggle();
        this._indicator.quickSettingsItems.push(this._toggle);
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator, 2);
    }

    disable() {
        this._toggle?.destroy();
        this._toggle = null;
        this._indicator?.destroy();
        this._indicator = null;
    }
}
