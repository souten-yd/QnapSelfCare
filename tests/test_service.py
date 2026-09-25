"""Exercise the QPKG service lifecycle without requiring a QNAP device."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SERVICE = Path(__file__).resolve().parents[1] / "qpkg/shared"


class ServiceTests(unittest.TestCase):
    def test_start_and_stop_without_nohup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(SERVICE / "selfcare.sh", root / "selfcare.sh")
            shutil.copy(SERVICE / "python3-path", root / "python3-path")
            (root / "webapp.py").write_text(
                "import time\nprint('service started', flush=True)\ntime.sleep(30)\n"
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            (bin_dir / "nohup").write_text("#!/bin/sh\nexit 79\n")
            (bin_dir / "nohup").chmod(0o755)
            data_dir = root / "Container" / "QnapSelfCare"
            data_dir.parent.mkdir()
            env = dict(os.environ, SELFCARE_PYTHON=sys.executable,
                       SELFCARE_DATA_DIR=str(data_dir),
                       PATH=f"{bin_dir}:{os.environ['PATH']}")
            command = ["sh", str(root / "selfcare.sh")]
            try:
                started = subprocess.run(command + ["start"], env=env,
                                         capture_output=True, text=True, timeout=10)
                self.assertEqual(started.returncode, 0, started.stderr)
                self.assertIn("service started", (data_dir / "logs/selfcare.log").read_text())
                self.assertTrue((root / "selfcare.pid").exists())
            finally:
                subprocess.run(command + ["stop"], env=env,
                               capture_output=True, timeout=10)
            self.assertFalse((root / "selfcare.pid").exists())


if __name__ == "__main__":
    unittest.main()
