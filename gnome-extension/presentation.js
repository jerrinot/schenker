// Pure presentation logic shared by the tile and its hardware-independent tests.
export const chargingProfiles = {
    trickle: 'Trickle',
    standard: 'Standard',
    long_life: 'Long Life',
};

export function chargingPresentation(status) {
    if (!status?.Ready)
        return {title: 'Charging profile: Unavailable', selected: '', detail: ''};
    const actual = chargingProfiles[status.ChargingProfile] ?? 'Unavailable';
    const desired = status.DesiredChargingProfile;
    let detail = '';
    if (status.ChargingProfileError)
        detail = 'Could not read or apply the charging profile; retrying.';
    else if (desired && desired !== status.ChargingProfile)
        detail = `Waiting to apply ${chargingProfiles[desired] ?? desired}.`;
    return {
        title: `Charging profile: ${actual}`,
        selected: desired || status.ChargingProfile,
        detail,
    };
}

export function presentation(status) {
    if (!status?.Ready)
        return {subtitle: 'Service unavailable', priority: 'Unavailable', detail: ''};

    const priority = status.Priority === 'performance' ? 'Performance' : 'Charging';
    const subtitles = {
        supply: 'Waiting for suitable charger',
        battery: 'Waiting for battery recovery',
        discharge: 'Battery supplying extra power',
        temperature: 'Waiting for cooldown',
    };
    const details = {
        ready: 'Cable rating is unverified.',
        user: 'More USB-C power is reserved for charging.',
        supply: 'A reported USB-C budget of at least 100 W is required.',
        battery: 'Performance resumes when the battery reaches 35%.',
        discharge: 'The battery is supplying over 3 W of extra power.',
        temperature: 'Waiting for the CPU to cool down.',
    };
    return {
        subtitle: status.Priority === 'performance' ? 'Performance active'
            : !status.PerformancePreferred ? 'Charging priority'
                : subtitles[status.Reason] ?? 'Performance temporarily unavailable',
        priority,
        detail: details[status.Reason] ?? 'Waiting for the power policy.',
    };
}
