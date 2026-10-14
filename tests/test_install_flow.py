"""Run the real installer in a private filesystem with fake kernel/service tools.

Requires bubblewrap and unprivileged user namespaces; never invokes host DKMS,
modprobe or systemctl. The checkout and host files are mounted read-only.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
KERNEL = os.uname().release
KERNELS = (KERNEL, 'test-other-kernel')
SERVICE = 'schenker-usbc-policy.service'
MANAGED = (
    'etc/modprobe.d/schenker-kbd.conf',
    'etc/modules-load.d/schenker-kbd.conf',
    'usr/local/bin/schenker-backlight',
    'usr/local/sbin/schenker-power',
    'usr/local/libexec/schenker-usbc-policy.py',
    f'etc/systemd/system/{SERVICE}',
    f'etc/systemd/system/{SERVICE}.d/charging-profile.conf',
    f'etc/systemd/system/{SERVICE}.d/firmware-profile.conf',
)


@unittest.skipUnless(shutil.which('bwrap'), 'install flow tests require bubblewrap')
class InstallFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='schenker-install-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ('etc', 'var/lib', 'var/tmp', 'tmp', 'sys', 'boot',
                          'usr/src', 'usr/local', 'lib/modules', 'mock-bin'):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.state = dict(installed={k: '0.6.0' for k in KERNELS}, loaded='0.6.0',
                          parameters=dict(writable='Y', usb_c_policy='Y',
                                          firmware_curve='Y', fan_policy='N'),
                          active=True, enabled=True, events=[], failure=None)
        self.write('sys/class/dmi/id/sys_vendor', 'SchenkerTechnologiesGmbH')
        self.write('sys/class/dmi/id/product_name', 'SCHENKER VISION (E22)')
        self.write('sys/class/dmi/id/board_name', 'PHxARX1_PHxAQF1')
        self.write('sys/class/dmi/id/bios_version', 'N.1.05A09')
        for kernel in KERNELS:
            self.write(f'lib/modules/{kernel}/build/Makefile', '# fake headers')
            self.write(f'boot/vmlinuz-{kernel}', 'fake image')
        (self.root / 'var/lib/dkms/schenker-kbd/0.6.0').mkdir(parents=True)
        self.write('sys/module/schenker_kbd/version', '0.6.0')
        for name, value in self.state['parameters'].items():
            self.write(f'sys/module/schenker_kbd/parameters/{name}', value)
        for path in MANAGED:
            self.write(path, 'old deployment\n')
        self.write(MANAGED[0], 'options schenker_kbd writable=1 usb_c_policy=1 fan_policy=0 firmware_curve=1\n')
        for name in ('modinfo', 'modprobe', 'dkms', 'systemctl', 'update-initramfs'):
            target = self.root / 'mock-bin' / name
            shutil.copyfile(ROOT / 'tests/fixtures/install-command.py', target)
            target.chmod(0o755)
        self.original_files = {path: (self.root / path).read_bytes() for path in MANAGED}

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def run_installer(self, action='install'):
        self.write('tmp/mock-state.json', json.dumps(self.state))
        command = ['bwrap', '--unshare-all', '--uid', '0', '--gid', '0',
                   '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc']
        for directory in ('etc', 'var/lib', 'var/tmp', 'tmp', 'sys', 'boot',
                          'usr/src', 'usr/local', 'lib/modules'):
            command += ['--bind', str(self.root / directory), '/' + directory]
        command += ['--ro-bind', str(self.root / 'mock-bin'), '/usr/sbin',
                    '--chdir', str(ROOT), '/bin/bash', str(ROOT / 'install.sh'), action]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.state = json.loads((self.root / 'tmp/mock-state.json').read_text())
        return result

    def assert_restored(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Previous deployment restored', result.stderr)
        self.assertEqual(self.state['installed'], {k: '0.6.0' for k in KERNELS})
        self.assertEqual(self.state['loaded'], '0.6.0')
        self.assertEqual(self.state['parameters'], dict(writable='Y', usb_c_policy='Y',
                                                      firmware_curve='Y', fan_policy='N'))
        self.assertTrue(self.state['active'])
        self.assertTrue(self.state['enabled'])
        for path, original in self.original_files.items():
            self.assertEqual((self.root / path).read_bytes(), original, path)
        self.assertFalse((self.root / 'var/lib/schenker-kbd/config-defaults/modprobe.conf').exists())

    def test_successful_upgrade(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state['installed'], {k: '0.7.0' for k in KERNELS})
        self.assertEqual(self.state['loaded'], '0.7.0')
        self.assertTrue(self.state['active'])
        self.assertEqual((self.root / MANAGED[3]).read_bytes(), (ROOT / 'userspace/schenker-power').read_bytes())

    def test_custom_disabled_features_are_honored(self):
        self.write(MANAGED[0], 'options schenker_kbd writable=0 usb_c_policy=0 firmware_curve=0\n')
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state['parameters'], dict(writable='N', usb_c_policy='N', firmware_curve='N'))
        self.assertFalse(self.state['active'])
        self.assertFalse(self.state['enabled'])
        self.assertIn('writable=0', (self.root / MANAGED[0]).read_text())

    def test_load_failure_restores_old_deployment(self):
        self.state['failure'] = 'module_load'
        self.assert_restored(self.run_installer())

    def test_service_failure_restores_files_and_previous_service(self):
        self.state['failure'] = 'service_start'
        self.assert_restored(self.run_installer())

    def test_partial_dkms_install_failure_restores_modules(self):
        self.state['failure'] = 'dkms_install'
        self.assert_restored(self.run_installer())

    def test_initramfs_failure_rolls_back(self):
        self.state['failure'] = 'initramfs'
        self.assert_restored(self.run_installer())

    def test_unload_failure_preserves_running_driver_and_service(self):
        self.state['failure'] = 'unload'
        self.assert_restored(self.run_installer())

    def test_rollback_keeps_previously_inactive_service_inactive(self):
        self.state.update(active=False, enabled=False, failure='service_start')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Previous deployment restored', result.stderr)
        self.assertEqual(self.state['loaded'], '0.6.0')
        self.assertFalse(self.state['active'])
        self.assertFalse(self.state['enabled'])

    def test_fresh_install_failure_removes_new_deployment(self):
        self.state.update(installed={}, loaded=None, parameters={}, active=False,
                          enabled=False, failure='service_start')
        shutil.rmtree(self.root / 'sys/module/schenker_kbd')
        for path in MANAGED:
            (self.root / path).unlink()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Previous deployment restored', result.stderr)
        self.assertEqual(self.state['installed'], {})
        self.assertIsNone(self.state['loaded'])
        self.assertFalse(self.state['active'])
        self.assertFalse(self.state['enabled'])
        for path in MANAGED:
            self.assertFalse((self.root / path).exists(), path)

    def test_rollback_failure_reports_backup_without_claiming_success(self):
        self.state['failures'] = ['module_load', 'rollback_dkms']
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Rollback incomplete', result.stderr)
        self.assertNotIn('Previous deployment restored', result.stderr)
        backups = list((self.root / 'var/tmp').glob('schenker-rollback.*'))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / 'kernel-versions').is_file())

    def test_remove_refuses_different_loaded_version(self):
        self.state['installed'] = {k: '0.7.0' for k in KERNELS}
        result = self.run_installer('remove')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('running driver is 0.6.0', result.stderr)
        self.assertTrue(self.state['active'])
        self.assertEqual(self.state['loaded'], '0.6.0')

    def test_remove_wrong_version_does_not_mutate_deployment(self):
        result = self.run_installer('remove')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Refusing removal', result.stderr)
        self.assertEqual(self.state['loaded'], '0.6.0')
        self.assertTrue(self.state['active'])
        self.assertTrue(all(event[0] in ('modinfo', 'dkms') for event in self.state['events']))
        for path, original in self.original_files.items():
            self.assertEqual((self.root / path).read_bytes(), original)

    def test_remove_matching_version(self):
        self.state['installed'] = {k: '0.7.0' for k in KERNELS}
        self.state['loaded'] = '0.7.0'
        self.write('sys/module/schenker_kbd/version', '0.7.0')
        (self.root / 'var/lib/dkms/schenker-kbd/0.7.0').mkdir()
        result = self.run_installer('remove')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state['installed'], {})
        self.assertIsNone(self.state['loaded'])
        self.assertFalse(self.state['active'])
        for path in MANAGED:
            self.assertFalse((self.root / path).exists(), path)
