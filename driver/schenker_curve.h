/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Caller holds kbd->lock. Both fans use the EC 1.47 CPU curve.
 * Table layout: 16 rising thresholds, 16 falling thresholds, 16 PWM outputs.
 * The default extends the ROM curve with two higher-output hot stages.
 */
static const u8 schenker_curve_default[48] = {
	54,60,65,67,70,72,74,76,79,85,90,255,255,255,255,255,
	0,45,57,64,66,69,71,73,75,77,82,87,255,255,255,255,
	0,50,60,70,100,120,140,160,160,170,190,200,200,200,200,200
};

static int curve_flag_locked(struct schenker_kbd *kbd, u16 address, u8 mask, bool on)
{
	u8 old, next, actual;
	int ret, i;

	for (i = 0; i < 3; i++) {
		ret = schenker_ec_read(kbd, address, &old);
		if (ret)
			continue;
		next = (old & ~mask) | (on ? mask : 0);
		if (old == next)
			return 0;
		ret = schenker_curve_write(kbd, address, next);
		if (!ret)
			ret = schenker_ec_read(kbd, address, &actual);
		if (!ret && (actual & mask) == (next & mask))
			return 0;
	}
	return -EIO;
}

static int curve_table_write_locked(struct schenker_kbd *kbd, const u8 *table)
{
	u8 actual;
	int i, attempt, ret;

	for (i = 0; i < 48; i++) {
		for (attempt = 0; attempt < 3; attempt++) {
			ret = schenker_curve_write(kbd, 0xf00 + i, table[i]);
			if (!ret)
				ret = schenker_ec_read(kbd, 0xf00 + i, &actual);
			if (!ret && actual == table[i])
				break;
		}
		if (attempt == 3)
			return -EIO;
	}
	for (i = 0; i < 48; i++) {
		ret = schenker_ec_read(kbd, 0xf00 + i, &actual);
		if (ret || actual != table[i])
			return -EIO;
	}
	return 0;
}

static int curve_restore_locked(struct schenker_kbd *kbd)
{
	int ret;

	if (!kbd->curve_owned)
		return 0;
	/* Never overwrite table RAM until both custom selectors are verified off. */
	ret = curve_flag_locked(kbd, 0x7c6, BIT(2), false);
	if (ret)
		return ret;
	ret = curve_flag_locked(kbd, 0x7c5, BIT(7), false);
	if (ret)
		return ret;
	kbd->curve_active = false;
	ret = curve_table_write_locked(kbd, kbd->curve_saved);
	if (!ret)
		kbd->curve_owned = false;
	return ret;
}

static int curve_apply_locked(struct schenker_kbd *kbd)
{
	const u16 guards[] = { 0x456, 0x751, 0x771, 0x78e, 0x7c5, 0x7c6,
			       0x7d3, 0xf5d, 0xf5e };
	u8 v[ARRAY_SIZE(guards)], effective[48];
	int i, ret;

	if (kbd->curve_mode == 3)
		return 0;
	if (kbd->curve_owned)
		return -EBUSY;
	memcpy(effective, kbd->curve_config, sizeof(effective));
	if (kbd->curve_mode == 0)
		memset(effective + 32, 200, 16);
	for (i = 0; i < ARRAY_SIZE(guards); i++) {
		ret = schenker_ec_read(kbd, guards[i], &v[i]);
		if (ret)
			return ret;
	}
	/* Only known CPU-only routing; no conflicting fan mode or other owner. */
	if ((v[0] & BIT(7)) || v[1] || v[2] || !(v[3] & BIT(6)) ||
	    (v[4] & BIT(7)) || (v[5] & BIT(2)) || (v[6] & BIT(0)) ||
	    (v[7] == 0xfd && v[8] == 0xc9))
		return -EBUSY;
	for (i = 0; i < 48; i++) {
		ret = schenker_ec_read(kbd, 0xf00 + i, &kbd->curve_saved[i]);
		if (ret)
			return ret;
	}
	/* Recovery owns a complete snapshot before any write can take effect. */
	kbd->curve_owned = true;
	ret = manual_set_locked(kbd, true);
	if (!ret)
		ret = curve_table_write_locked(kbd, effective);
	if (!ret)
		ret = curve_flag_locked(kbd, 0x7c5, BIT(7), true);
	if (!ret)
		ret = curve_flag_locked(kbd, 0x7c6, BIT(2), true);
	if (!ret)
		kbd->curve_active = true;
	/* Caller retains ownership and performs cleanup, including write failures. */
	return ret;
}

