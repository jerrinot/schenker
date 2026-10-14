// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Backlight, USB-C priority and firmware fan control for SCHENKER VISION E22.
 * Protocol derived from Linux drivers/platform/x86/uniwill/uniwill-acpi.c
 * (Armin Wolf and contributors) and TUXEDO's uniwill_leds.h.
 * Keyboard writes preserve unrelated bits. Optional USB-C policy changes only
 * EC 0x07cc bit 7, defaults to charging and expires without userspace renewal.
 */
#include <linux/acpi.h>
#include <linux/bitfield.h>
#include <linux/delay.h>
#include <linux/dmi.h>
#include <linux/leds.h>
#include <linux/hwmon.h>
#include <linux/hwmon-sysfs.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/workqueue.h>

#define KBD_STATUS 0x078c
#define KBD_BRIGHTNESS GENMASK(7, 5)
#define KBD_APPLY BIT(4)
#define KBD_OFF BIT(1)
#define KBD_WRITE_MASK (KBD_BRIGHTNESS | KBD_APPLY | KBD_OFF)
#define MAX_LEVEL 2

static bool writable;
module_param(writable, bool, 0400);
MODULE_PARM_DESC(writable, "Expose LED controls; default is read-only diagnostics");

static bool usb_c_policy;
module_param(usb_c_policy, bool, 0400);
MODULE_PARM_DESC(usb_c_policy, "Enable model/BIOS-restricted leased USB-C priority control");

static bool firmware_curve;
module_param(firmware_curve, bool, 0400);
MODULE_PARM_DESC(firmware_curve, "Enable autonomous model-specific EC temperature curve");

struct schenker_kbd {
	acpi_handle handle;
	struct mutex lock;
	struct led_classdev led;
	struct delayed_work priority_timeout;
	unsigned long priority_deadline;
	bool priority_supported, priority_owned, blocked;
	bool charge_profile_supported;
	bool manual_owned, manual_original;
	bool fan_supported;
	struct delayed_work curve_cleanup;
	bool curve_supported, curve_owned, curve_active;
	u8 curve_saved[48], curve_config[48];
	int curve_mode;
	bool curve_recovery_pending;
	int curve_error;
};

static const struct dmi_system_id schenker_models[] = {
	{
		.ident = "SCHENKER VISION E22 PHxARX1_PHxAQF1",
		.matches = {
			DMI_EXACT_MATCH(DMI_SYS_VENDOR, "SchenkerTechnologiesGmbH"),
			DMI_EXACT_MATCH(DMI_PRODUCT_NAME, "SCHENKER VISION (E22)"),
			DMI_EXACT_MATCH(DMI_BOARD_NAME, "PHxARX1_PHxAQF1"),
		},
	},
	{ }
};
/* No broad ACPI alias: boot loading is configured explicitly by the installer. */

static int schenker_ec_read(struct schenker_kbd *kbd, u16 address, u8 *value)
{
	union acpi_object arg = {
		.integer = { .type = ACPI_TYPE_INTEGER, .value = address },
	};
	struct acpi_object_list args = { .count = 1, .pointer = &arg };
	unsigned long long result;
	acpi_status status;

	/* Only these documented addresses are readable through this driver. */
	if (address != KBD_STATUS && address != 0x0766 && address != 0x0782 &&
	    address != 0x0742 && address != 0x07cc && address != 0x078e &&
	    address != 0x07a6 && address != 0x046a && address != 0x046b &&
	    address != 0x046f && address != 0x07cd && address != 0x07ce && address != 0x0741 &&
	    address != 0x0751 && address != 0x0765 &&
	    address != 0x0464 && address != 0x0465 &&
	    address != 0x046c && address != 0x046d &&
	    address != 0x075b && address != 0x075c &&
	    address != 0x043e && address != 0x0460 && address != 0x0461 &&
	    address != 0x0456 && address != 0x0771 && address != 0x07c5 &&
	    address != 0x07c6 && address != 0x07d3 && address != 0x0f5d &&
	    address != 0x0f5e && !(address >= 0x0f00 && address < 0x0f30))
		return -EINVAL;
	status = acpi_evaluate_integer(kbd->handle, "ECRR", &args, &result);
	if (ACPI_FAILURE(status))
		return -EIO;
	usleep_range(6000, 12000);
	if (result > U8_MAX)
		return -ERANGE;
	*value = result;
	return 0;
}

