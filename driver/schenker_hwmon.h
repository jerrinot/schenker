/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Shared firmware fan controller, two tachometers. No userspace polling needed. */
static int hwmon_configure_locked(struct schenker_kbd *kbd, const u8 *table, int mode)
{
	int ret;

	if (kbd->blocked)
		return -EBUSY;
	if (mode != 0 && mode != 2 && mode != 3)
		return -EOPNOTSUPP;
	if (curve_validate(table))
		return -EINVAL;
	/* Callback may already be waiting for lock: pending flag prevents stale cleanup. */
	kbd->curve_recovery_pending = false;
	cancel_delayed_work(&kbd->curve_cleanup);
	ret = curve_configure_locked(kbd, table, mode);
	kbd->curve_error = ret;
	if (kbd->curve_recovery_pending)
		mod_delayed_work(system_wq, &kbd->curve_cleanup, HZ);
	return ret;
}

static int hwmon_mode_locked(struct schenker_kbd *kbd, long *value)
{
	u8 manual, select, output, boost;
	int ret;

	ret = schenker_ec_read(kbd, 0x741, &manual);
	if (!ret)
		ret = schenker_ec_read(kbd, 0x7c6, &select);
	if (!ret)
		ret = schenker_ec_read(kbd, 0x7c5, &output);
	if (!ret)
		ret = schenker_ec_read(kbd, 0x751, &boost);
	if (ret)
		return ret;
	if (boost)
		return -EBUSY;
	if (!(select & BIT(2)) && !(output & BIT(7))) {
		*value = 3;
		return 0;
	}
	if (!(manual & BIT(0)) || !(select & BIT(2)) || !(output & BIT(7)) ||
	    !kbd->curve_owned || !kbd->curve_active || kbd->curve_mode == 3)
		return -EIO;
	*value = kbd->curve_mode;
	return 0;
}

static umode_t schenker_hwmon_visible(const void *data, enum hwmon_sensor_types type,
				     u32 attr, int channel)
{
	if (type == hwmon_pwm && attr == hwmon_pwm_enable && channel == 0)
		return 0644;
	return 0444;
}

static int schenker_hwmon_read(struct device *dev, enum hwmon_sensor_types type,
			      u32 attr, int channel, long *value)
{
	struct schenker_kbd *kbd = dev_get_drvdata(dev);
	unsigned int rpm;
	u8 raw;
	int ret = -EOPNOTSUPP;

	mutex_lock(&kbd->lock);
	if (kbd->blocked) {
		ret = -EBUSY;
		goto out;
	}
	switch (type) {
	case hwmon_temp:
		ret = schenker_ec_read(kbd, 0x43e, &raw);
		if (!ret) {
			if (raw > 125)
				ret = -ENODATA;
			else
				*value = raw * 1000;
		}
		break;
	case hwmon_fan:
		ret = fan_rpm_locked(kbd, channel == 0 ? 0x464 : 0x46c, &rpm);
		if (!ret)
			*value = rpm;
		break;
	case hwmon_pwm:
		switch (attr) {
		case hwmon_pwm_input:
			ret = schenker_ec_read(kbd, channel == 0 ? 0x75b : 0x75c, &raw);
			if (!ret) {
				if (raw > 200)
					ret = -ERANGE;
				else
					*value = (raw * 255 + 100) / 200;
			}
			break;
		case hwmon_pwm_enable:
			ret = hwmon_mode_locked(kbd, value);
			break;
		case hwmon_pwm_auto_channels_temp:
			*value = 1;
			ret = 0;
			break;
		default:
			break;
		}
		break;
	default:
		break;
	}
out:
	mutex_unlock(&kbd->lock);
	return ret;
}

static int schenker_hwmon_read_string(struct device *dev, enum hwmon_sensor_types type,
				     u32 attr, int channel, const char **str)
{
	if (type == hwmon_temp) {
		*str = "CPU EC";
		return 0;
	}
	if (type == hwmon_fan) {
		*str = channel == 0 ? "Fan 1 (shared CPU curve)" : "Fan 2 (shared CPU curve)";
		return 0;
	}
	return -EOPNOTSUPP;
}

static int schenker_hwmon_write(struct device *dev, enum hwmon_sensor_types type,
			       u32 attr, int channel, long value)
{
	struct schenker_kbd *kbd = dev_get_drvdata(dev);
	u8 candidate[48];
	int ret;

	if (type != hwmon_pwm || attr != hwmon_pwm_enable || channel != 0)
		return -EOPNOTSUPP;
	/* Check before converting long to the internal int mode. */
	if (value != 0 && value != 2 && value != 3)
		return -EOPNOTSUPP;
	mutex_lock(&kbd->lock);
	memcpy(candidate, kbd->curve_config, sizeof(candidate));
	ret = hwmon_configure_locked(kbd, candidate, value);
	mutex_unlock(&kbd->lock);
	return ret;
}

static ssize_t curve_point_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct schenker_kbd *kbd = dev_get_drvdata(dev);
	struct sensor_device_attribute_2 *a = to_sensor_dev_attr_2(attr);
	unsigned int value;

	mutex_lock(&kbd->lock);
	if (!a->index)
		value = 0;
	else if (a->nr == 2)
		value = (kbd->curve_config[32 + a->index] * 255 + 100) / 200;
	else
		value = kbd->curve_config[a->nr == 0 ? a->index - 1 : 16 + a->index] * 1000;
	mutex_unlock(&kbd->lock);
	return sysfs_emit(buf, "%u\n", value);
}

