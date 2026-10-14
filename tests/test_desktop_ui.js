import {presentation, chargingPresentation} from '../gnome-extension/presentation.js';

function assert(value, message) {
    if (!value)
        throw new Error(message);
}

const status = {Ready: true, PerformancePreferred: true, Priority: 'performance',
    Reason: 'ready', Description: '100 W supply'};
assert(presentation(status).subtitle === 'Performance active', 'Active performance');
assert(presentation({...status, Priority: 'charging', Reason: 'supply'}).subtitle ===
    'Waiting for suitable charger', 'Preference must not imply active performance');
assert(presentation({...status, Priority: 'charging', Reason: 'battery'}).subtitle ===
    'Waiting for battery recovery', 'Battery fallback');
assert(presentation({...status, Priority: 'charging', PerformancePreferred: false}).subtitle ===
    'Charging priority', 'Explicit charging choice');
assert(presentation(null).priority === 'Unavailable', 'Lost service must clear active priority');
assert(presentation({...status, Ready: false}).detail === '', 'Starting service must not show stale telemetry');
const charging = {Ready: true, ChargingProfile: 'trickle', DesiredChargingProfile: 'trickle',
    ChargingProfileError: ''};
assert(chargingPresentation(charging).title === 'Charging profile: Trickle', 'Active charging profile');
const pending = chargingPresentation({...charging, DesiredChargingProfile: 'standard'});
assert(pending.title === 'Charging profile: Trickle' && pending.selected === 'standard' && pending.detail,
    'Requested profile must not imply firmware success');
const failed = chargingPresentation({...charging, ChargingProfile: '', ChargingProfileError: 'write failed'});
assert(failed.title === 'Charging profile: Unavailable' && failed.detail, 'Profile write failure is visible');
assert(chargingPresentation(null).selected === '', 'Disconnected profile selection is cleared');
print('PASS: Quick Settings presentation');