static int ec_write_status(struct schenker_kbd *kbd, u8 value)
{
	union acpi_object args_array[] = {
		{ .integer = { .type = ACPI_TYPE_INTEGER, .value = KBD_STATUS } },
		{ .integer = { .type = ACPI_TYPE_INTEGER, .value = value } },
	};
	struct acpi_object_list args = { .count = 2, .pointer = args_array };
	acpi_status status;

	if (!writable)
		return -EPERM;
	status = acpi_evaluate_object(kbd->handle, "ECRW", &args, NULL);
	if (ACPI_FAILURE(status))
		return -EIO;
	usleep_range(6000, 12000);
	return 0;
}

/* Caller holds lock. The firmware's UWOL mutex serializes each ACPI access. */
static int priority_set_locked(struct schenker_kbd *kbd, bool performance)
{
	union acpi_object a[] = {
		{ .integer = { .type = ACPI_TYPE_INTEGER, .value = 0x07cc } },
		{ .integer = { .type = ACPI_TYPE_INTEGER } },
	};
	struct acpi_object_list args = { .count = 2, .pointer = a };
	u8 old, next, actual;
	int ret;

	if (!kbd->priority_supported)
		return -EOPNOTSUPP;
	ret = schenker_ec_read(kbd, 0x07cc, &old);
	if (ret)
		return ret;
	next = (old & ~BIT(7)) | (performance ? BIT(7) : 0);
	if (next == old)
		return 0;
	a[1].integer.value = next;
	if (ACPI_FAILURE(acpi_evaluate_object(kbd->handle, "ECRW", &args, NULL)))
		return -EIO;
	usleep_range(6000, 12000);
	ret = schenker_ec_read(kbd, 0x07cc, &actual);
	if (ret)
		return ret;
	return actual == next ? 0 : -EIO;
}

static void priority_timeout_fn(struct work_struct *work)
{
	struct schenker_kbd *kbd = container_of(to_delayed_work(work),
						struct schenker_kbd, priority_timeout);
	unsigned long now;

	mutex_lock(&kbd->lock);
	now = jiffies;
	if (kbd->blocked || !kbd->priority_owned)
		goto out;
	/* A renewal can race a callback already waiting for our mutex. */
	if (time_before(now, kbd->priority_deadline)) {
		mod_delayed_work(system_wq, &kbd->priority_timeout,
				 kbd->priority_deadline - now);
		goto out;
	}
	if (priority_set_locked(kbd, false)) {
		pr_err_ratelimited("schenker-kbd: charging-priority restore failed; retrying\n");
		mod_delayed_work(system_wq, &kbd->priority_timeout, HZ);
	} else {
		kbd->priority_owned = false;
		pr_info("schenker-kbd: USB-C performance lease expired; charging priority restored\n");
	}
out:
	mutex_unlock(&kbd->lock);
}

static ssize_t usb_c_power_priority_show(struct device *dev,
					struct device_attribute *attr, char *buf)
{
	struct schenker_kbd *kbd = dev_get_drvdata(dev);
	u8 value;
	int ret;

	mutex_lock(&kbd->lock);
	ret = schenker_ec_read(kbd, 0x07cc, &value);
	mutex_unlock(&kbd->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "%s\n", value & BIT(7) ? "performance" : "charging");
}

