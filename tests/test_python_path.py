"""The QPKG Python resolver must work with an explicit executable path."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


RESOLVER = Path(__file__).resolve().parents[1] / "qpkg/shared/python3-path"


class PythonPathTests(unittest.TestCase):
    def test_valid_override_returns_executable(self):
        env = dict(os.environ, SELFCARE_PYTHON=sys.executable)
        result = subprocess.run(["sh", str(RESOLVER)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), sys.executable)

    def test_invalid_override_does_not_silently_use_path(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, SELFCARE_PYTHON=str(Path(directory) / "missing"))
            result = subprocess.run(["sh", str(RESOLVER)], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SELFCARE_PYTHON", result.stderr)


if __name__ == "__main__":
    unittest.main()
