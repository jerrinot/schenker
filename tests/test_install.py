"""Configuration upgrades exercised in a temporary tree, without root or DKMS."""
from pathlib import Path
import subprocess
import tempfile
import unittest

INSTALLER = Path(__file__).resolve().parents[1] / 'install.sh'


class ConfigUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source' / 'preferences.conf'
        self.source.parent.mkdir()
        self.source.write_text('new default\n')
        self.destination = self.root / 'etc' / 'preferences.conf'
        self.baseline = self.root / 'state' / 'preferences.conf'

    def install(self):
        subprocess.run(
            ['bash', '-euc', 'source "$1"; install_config "$2" "$3" "$4"',
             'test', str(INSTALLER), str(self.source), str(self.destination),
             str(self.baseline)], check=True, capture_output=True, text=True)

    def test_fresh_install_and_upgrade_unmodified_default(self):
        self.install()
        self.assertEqual(self.destination.read_text(), 'new default\n')
        self.source.write_text('next release\n')
        self.install()
        self.assertEqual(self.destination.read_text(), 'next release\n')
        self.assertEqual(self.baseline.read_bytes(), self.source.read_bytes())

    def test_preserves_local_edits_across_repeated_upgrades(self):
        self.install()
        self.destination.write_text('local preference\n')
        for default in ('release two\n', 'release three\n'):
            self.source.write_text(default)
            self.install()
            self.assertEqual(self.destination.read_text(), 'local preference\n')
            self.assertEqual(Path(str(self.destination) + '.dist').read_text(), default)

    def test_preserves_existing_config_without_baseline(self):
        self.destination.parent.mkdir()
        self.destination.write_text('existing local preference\n')
        self.install()
        self.assertEqual(self.destination.read_text(), 'existing local preference\n')

    def test_migrates_previous_module_defaults(self):
        self.source = self.source.with_name('schenker-kbd.modprobe.conf')
        self.source.write_text('options schenker_kbd writable=1 usb_c_policy=1 firmware_curve=1\n')
        self.destination.parent.mkdir()
        self.destination.write_text('# Previous default\noptions schenker_kbd writable=1 usb_c_policy=1 fan_policy=0 firmware_curve=1\n')
        self.install()
        self.assertEqual(self.destination.read_bytes(), self.source.read_bytes())

    def test_preserves_custom_module_options(self):
        self.source = self.source.with_name('schenker-kbd.modprobe.conf')
        self.source.write_text('options schenker_kbd writable=1 usb_c_policy=1 firmware_curve=1\n')
        self.destination.parent.mkdir()
        self.destination.write_text('options schenker_kbd writable=0 firmware_curve=0\n')
        self.install()
        self.assertEqual(self.destination.read_text(), 'options schenker_kbd writable=0 firmware_curve=0\n')