static ssize_t usb_c_power_priority_store(struct device *dev,
					 struct device_attribute *attr,
					 const char *buf, size_t count)
{
	struct schenker_kbd *kbd = dev_get_drvdata(dev);
	bool performance;
	int ret;

	if (sysfs_streq(buf, "performance"))
		performance = true;
	else if (sysfs_streq(buf, "charging"))
		performance = false;
	else
		return -EINVAL;
	mutex_lock(&kbd->lock);
	if (kbd->blocked) {
		ret = -EBUSY;
		goto out;
	}
	if (performance) {
		/* Arm recovery even if a write succeeds but verification fails. */
		kbd->priority_owned = true;
		kbd->priority_deadline = jiffies + 20 * HZ;
		mod_delayed_work(system_wq, &kbd->priority_timeout, 20 * HZ);
	}
	ret = priority_set_locked(kbd, performance);
	if (!ret && !performance) {
		kbd->priority_owned = false;
		cancel_delayed_work(&kbd->priority_timeout);
	} else if (ret && performance) {
		kbd->priority_deadline = jiffies;
		mod_delayed_work(system_wq, &kbd->priority_timeout, 1);
	}
out:
	mutex_unlock(&kbd->lock);
	return ret ? ret : count;
}
static DEVICE_ATTR(usb_c_power_priority, 0600,
		   usb_c_power_priority_show, usb_c_power_priority_store);

/* RPM bytes can change between EC reads; high-low-high prevents torn values. */
static int fan_rpm_locked(struct schenker_kbd *kbd, u16 address, unsigned int *rpm)
{
	u8 hi, lo, check;
	int i, ret;
	for (i = 0; i < 3; i++) {
		ret = schenker_ec_read(kbd, address, &hi);
		if (!ret)
			ret = schenker_ec_read(kbd, address + 1, &lo);
		if (!ret)
			ret = schenker_ec_read(kbd, address, &check);
		if (ret)
			return ret;
		if (hi == check) {
			*rpm = ((unsigned int)hi << 8) | lo;
			return 0;
		}
	}
	return -EAGAIN;
}

static ssize_t fan_status_show(struct device *dev,
			      struct device_attribute *attr, char *buf)
{
	struct schenker_kbd *kbd = dev_get_drvdata(dev);
	unsigned int rpm1, rpm2;
	u8 pwm1, pwm2, mode;
	int ret;
	mutex_lock(&kbd->lock);
	ret = fan_rpm_locked(kbd, 0x0464, &rpm1);
	if (!ret)
		ret = fan_rpm_locked(kbd, 0x046c, &rpm2);
	if (!ret)
		ret = schenker_ec_read(kbd, 0x075b, &pwm1);
	if (!ret)
		ret = schenker_ec_read(kbd, 0x075c, &pwm2);
	if (!ret)
		ret = schenker_ec_read(kbd, 0x0751, &mode);
	mutex_unlock(&kbd->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "fan1_rpm=%u fan2_rpm=%u pwm1=%u pwm2=%u mode=0x%02x\n",
			  rpm1, rpm2, pwm1, pwm2, mode);
}
static DEVICE_ATTR_RO(fan_status);

/*
 * EC 1.47 bank 2 b9ec..b9fd clears charging-profile bits unless 0741 bit 0
 * is set (helper d170). Upstream Uniwill enables this same manual-control bit.
 * Charging-profile writes and the custom fan curve share this flag. Restore
 * its original state on removal/shutdown after releasing the custom curve.
 */
