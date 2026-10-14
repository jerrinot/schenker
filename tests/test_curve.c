/* Test the curve transaction code against an EC emulator with I/O failures. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>

typedef uint8_t u8;
typedef uint16_t u16;
#define BIT(n) (1U << (n))
#define ARRAY_SIZE(a) ((int)(sizeof(a) / sizeof((a)[0])))

/* Only the fields used by the shared curve code; not a kernel driver mock. */
struct schenker_kbd {
	bool curve_owned, curve_active, curve_recovery_pending;
	u8 curve_saved[48], curve_config[48];
	int curve_mode;
};

static u8 ec[4096], original[48];
static int calls, fail_after, fail_until, writes, faults;
static bool ambiguous;
static int fault_cases, no_fault_cases;

static bool io_failed(void)
{
	bool failed = ++calls >= fail_after && calls <= fail_until;

	if (failed)
		faults++;
	return failed;
}

static int schenker_ec_read(struct schenker_kbd *kbd, u16 address, u8 *value)
{
	(void)kbd;
	if (io_failed())
		return -EIO;
	*value = ec[address];
	return 0;
}

static int schenker_curve_write(struct schenker_kbd *kbd, u16 address, u8 value);

static int manual_set_locked(struct schenker_kbd *kbd, bool on)
{
	(void)kbd;
	if (io_failed())
		return -EIO;
	if (on)
		ec[0x741] |= BIT(0);
	else
		ec[0x741] &= ~BIT(0);
	return 0;
}

#include "schenker_curve.h"

static int schenker_curve_write(struct schenker_kbd *kbd, u16 address, u8 value)
{
	bool failed = io_failed();
	u8 expected[48];

	if (failed && !ambiguous)
		return -EIO;
	if (address >= 0xf00 && address < 0xf30) {
		assert(!(ec[0x7c5] & BIT(7)));
		assert(!(ec[0x7c6] & BIT(2)));
	}
	if (address == 0x7c6 && (value & BIT(2))) {
		assert(ec[0x741] & BIT(0));
		assert(ec[0x7c5] & BIT(7));
		memcpy(expected, kbd->curve_config, sizeof(expected));
		if (kbd->curve_mode == 0)
			memset(expected + 32, 200, 16);
		assert(!memcmp(ec + 0xf00, expected, sizeof(expected)));
	}
	ec[address] = value;
	writes++;
	return failed ? -EIO : 0;
}

static void reset(struct schenker_kbd *kbd)
{
	memset(kbd, 0, sizeof(*kbd));
	memset(ec, 0, sizeof(ec));
	ec[0x78e] = BIT(6);
	ec[0x7c5] = 3;
	ec[0x7c6] = 16;
	for (int i = 0; i < 48; i++)
		original[i] = i + 3;
	memcpy(ec + 0xf00, original, sizeof(original));
	calls = writes = faults = 0;
	fail_after = 100000;
	fail_until = 1000000;
	ambiguous = false;
	memcpy(kbd->curve_config, schenker_curve_default, 48);
	kbd->curve_mode = 2;
}

static void assert_restored(const struct schenker_kbd *kbd)
{
	assert(!kbd->curve_owned && !kbd->curve_active);
	assert(ec[0x7c5] == 3 && ec[0x7c6] == 16);
	assert(!memcmp(ec + 0xf00, original, sizeof(original)));
}

static void finish_fault_case(struct schenker_kbd *kbd)
{
	if (faults)
		fault_cases++;
	else
		no_fault_cases++;
	fail_after = 100000;
	assert(!curve_restore_locked(kbd));
	assert_restored(kbd);
}

static void test_ownership_and_reapply(void)
{
	struct schenker_kbd k;

	reset(&k);
	assert(!curve_apply_locked(&k));
	assert(k.curve_active);
	assert(curve_apply_locked(&k) == -EBUSY);
	assert(!curve_restore_locked(&k));
	assert_restored(&k);
	/* Exercises transactions used by PM, not the kernel PM callbacks. */
	for (int i = 0; i < 4; i++) {
		assert(!curve_apply_locked(&k));
		assert(!curve_restore_locked(&k));
		assert_restored(&k);
	}
}

static void test_routing_guards(void)
{
	const u16 addresses[] = { 0x456, 0x751, 0x771, 0x78e, 0x7c5, 0x7c6, 0x7d3 };
	const u8 values[] = { 128, 64, 1, 0, 131, 20, 1 };
	struct schenker_kbd k;

	for (int i = 0; i < ARRAY_SIZE(addresses); i++) {
		reset(&k);
		ec[addresses[i]] = values[i];
		assert(curve_apply_locked(&k) == -EBUSY);
		assert(writes == 0 && !k.curve_owned);
	}
	reset(&k);
	ec[0xf5d] = 0xfd;
	ec[0xf5e] = 0xc9;
	assert(curve_apply_locked(&k) == -EBUSY);
	assert(!writes);
}