static ssize_t curve_point_store(struct device *dev, struct device_attribute *attr,
				 const char *buf, size_t count)
{
	struct schenker_kbd *kbd = dev_get_drvdata(dev);
	struct sensor_device_attribute_2 *a = to_sensor_dev_attr_2(attr);
	u8 candidate[48];
	long value;
	int ret;

	ret = kstrtol(buf, 10, &value);
	if (ret)
		return ret;
	mutex_lock(&kbd->lock);
	if (kbd->blocked) {
		ret = -EBUSY;
		goto out;
	}
	memcpy(candidate, kbd->curve_config, sizeof(candidate));
	ret = curve_point_update(candidate, a->index, a->nr, value);
	if (ret || !memcmp(candidate, kbd->curve_config, sizeof(candidate)))
		goto out;
	if (kbd->curve_mode == 2)
		ret = hwmon_configure_locked(kbd, candidate, 2);
	else
		/* Editing an inactive curve does not change stock/full-speed operation. */
		memcpy(kbd->curve_config, candidate, sizeof(candidate));
out:
	mutex_unlock(&kbd->lock);
	return ret ? ret : count;
}

#define CURVE_POINT(n, tmode, pmode) \
	static SENSOR_DEVICE_ATTR_2(pwm1_auto_point##n##_temp, tmode, \
		curve_point_show, curve_point_store, 0, (n) - 1); \
	static SENSOR_DEVICE_ATTR_2(pwm1_auto_point##n##_temp_hyst, tmode, \
		curve_point_show, curve_point_store, 1, (n) - 1); \
	static SENSOR_DEVICE_ATTR_2(pwm1_auto_point##n##_pwm, pmode, \
		curve_point_show, curve_point_store, 2, (n) - 1)

CURVE_POINT(1, 0444, 0444);
CURVE_POINT(2, 0644, 0644);
CURVE_POINT(3, 0644, 0644);
CURVE_POINT(4, 0644, 0644);
CURVE_POINT(5, 0644, 0644);
CURVE_POINT(6, 0644, 0644);
CURVE_POINT(7, 0644, 0644);
CURVE_POINT(8, 0644, 0644);
CURVE_POINT(9, 0644, 0644);
CURVE_POINT(10, 0644, 0644);
CURVE_POINT(11, 0644, 0644);
CURVE_POINT(12, 0644, 0444);

#define CURVE_POINT_ATTRS(n) \
	&sensor_dev_attr_pwm1_auto_point##n##_temp.dev_attr.attr, \
	&sensor_dev_attr_pwm1_auto_point##n##_temp_hyst.dev_attr.attr, \
	&sensor_dev_attr_pwm1_auto_point##n##_pwm.dev_attr.attr

static struct attribute *schenker_curve_attrs[] = {
	CURVE_POINT_ATTRS(1), CURVE_POINT_ATTRS(2), CURVE_POINT_ATTRS(3),
	CURVE_POINT_ATTRS(4), CURVE_POINT_ATTRS(5), CURVE_POINT_ATTRS(6),
	CURVE_POINT_ATTRS(7), CURVE_POINT_ATTRS(8), CURVE_POINT_ATTRS(9),
	CURVE_POINT_ATTRS(10), CURVE_POINT_ATTRS(11), CURVE_POINT_ATTRS(12), NULL,
};
static const struct attribute_group schenker_curve_group = { .attrs = schenker_curve_attrs };
static const struct attribute_group *schenker_hwmon_groups[] = { &schenker_curve_group, NULL };
static const struct hwmon_ops schenker_hwmon_ops = {
	.is_visible = schenker_hwmon_visible,
	.read = schenker_hwmon_read,
	.read_string = schenker_hwmon_read_string,
	.write = schenker_hwmon_write,
};
static const struct hwmon_channel_info * const schenker_hwmon_info[] = {
	HWMON_CHANNEL_INFO(temp, HWMON_T_INPUT | HWMON_T_LABEL),
	HWMON_CHANNEL_INFO(fan, HWMON_F_INPUT | HWMON_F_LABEL, HWMON_F_INPUT | HWMON_F_LABEL),
	HWMON_CHANNEL_INFO(pwm, HWMON_PWM_INPUT | HWMON_PWM_ENABLE | HWMON_PWM_AUTO_CHANNELS_TEMP,
			   HWMON_PWM_INPUT | HWMON_PWM_ENABLE),
	NULL,
};
static const struct hwmon_chip_info schenker_hwmon_chip = {
	.ops = &schenker_hwmon_ops,
	.info = schenker_hwmon_info,
};
static int schenker_hwmon_init(struct schenker_kbd *kbd, struct device *parent)
{
	struct device *hwdev;

	hwdev = devm_hwmon_device_register_with_info(parent, "schenker", kbd,
						   &schenker_hwmon_chip, schenker_hwmon_groups);
	return PTR_ERR_OR_ZERO(hwdev);
}