static int manual_set_locked(struct schenker_kbd *kbd, bool enable)
{
	union acpi_object a[] = {
		{ .integer = { .type = ACPI_TYPE_INTEGER, .value = 0x0741 } },
		{ .integer = { .type = ACPI_TYPE_INTEGER } },
	};
	struct acpi_object_list args = { .count = 2, .pointer = a };
	u8 old, next, actual;
	int ret;

	ret = schenker_ec_read(kbd, 0x0741, &old);
	if (ret)
		return ret;
	if (!kbd->manual_owned) {
		kbd->manual_original = !!(old & BIT(0));
		/* Retain cleanup ownership even if write verification fails. */
		kbd->manual_owned = true;
	}
	next = (old & ~BIT(0)) | (enable ? BIT(0) : 0);
	if (next == old)
		return 0;
	a[1].integer.value = next;
	if (ACPI_FAILURE(acpi_evaluate_object(kbd->handle, "ECRW", &args, NULL)))
		return -EIO;
	usleep_range(6000, 12000);
	ret = schenker_ec_read(kbd, 0x0741, &actual);
	if (ret)
		return ret;
	return !!(actual & BIT(0)) == enable ? 0 : -EIO;
}

/* Only RAM curve and documented selector bits can be written here. */
static int schenker_curve_write(struct schenker_kbd *kbd, u16 address, u8 value)
{
	union acpi_object a[] = {
		{ .integer = { .type = ACPI_TYPE_INTEGER, .value = address } },
		{ .integer = { .type = ACPI_TYPE_INTEGER, .value = value } },
	};
	struct acpi_object_list args = { .count = 2, .pointer = a };

	if (!kbd->curve_supported ||
	    !((address >= 0xf00 && address < 0xf30) || address == 0x7c5 || address == 0x7c6))
		return -EPERM;
	if (ACPI_FAILURE(acpi_evaluate_object(kbd->handle, "ECRW", &args, NULL)))
		return -EIO;
	usleep_range(6000, 12000);
	return 0;
}

#include "schenker_curve.h"

/* Runs only on a failed transaction, never to control fan speed or temperature. */
static void curve_cleanup_fn(struct work_struct *work)
{
	struct schenker_kbd *kbd = container_of(to_delayed_work(work),
					      struct schenker_kbd, curve_cleanup);
	mutex_lock(&kbd->lock);
	if (!kbd->blocked && kbd->curve_recovery_pending) {
		if (!curve_restore_locked(kbd)) {
			kbd->curve_recovery_pending = false;
			goto out;
		}
		pr_err_ratelimited("schenker-kbd: curve cleanup failed; retaining table and retrying\n");
		mod_delayed_work(system_wq, &kbd->curve_cleanup, HZ);
	}
out:
	mutex_unlock(&kbd->lock);
}

static void curve_start_locked(struct schenker_kbd *kbd)
{
	int ret;

	if (!kbd->curve_supported)
		return;
	kbd->curve_recovery_pending = false;
	cancel_delayed_work(&kbd->curve_cleanup);
	ret = curve_restore_locked(kbd);
	if (!ret)
		ret = curve_apply_locked(kbd);
	kbd->curve_error = ret;
	if (ret) {
		pr_err("schenker-kbd: firmware curve activation failed (%d); restoring stock\n", ret);
		if (curve_restore_locked(kbd)) {
			kbd->curve_recovery_pending = true;
			mod_delayed_work(system_wq, &kbd->curve_cleanup, HZ);
		}
	} else {
		pr_info("schenker-kbd: firmware fan control ready, requested hwmon mode %d\n",
			kbd->curve_mode);
	}
}

static ssize_t fan_curve_status_show(struct device *dev,
				    struct device_attribute *attr, char *buf)
{
	struct schenker_kbd *kbd = dev_get_drvdata(dev);
	const u16 addresses[] = { 0x741, 0x7c5, 0x7c6, 0x43e, 0x460, 0x461 };
	u8 v[ARRAY_SIZE(addresses)];
	int i, ret = 0;
	ssize_t len;