static void test_activation_failures(void)
{
	struct schenker_kbd k;

	for (int ambiguity = 0; ambiguity < 2; ambiguity++) {
		for (int n = 1; n <= 240; n++) {
			reset(&k);
			ambiguous = ambiguity;
			fail_after = n;
			curve_apply_locked(&k);
			/* A second attempt while still disconnected must preserve active RAM. */
			curve_restore_locked(&k);
			finish_fault_case(&k);
		}
	}
}

static void test_teardown_failures(void)
{
	struct schenker_kbd k;

	for (int ambiguity = 0; ambiguity < 2; ambiguity++) {
		for (int n = 1; n <= 160; n++) {
			reset(&k);
			assert(!curve_apply_locked(&k));
			calls = 0;
			fail_after = n;
			ambiguous = ambiguity;
			curve_restore_locked(&k);
			finish_fault_case(&k);
		}
	}
}

static void test_point_validation(void)
{
	struct schenker_kbd k;
	u8 candidate[48], saved[48];

	reset(&k);
	memcpy(candidate, k.curve_config, sizeof(candidate));
	assert(!curve_validate(candidate));
	assert(!curve_point_update(candidate, 11, 0, 88000));
	assert(candidate[10] == 88);
	assert(!curve_point_update(candidate, 11, 1, 86000));
	assert(candidate[27] == 86);
	assert(!curve_point_update(candidate, 10, 2, 245));
	assert(candidate[42] == 192);
	memcpy(saved, candidate, sizeof(saved));
	assert(curve_point_update(candidate, 11, 0, 86000) == -EINVAL);
	assert(curve_point_update(candidate, 11, 0, 99000) == -EINVAL);
	assert(curve_point_update(candidate, 11, 0, 88500) == -EINVAL);
	assert(curve_point_update(candidate, 11, 2, 200) == -EACCES);
	assert(curve_point_update(candidate, 0, 0, 1000) == -EACCES);
	assert(curve_point_update(candidate, 1, 2, 0) == -EINVAL);
	assert(curve_point_update(candidate, 1, 2, 256) == -EINVAL);
	assert(curve_point_update(candidate, 1, 0, -1000) == -EINVAL);
	assert(!memcmp(candidate, saved, sizeof(saved)));
}

static void test_modes(void)
{
	struct schenker_kbd k;
	u8 candidate[48];

	reset(&k);
	memcpy(candidate, k.curve_config, sizeof(candidate));
	assert(!curve_point_update(candidate, 11, 0, 88000));
	assert(!curve_apply_locked(&k));
	assert(!curve_configure_locked(&k, candidate, 2));
	assert(!memcmp(ec + 0xf00, candidate, sizeof(candidate)));
	assert(!curve_configure_locked(&k, candidate, 0));
	for (int i = 0; i < 16; i++)
		assert(ec[0xf20 + i] == 200);
	assert(!curve_restore_locked(&k));
	assert(!curve_apply_locked(&k));
	for (int i = 0; i < 16; i++)
		assert(ec[0xf20 + i] == 200);
	assert(!curve_configure_locked(&k, candidate, 3));
	assert_restored(&k);
	assert(!curve_restore_locked(&k));
	assert(!curve_apply_locked(&k));
	assert_restored(&k);
	assert(!curve_configure_locked(&k, candidate, 2));
	assert(!memcmp(ec + 0xf00, candidate, sizeof(candidate)));
	assert(curve_configure_locked(&k, candidate, 1) == -EOPNOTSUPP);
	assert(!curve_restore_locked(&k));
	assert_restored(&k);
}

static void test_live_update_failure(bool ambiguity, bool temporary, int n)
{
	struct schenker_kbd k;
	u8 candidate[48];
	int ret;

	reset(&k);
	assert(!curve_apply_locked(&k));
	memcpy(candidate, k.curve_config, sizeof(candidate));
	assert(!curve_point_update(candidate, 11, 0, 88000));
	calls = 0;
	fail_after = n;
	ambiguous = ambiguity;
	if (temporary)
		fail_until = n + 3;
	ret = curve_configure_locked(&k, candidate, 2);
	if (ret)
		assert(!memcmp(k.curve_config, schenker_curve_default, 48));
	else
		assert(!memcmp(k.curve_config, candidate, sizeof(candidate)));
	if (ret && !k.curve_recovery_pending && k.curve_active)
		assert(!memcmp(ec + 0xf00, schenker_curve_default, 48));
	finish_fault_case(&k);
}

static void test_live_update_failures(void)
{
	for (int ambiguity = 0; ambiguity < 2; ambiguity++) {
		for (int temporary = 0; temporary < 2; temporary++) {
			for (int n = 1; n <= 400; n++)
				test_live_update_failure(ambiguity, temporary, n);
		}
	}
}

int main(void)
{
	test_ownership_and_reapply();
	test_routing_guards();
	test_point_validation();
	test_modes();
	test_activation_failures();
	test_teardown_failures();
	test_live_update_failures();
	assert(fault_cases > 0);
	printf("PASS: curve validation, ownership, routing, modes and reapply; "
	       "%d fault-injected cases, %d cases outside the failure window\n",
	       fault_cases, no_fault_cases);
	return 0;
}