/* Twelve exposed states: state0 is the fixed off baseline; states12..15
 * are unreachable sentinels. PointN maps stateN-1, entering from up[N-2]
 * and leaving via down[N-1]. Threshold comparisons are strictly > and <.
 */
static int curve_validate(const u8 *table)
{
	int s;

	if (table[16] || table[32] || table[43] != 200)
		return -EINVAL;
	for (s = 1; s <= 11; s++) {
		if (table[s - 1] < 20 || table[s - 1] > 95 ||
		    table[16 + s] < 1 || table[16 + s] >= table[s - 1] ||
		    table[32 + s] < 50 || table[32 + s] > 200)
			return -EINVAL;
		if (s > 1 && (table[s - 1] <= table[s - 2] ||
			      table[16 + s] < table[15 + s] ||
			      table[32 + s] < table[31 + s]))
			return -EINVAL;
	}
	for (s = 11; s < 16; s++)
		if (table[s] != 255)
			return -EINVAL;
	for (s = 12; s < 16; s++)
		if (table[16 + s] != 255 || table[32 + s] != 200)
			return -EINVAL;
	return 0;
}

/* nr:0 rising threshold,1 absolute cooling threshold,2 normalized PWM. */
static int curve_point_update(u8 *table, int point, int nr, long value)
{
	u8 next[48];
	int index;

	if (point < 1 || point > 11 || nr < 0 || nr > 2)
		return -EACCES;
	memcpy(next, table, sizeof(next));
	if (nr == 2) {
		if (point == 11)
			return -EACCES;
		if (value < 0 || value > 255)
			return -EINVAL;
		index = 32 + point;
		next[index] = (value * 200 + 127) / 255;
	} else {
		if (value < 0 || value > 95000 || value % 1000)
			return -EINVAL;
		index = nr == 0 ? point - 1 : 16 + point;
		next[index] = value / 1000;
	}
	if (curve_validate(next))
		return -EINVAL;
	memcpy(table, next, sizeof(next));
	return 0;
}

/* Cache is committed only on success; failed I/O attempts the old configuration.
 * If the bus stays broken, preserve ownership for the caller's cleanup retry.
 */
static int curve_configure_locked(struct schenker_kbd *kbd, const u8 *candidate,
				  int mode)
{
	u8 previous[48];
	int old_mode = kbd->curve_mode, ret, cleanup;

	if (mode != 0 && mode != 2 && mode != 3)
		return -EOPNOTSUPP;
	if (curve_validate(candidate))
		return -EINVAL;
	memcpy(previous, kbd->curve_config, sizeof(previous));
	kbd->curve_recovery_pending = false;
	ret = curve_restore_locked(kbd);
	if (ret) {
		kbd->curve_recovery_pending = kbd->curve_owned;
		return ret;
	}
	memcpy(kbd->curve_config, candidate, sizeof(previous));
	kbd->curve_mode = mode;
	ret = curve_apply_locked(kbd);
	if (!ret)
		return 0;
	memcpy(kbd->curve_config, previous, sizeof(previous));
	kbd->curve_mode = old_mode;
	cleanup = curve_restore_locked(kbd);
	if (!cleanup && curve_apply_locked(kbd))
		cleanup = curve_restore_locked(kbd);
	kbd->curve_recovery_pending = cleanup && kbd->curve_owned;
	return ret;
}