	mutex_lock(&kbd->lock);
	for (i = 0; i < ARRAY_SIZE(addresses); i++) {
		ret = schenker_ec_read(kbd, addresses[i], &v[i]);
		if (ret)
			break;
	}
	if (ret) {
		mutex_unlock(&kbd->lock);
		return ret;
	}
	len = sysfs_emit(buf, "configured=%u active=%u error=%d requested_mode=%d manual=%u output=%u selected=%u ec_temp_c=%u stage=%u target_pwm=%u\n",
			 firmware_curve, kbd->curve_active, kbd->curve_error, kbd->curve_mode,
			 !!(v[0] & BIT(0)), !!(v[1] & BIT(7)), !!(v[2] & BIT(2)),
			 v[3], v[4], v[5]);
	mutex_unlock(&kbd->lock);
	return len;
}
static DEVICE_ATTR_RO(fan_curve_status);

#include "schenker_hwmon.h"

static const char * const charge_profiles[] = { "standard", "long_life", "trickle" };

static ssize_t charging_profile_show(struct device *dev,
				    struct device_attribute *attr, char *buf)
{
	struct schenker_kbd *kbd = dev_get_drvdata(dev);
	u8 value;
	int ret;

	mutex_lock(&kbd->lock);
	ret = schenker_ec_read(kbd, 0x07a6, &value);
	mutex_unlock(&kbd->lock);
	if (ret)
		return ret;
	value = FIELD_GET(GENMASK(5, 4), value);
	if (value >= ARRAY_SIZE(charge_profiles))
		return -ERANGE;
	return sysfs_emit(buf, "%s\n", charge_profiles[value]);
}

static ssize_t charging_profile_store(struct device *dev,
				     struct device_attribute *attr,
				     const char *buf, size_t count)
{
	struct schenker_kbd *kbd = dev_get_drvdata(dev);
	union acpi_object a[] = {
		{ .integer = { .type = ACPI_TYPE_INTEGER, .value = 0x07a6 } },
		{ .integer = { .type = ACPI_TYPE_INTEGER } },
	};
	struct acpi_object_list args = { .count = 2, .pointer = a };
	u8 old, next, actual;
	int profile, ret;

	for (profile = 0; profile < ARRAY_SIZE(charge_profiles); profile++)
		if (sysfs_streq(buf, charge_profiles[profile]))
			break;
	if (profile == ARRAY_SIZE(charge_profiles))
		return -EINVAL;
	mutex_lock(&kbd->lock);
	if (kbd->blocked || !kbd->charge_profile_supported) {
		ret = -EBUSY;
		goto out;
	}
	ret = manual_set_locked(kbd, true);
	if (ret)
		goto out;
	ret = schenker_ec_read(kbd, 0x07a6, &old);
	if (ret)
		goto out;
	next = (old & ~GENMASK(5, 4)) | FIELD_PREP(GENMASK(5, 4), profile);
	if (next == old)
		goto out;
	a[1].integer.value = next;
	if (ACPI_FAILURE(acpi_evaluate_object(kbd->handle, "ECRW", &args, NULL))) {
		ret = -EIO;
		goto out;
	}
	usleep_range(6000, 12000);
	ret = schenker_ec_read(kbd, 0x07a6, &actual);
	if (!ret && FIELD_GET(GENMASK(5, 4), actual) != profile)
		ret = -EIO;
out:
	mutex_unlock(&kbd->lock);
	return ret ? ret : count;
}
static DEVICE_ATTR(charging_profile, 0600, charging_profile_show, charging_profile_store);

/* Firmware-reported targets only: these are NOT PECI/CPU limit readback. */
static ssize_t firmware_power_targets_show(struct device *dev,
					  struct device_attribute *attr, char *buf)
{
	struct schenker_kbd *kbd = dev_get_drvdata(dev);
	const u16 addresses[] = { 0x046a, 0x046b, 0x046f, 0x07cd, 0x07ce };
	u8 values[ARRAY_SIZE(addresses)];
	int i, ret = 0;

