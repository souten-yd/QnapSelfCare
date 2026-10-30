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
            shutil.copy(SERVICE / "selfcare_pid.py", root / "selfcare_pid.py")
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


    def test_reconcile_orphan_from_deleted_qts_temporary_cwd(self):
        # The real NAS showed a prior QPKG's cwd at
        # .../.tmp-QnapSelfCare (deleted), although argv points to the
        # installed webapp.py. A missing PID file must not cause overlap.
        with tempfile.TemporaryDirectory() as directory:
            top = Path(directory)
            root = top / "QnapSelfCare"
            root.mkdir()
            for file in ("selfcare.sh", "selfcare_pid.py", "python3-path"):
                shutil.copy(SERVICE / file, root / file)
            (root / "webapp.py").write_text("import time\nprint('alive', flush=True)\ntime.sleep(30)\n")
            data_dir = top / "Container" / "QnapSelfCare"
            data_dir.parent.mkdir()
            env = dict(os.environ, SELFCARE_PYTHON=sys.executable,
                       SELFCARE_DATA_DIR=str(data_dir))
            temp_cwd = top / ".tmp-QnapSelfCare"
            temp_cwd.mkdir()
            old = subprocess.Popen(
                [sys.executable, str(root / "webapp.py"), "--lan",
                 "--port", "17863", "--data-dir", str(data_dir)],
                cwd=temp_cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                import time
                time.sleep(0.2)
                temp_cwd.rmdir()
                self.assertIn(" (deleted)", os.readlink("/proc/%s/cwd" % old.pid))
                command = ["sh", str(root / "selfcare.sh")]
                result = subprocess.run(command + ["start"], env=env,
                                        capture_output=True, text=True, timeout=18)
                self.assertEqual(result.returncode, 0, result.stderr)
                old.wait(timeout=5)
                current_pid = int((root / "selfcare.pid").read_text())
                self.assertNotEqual(current_pid, old.pid)
                self.assertEqual(subprocess.run(command + ["status"], env=env).returncode, 0)
                self.assertEqual(os.readlink("/proc/%s/cwd" % current_pid), str(root))
                stopped = subprocess.run(command + ["stop"], env=env,
                                         capture_output=True, text=True, timeout=18)
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                self.assertFalse((root / "selfcare.pid").exists())
            finally:
                if old.poll() is None:
                    old.terminate()
                    old.wait(timeout=5)
                subprocess.run(["sh", str(root / "selfcare.sh"), "stop"],
                               env=env, capture_output=True, timeout=18)

    def test_stale_pid_file_does_not_kill_foreign_process(self):
        with tempfile.TemporaryDirectory() as directory:
            top = Path(directory)
            root = top / "QnapSelfCare"
            root.mkdir()
            for file in ("selfcare.sh", "selfcare_pid.py", "python3-path"):
                shutil.copy(SERVICE / file, root / file)
            (root / "webapp.py").write_text("import time\ntime.sleep(30)\n")
            data_dir = top / "Container" / "QnapSelfCare"
            data_dir.parent.mkdir()
            foreign_cwd = top / "foreign"
            foreign_cwd.mkdir()
            env = dict(os.environ, SELFCARE_PYTHON=sys.executable,
                       SELFCARE_DATA_DIR=str(data_dir))
            foreign = subprocess.Popen(
                [sys.executable, str(root / "webapp.py"), "--lan",
                 "--port", "17863", "--data-dir", str(data_dir)],
                cwd=foreign_cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                (root / "selfcare.pid").write_text(str(foreign.pid))
                result = subprocess.run(["sh", str(root / "selfcare.sh"), "stop"],
                                        env=env, capture_output=True, text=True, timeout=12)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNone(foreign.poll(), "unowned process must never be killed")
                self.assertFalse((root / "selfcare.pid").exists())
            finally:
                foreign.terminate()
                foreign.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
