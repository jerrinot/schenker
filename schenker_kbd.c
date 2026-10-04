// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Experimental backlight-only driver for SCHENKER VISION E22.
 * Protocol derived from Linux drivers/platform/x86/uniwill/uniwill-acpi.c
 * (Armin Wolf and contributors) and TUXEDO's uniwill_leds.h.
 * No writes on probe, removal or suspend. The only writable EC address is
 * the keyboard status register, and only its brightness/apply/off bits change.
 */
#include <linux/acpi.h>
#include <linux/bitfield.h>
#include <linux/delay.h>
#include <linux/dmi.h>
#include <linux/leds.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_device.h>
#include <linux/slab.h>

#define KBD_STATUS 0x078c
#define KBD_BRIGHTNESS GENMASK(7, 5)
#define KBD_APPLY BIT(4)
#define KBD_OFF BIT(1)
#define KBD_WRITE_MASK (KBD_BRIGHTNESS | KBD_APPLY | KBD_OFF)
#define MAX_LEVEL 2

static bool writable;
module_param(writable, bool, 0400);
MODULE_PARM_DESC(writable, "Expose LED controls; default is read-only diagnostics");

struct schenker_kbd {
	acpi_handle handle;
	struct mutex lock;
	struct led_classdev led;
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
	if (address != KBD_STATUS && address != 0x0766 && address != 0x0782)
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
	NULL,
};
ATTRIBUTE_GROUPS(schenker);

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
	platform_set_drvdata(pdev, kbd);
	ret = schenker_ec_read(kbd, KBD_STATUS, &value);
	if (ret)
		return dev_err_probe(&pdev->dev, ret, "Cannot read keyboard status\n");
	dev_info(&pdev->dev, "Keyboard status 0x%02x; %s mode\n", value,
		 writable ? "LED control" : "read-only diagnostic");
	if (!writable)
		return 0;
	/* Fail closed on an unexpected layout; diagnostics remain available. */
	if (FIELD_GET(KBD_BRIGHTNESS, value) > MAX_LEVEL)
		return dev_err_probe(&pdev->dev, -ERANGE, "Unexpected brightness field\n");
	kbd->led.name = "schenker:white:kbd_backlight";
	kbd->led.max_brightness = MAX_LEVEL;
	kbd->led.color = LED_COLOR_ID_WHITE;
	kbd->led.flags = LED_RETAIN_AT_SHUTDOWN | LED_REJECT_NAME_CONFLICT;
	kbd->led.brightness_get = brightness_get;
	kbd->led.brightness_set_blocking = brightness_set;
	return devm_led_classdev_register(&pdev->dev, &kbd->led);
}

static const struct acpi_device_id schenker_acpi_ids[] = {
	{ "INOU0000" },
	{ }
};

static struct platform_driver schenker_driver = {
	.probe = schenker_probe,
	.driver = {
		.name = "schenker-kbd",
		.acpi_match_table = schenker_acpi_ids,
		.dev_groups = schenker_groups,
		.probe_type = PROBE_FORCE_SYNCHRONOUS,
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
MODULE_VERSION("0.1.0");
MODULE_DESCRIPTION("Model-restricted experimental SCHENKER VISION E22 keyboard backlight");