	mutex_lock(&kbd->lock);
	for (i = 0; i < ARRAY_SIZE(addresses); i++) {
		ret = schenker_ec_read(kbd, addresses[i], &values[i]);
		if (ret)
			break;
	}
	mutex_unlock(&kbd->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "pl1_w=%u pl2_w=%u pl4_w=%u port1_w=%u port2_w=%u\n",
			 values[0], values[1], values[2], values[3], values[4]);
}
static DEVICE_ATTR_RO(firmware_power_targets);

static enum led_brightness brightness_get(struct led_classdev *led)
{
	struct schenker_kbd *kbd = container_of(led, struct schenker_kbd, led);
	u8 value;
	int ret;

	mutex_lock(&kbd->lock);
	ret = schenker_ec_read(kbd, KBD_STATUS, &value);
	mutex_unlock(&kbd->lock);
	if (ret)
		return ret;
	if (value & KBD_OFF)
		return LED_OFF;
	return min_t(unsigned int, FIELD_GET(KBD_BRIGHTNESS, value), MAX_LEVEL);
}

static int brightness_set(struct led_classdev *led, enum led_brightness level)
{
	struct schenker_kbd *kbd = container_of(led, struct schenker_kbd, led);
	u8 value;
	int ret;

	if (level > MAX_LEVEL)
		return -EINVAL;
	mutex_lock(&kbd->lock);
	ret = schenker_ec_read(kbd, KBD_STATUS, &value);
	if (!ret) {
		/* Fresh RMW preserves the firmware-owned low bits, exactly as upstream. */
		value = (value & ~KBD_WRITE_MASK) |
			FIELD_PREP(KBD_BRIGHTNESS, level) | KBD_APPLY;
		ret = ec_write_status(kbd, value);
	}
	mutex_unlock(&kbd->lock);
	return ret;
}

static ssize_t raw_status_show(struct device *dev,
			       struct device_attribute *attr, char *buf)
{
	struct schenker_kbd *kbd = dev_get_drvdata(dev);
	u8 status, support, oem;
	int ret;

	mutex_lock(&kbd->lock);
	ret = schenker_ec_read(kbd, KBD_STATUS, &status);
	if (!ret)
		ret = schenker_ec_read(kbd, 0x0766, &support);
	if (!ret)
		ret = schenker_ec_read(kbd, 0x0782, &oem);
	mutex_unlock(&kbd->lock);
	if (ret)
		return ret;
	return sysfs_emit(buf, "kbd_status=0x%02x level=%lu power_off=%u support2=0x%02x bios_oem2=0x%02x writable=%u\n",
			  status, FIELD_GET(KBD_BRIGHTNESS, status),
			  !!(status & KBD_OFF), support, oem, writable);
}
static DEVICE_ATTR_RO(raw_status);

static struct attribute *schenker_attrs[] = {
	&dev_attr_raw_status.attr,
	&dev_attr_fan_status.attr,
	&dev_attr_fan_curve_status.attr,
	&dev_attr_usb_c_power_priority.attr,
	&dev_attr_charging_profile.attr,
	&dev_attr_firmware_power_targets.attr,
	NULL,
};
static umode_t schenker_visible(struct kobject *kobj, struct attribute *attr, int n)
{
	struct schenker_kbd *kbd = dev_get_drvdata(kobj_to_dev(kobj));
	if (attr == &dev_attr_fan_curve_status.attr && !kbd->curve_supported)
		return 0;
	if (attr == &dev_attr_fan_status.attr && !kbd->fan_supported)
		return 0;
	if (attr == &dev_attr_usb_c_power_priority.attr && !kbd->priority_supported)
		return 0;
	if (attr == &dev_attr_charging_profile.attr && !kbd->charge_profile_supported)
		return 0;
	if (attr == &dev_attr_firmware_power_targets.attr && !kbd->priority_supported)
		return 0;
	return attr->mode;
}
static const struct attribute_group schenker_group = {
	.attrs = schenker_attrs,
	.is_visible = schenker_visible,
};
static const struct attribute_group *schenker_groups[] = { &schenker_group, NULL };

