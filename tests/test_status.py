"""Status reads distinguish missing optional attributes from failed reads."""
import contextlib
import errno
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

source = Path(__file__).resolve().parents[1] / 'userspace/schenker-power'
loader = importlib.machinery.SourceFileLoader('power_status', str(source))
spec = importlib.util.spec_from_loader(loader.name, loader)
status = importlib.util.module_from_spec(spec)
loader.exec_module(status)


class StatusTests(unittest.TestCase):
    def test_missing_optional_attribute_is_unavailable(self):
        with patch.object(Path, 'read_text', side_effect=FileNotFoundError()):
            self.assertIsNone(status.read('/sys/missing'))

    def test_io_failure_is_not_silently_unavailable(self):
        with patch.object(Path, 'read_text', side_effect=OSError(errno.EIO, 'I/O error')):
            with self.assertRaises(OSError):
                status.read('/sys/broken')

    def test_permission_failure_has_actionable_error_and_nonzero_exit(self):
        error = PermissionError(errno.EACCES, 'Permission denied', '/sys/protected')
        output, errors = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['schenker-power', '--json']), \
             patch.object(Path, 'read_text', side_effect=error), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as raised:
                status.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(output.getvalue(), '')
        self.assertIn('/sys/protected', errors.getvalue())
        self.assertIn('sudo', errors.getvalue())