static int schenker_probe(struct platform_device *pdev)
{
	struct schenker_kbd *kbd;
	u8 value;
	int ret;

	if (!dmi_check_system(schenker_models))
		return -ENODEV;
	kbd = devm_kzalloc(&pdev->dev, sizeof(*kbd), GFP_KERNEL);
	if (!kbd)
		return -ENOMEM;
	kbd->handle = ACPI_HANDLE(&pdev->dev);
	if (!kbd->handle || !acpi_has_method(kbd->handle, "ECRR") ||
	    !acpi_has_method(kbd->handle, "ECRW"))
		return dev_err_probe(&pdev->dev, -ENODEV, "Missing EC ACPI methods\n");
	mutex_init(&kbd->lock);
	kbd->blocked = true;
	INIT_DELAYED_WORK(&kbd->priority_timeout, priority_timeout_fn);
	INIT_DELAYED_WORK(&kbd->curve_cleanup, curve_cleanup_fn);
	memcpy(kbd->curve_config, schenker_curve_default, sizeof(kbd->curve_config));
	kbd->curve_mode = 2;
	platform_set_drvdata(pdev, kbd);
	if (usb_c_policy && dmi_match(DMI_BIOS_VERSION, "N.1.05A09")) {
		ret = schenker_ec_read(kbd, 0x0742, &value);
		if (!ret && (value & BIT(5))) {
			kbd->priority_supported = true;
			/* Explicit opt-in policy starts conservatively, even after a crash. */
			mutex_lock(&kbd->lock);
			ret = priority_set_locked(kbd, false);
			mutex_unlock(&kbd->lock);
			if (ret)
				return dev_err_probe(&pdev->dev, ret, "Cannot initialize charging priority\n");
			dev_info(&pdev->dev, "USB-C priority control enabled; performance lease is 20 seconds\n");
		}
	}
	if (firmware_curve && dmi_match(DMI_BIOS_VERSION, "N.1.05A09")) {
		ret = schenker_ec_read(kbd, 0x0765, &value);
		if (ret)
			return ret;
		kbd->fan_supported = !!(value & BIT(7));
		if (kbd->fan_supported)
			dev_info(&pdev->dev, "Fan RPM/PWM telemetry available\n");
	}
	if (kbd->priority_supported) {
		ret = schenker_ec_read(kbd, 0x078e, &value);
		if (ret)
			return ret;
		kbd->charge_profile_supported = !!(value & BIT(3));
	}
	ret = schenker_ec_read(kbd, KBD_STATUS, &value);
	if (ret)
		return dev_err_probe(&pdev->dev, ret, "Cannot read keyboard status\n");
	dev_info(&pdev->dev, "Keyboard status 0x%02x; %s mode\n", value,
		 writable ? "LED control" : "read-only diagnostic");
	if (!writable)
		goto curve_init;
	/* Fail closed on an unexpected layout; diagnostics remain available. */
	if (FIELD_GET(KBD_BRIGHTNESS, value) > MAX_LEVEL)
		return dev_err_probe(&pdev->dev, -ERANGE, "Unexpected brightness field\n");
	kbd->led.name = "schenker:white:kbd_backlight";
	kbd->led.max_brightness = MAX_LEVEL;
	kbd->led.color = LED_COLOR_ID_WHITE;
	kbd->led.flags = LED_RETAIN_AT_SHUTDOWN | LED_REJECT_NAME_CONFLICT;
	kbd->led.brightness_get = brightness_get;
	kbd->led.brightness_set_blocking = brightness_set;
	ret = devm_led_classdev_register(&pdev->dev, &kbd->led);
	if (ret)
		return ret;
curve_init:
	if (firmware_curve) {
		if (!dmi_match(DMI_BIOS_VERSION, "N.1.05A09"))
			return -ENODEV;
		ret = schenker_ec_read(kbd, 0x078e, &value);
		if (ret || !(value & BIT(6)))
			return ret ? ret : -EOPNOTSUPP;
		kbd->curve_supported = true;
		ret = schenker_hwmon_init(kbd, &pdev->dev);
		if (ret)
			return ret;
		/* Last step: retain binding and cleanup ownership on transaction failure. */
		mutex_lock(&kbd->lock);
		curve_start_locked(kbd);
		kbd->blocked = false;
		mutex_unlock(&kbd->lock);
	}
	kbd->blocked = false;
	return 0;
}

static int priority_stop(struct device *dev)
{
	struct schenker_kbd *kbd = dev_get_drvdata(dev);
	int ret = 0, curve_ret;

	mutex_lock(&kbd->lock);
	kbd->blocked = true;
	mutex_unlock(&kbd->lock);
	cancel_delayed_work_sync(&kbd->priority_timeout);
	cancel_delayed_work_sync(&kbd->curve_cleanup);
	mutex_lock(&kbd->lock);
	if (kbd->priority_supported)
		ret = priority_set_locked(kbd, false);
	if (!ret)
		kbd->priority_owned = false;
	curve_ret = curve_restore_locked(kbd);
	if (!ret)
		ret = curve_ret;
	mutex_unlock(&kbd->lock);
	return ret;
}

static int priority_resume(struct device *dev)
{
	struct schenker_kbd *kbd = dev_get_drvdata(dev);
	mutex_lock(&kbd->lock);
	kbd->blocked = false;
	curve_start_locked(kbd);
	if (kbd->priority_owned) {
		/* A failed suspend restore must not lose its watchdog. */
		kbd->priority_deadline = jiffies;
		mod_delayed_work(system_wq, &kbd->priority_timeout, 1);
	}
	mutex_unlock(&kbd->lock);
	/* Userspace must recheck the supply and power caps before renewing. */
	return 0;
}

static int priority_suspend(struct device *dev)
{
	int ret = priority_stop(dev);
	if (ret)
		priority_resume(dev);
	return ret;
}

static DEFINE_SIMPLE_DEV_PM_OPS(schenker_pm_ops, priority_suspend, priority_resume);

static void schenker_remove(struct platform_device *pdev)
{
	struct schenker_kbd *kbd = platform_get_drvdata(pdev);

	if (priority_stop(&pdev->dev))
		dev_err(&pdev->dev, "Failed to restore USB-C priority or automatic fans\n");
	mutex_lock(&kbd->lock);
	if (kbd->manual_owned && !kbd->curve_owned) {
		if (manual_set_locked(kbd, kbd->manual_original))
			dev_err(&pdev->dev, "Failed to restore EC manual-control state\n");
		else
			kbd->manual_owned = false;
	}
	mutex_unlock(&kbd->lock);
}

static const struct acpi_device_id schenker_acpi_ids[] = {
	{ "INOU0000" },
	{ }
};

static struct platform_driver schenker_driver = {
	.probe = schenker_probe,
	.remove = schenker_remove,
	.shutdown = schenker_remove,
	.driver = {
		.name = "schenker-kbd",
		.acpi_match_table = schenker_acpi_ids,
		.dev_groups = schenker_groups,
		.probe_type = PROBE_FORCE_SYNCHRONOUS,
		.pm = pm_sleep_ptr(&schenker_pm_ops),
	},
};

static int __init schenker_init(void)
{
	if (!dmi_check_system(schenker_models))
		return -ENODEV;
	return platform_driver_register(&schenker_driver);
}
module_init(schenker_init);

static void __exit schenker_exit(void)
{
	platform_driver_unregister(&schenker_driver);
}
module_exit(schenker_exit);

MODULE_LICENSE("GPL");
MODULE_VERSION("0.7.0");
MODULE_DESCRIPTION("Model-restricted SCHENKER VISION E22 keyboard, USB-C priority and firmware fan curve");
