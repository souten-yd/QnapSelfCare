import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import self_update as update
import updater
from storage import Store

ROOT = Path(__file__).resolve().parents[1]


class SelfUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = update.UpdateManager(self.root, ROOT, '0.3.1', 'x86_64')
        self.store = Store(self.root / 'data')
        user = self.store.save_user({'name': 'Test'})
        self.store.add_records([{'user_id':user['id'], 'kind':'body_composition',
                                'measured_at':'2026-09-25T09:00:00+09:00', 'values':{'weight':70}}])
        self.identifier = 'a' * 32
        self.job = self.manager.directory / self.identifier
        self.job.mkdir()
        update.write_json(self.job / 'request.json', dict(id=self.identifier, current='0.3.1', target='v0.3.2',
            arch='x86_64', data_root=str(self.root), install_root=str(ROOT), port=17863))
        update.write_json(self.manager.state_file, dict(id=self.identifier, phase='queued', target='v0.3.2'))
        self.payload = b'#!/bin/sh\nexit 0\n'
        self.asset = dict(version='v0.3.2', name='QnapSelfCare_0.3.2_x86_64.qpkg', size=len(self.payload),
            sha256=hashlib.sha256(self.payload).hexdigest(), url='https://github.com/souten-yd/QnapSelfCare/releases/download/v0.3.2/QnapSelfCare_0.3.2_x86_64.qpkg')

    def run_worker(self, code=0, download=None, health_error=None, timeout=False, asset=None, stop_error=None):
        child = Mock(pid=os.getpid())
        child.wait.side_effect = subprocess.TimeoutExpired('installer', 600) if timeout else None
        child.wait.return_value = code
        child.poll.return_value = None if timeout else code
        fd = self.manager.lock()
        try:
            with patch.object(update, 'check_environment'), patch.object(updater, 'latest', return_value=asset or self.asset), \
                 patch.object(updater.urllib.request, 'urlopen', return_value=io.BytesIO(self.payload if download is None else download)), \
                 patch.object(update.subprocess, 'Popen', return_value=child) as execute, \
                 patch.object(update.subprocess, 'run', side_effect=stop_error), \
                 patch.object(update, 'getcfg', return_value='FALSE'), \
                 patch.object(update, 'wait_for_version', side_effect=health_error) as health:
                result = update.run_job(self.job, fd)
                return result, execute, health, child
        finally:
            os.close(fd)

    def test_verified_update_backs_up_records_and_requires_healthy_new_version(self):
        result, execute, health, _ = self.run_worker()
        self.assertEqual(result, 0)
        args, kwargs = execute.call_args
        self.assertEqual(args[0], ['/bin/sh', str(self.job / self.asset['name'])])
        self.assertTrue(kwargs['close_fds'])
        self.assertNotIn('pass_fds', kwargs)  # Newly started Web process cannot inherit the update lock.
        self.assertEqual(kwargs['env']['QINSTALL_PATH'], str(ROOT.parent))
        self.assertEqual(kwargs['env']['SELFCARE_DATA_DIR'], str(self.root))
        health.assert_called_once_with('v0.3.2', 17863)
        state = update.read_json(self.manager.state_file)
        self.assertEqual(state['phase'], 'success')
        with sqlite3.connect(Path(state['backup']) / 'measurements.sqlite3') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM measurements').fetchone()[0], 1)
        self.assertEqual(self.store.records()['total'], 1)
        self.assertFalse((self.job / self.asset['name']).exists())

    def test_bad_digest_never_executes_installer(self):
        result, execute, health, _ = self.run_worker(download=b'unverified')
        self.assertEqual(result, 1)
        execute.assert_not_called(); health.assert_not_called()
        self.assertEqual(update.read_json(self.manager.state_file)['phase'], 'failed')
        self.assertFalse((self.job / self.asset['name']).exists())
        self.assertEqual(self.store.records()['total'], 1)

    def test_qdk_exit_10_is_only_a_candidate_for_success(self):
        result, _, health, _ = self.run_worker(code=10)
        self.assertEqual(result, 0)
        health.assert_called_once_with('v0.3.2', 17863)

    def test_changed_release_never_executes_an_unselected_version(self):
        result, execute, _, _ = self.run_worker(asset=dict(self.asset, version='v0.3.3'))
        self.assertEqual(result, 1); execute.assert_not_called()

    def test_backup_failure_never_stops_or_installs(self):
        with patch.object(update, 'backup_data', side_effect=OSError('disk full')):
            result, execute, _, _ = self.run_worker()
        self.assertEqual(result, 1); execute.assert_not_called()
        self.assertEqual(self.store.records()['total'], 1)

    def test_stop_failure_prevents_qpkg_replacement(self):
        result, execute, _, _ = self.run_worker(stop_error=subprocess.CalledProcessError(1, 'stop'))
        self.assertEqual(result, 1); execute.assert_not_called()

    def test_health_waits_for_new_web_version_after_registration(self):
        client = Mock()
        client.open.side_effect = [io.BytesIO(b'{"version":"0.3.1","record_count":1}'),
                                   io.BytesIO(b'{"version":"0.3.2","record_count":1}')]
        with patch.object(update, 'getcfg', return_value='0.3.2'), \
             patch.object(update.urllib.request, 'build_opener', return_value=client), \
             patch.object(update.time, 'sleep'):
            update.wait_for_version('v0.3.2', 17863)
        self.assertEqual(client.open.call_count, 2)

    def test_old_registration_cannot_pass_health_check(self):
        client = Mock()
        with patch.object(update, 'getcfg', return_value='0.3.1'), \
             patch.object(update.urllib.request, 'build_opener', return_value=client), \
             patch.object(update.time, 'monotonic', side_effect=[0, 0, 121]), \
             patch.object(update.time, 'sleep'), self.assertRaises(ValueError):
            update.wait_for_version('v0.3.2', 17863)
        client.open.assert_not_called()

    def test_nonzero_installer_and_failed_health_are_not_success(self):
        for code, health_error in ((1, None), (10, ValueError('not healthy'))):
            with self.subTest(code=code):
                # Each attempt has its own directory in production.
                import shutil
                shutil.rmtree(self.job / 'backup', ignore_errors=True)
                (self.job / self.asset['name']).unlink(missing_ok=True)
                result, _, _, _ = self.run_worker(code=code, health_error=health_error)
                self.assertEqual(result, 1)
                self.assertEqual(update.read_json(self.manager.state_file)['phase'], 'failed')
                self.assertEqual(self.store.records()['total'], 1)

    def test_timeout_blocks_retry_while_installer_is_still_alive(self):
        result, _, _, child = self.run_worker(timeout=True)
        self.assertEqual(result, 1)
        child.kill.assert_not_called(); child.terminate.assert_not_called()
        with patch.object(update, 'check_environment'):
            state = self.manager.status()
            self.assertTrue(state['busy'])
            with self.assertRaises(update.UpdateConflict):
                self.manager.apply('v0.3.2')
        self.assertEqual(state['phase'], 'failed')

    def test_lock_blocks_double_click_and_survives_manager_recreation(self):
        fd = self.manager.lock()
        try:
            other = update.UpdateManager(self.root, ROOT, '0.3.1', 'x86_64')
            with patch.object(update, 'check_environment'), patch.object(update.subprocess, 'Popen') as spawn:
                self.assertTrue(other.status()['busy'])
                with self.assertRaises(update.UpdateConflict):
                    other.apply('v0.3.2')
                spawn.assert_not_called()
        finally:
            os.close(fd)

    def test_lost_worker_is_failed_without_automatic_reexecution(self):
        with patch.object(update, 'check_environment'), patch.object(update.subprocess, 'Popen') as spawn:
            state = self.manager.status()
            self.assertFalse(state['busy']); self.assertEqual(state['phase'], 'failed')
            spawn.assert_not_called()

    def test_arbitrary_target_and_non_qnap_environment_are_rejected(self):
        for value in ('https://evil.example/a.qpkg', '0.3.0', '0.3.1', 'v0.3.2;sh', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.manager.apply(value)
        with patch.object(update.os, 'geteuid', return_value=1000), self.assertRaises(ValueError):
            self.manager.apply('v0.3.2')

    def test_spawn_failure_releases_lock_and_keeps_error(self):
        with patch.object(update, 'check_environment'), patch.object(update.subprocess, 'Popen', side_effect=OSError('spawn failed')):
            with self.assertRaises(OSError): self.manager.apply('v0.3.2')
            self.assertFalse(self.manager.status()['busy'])
            self.assertEqual(self.manager.status()['phase'], 'failed')

    def test_worker_snapshot_outlives_web_parent_and_original_script(self):
        install = self.root / 'install'
        install.mkdir()
        (install / 'updater.py').write_text('')
        (install / 'durability.py').write_text('')
        # A harmless worker uses the same inherited lock and detached launch path.
        (install / 'self_update.py').write_text('''import json,os,pathlib,sys,time
job=pathlib.Path(sys.argv[2]); fd=int(sys.argv[4]); os.fstat(fd)
(job/'ready').write_text(str(os.getpid()))
deadline=time.monotonic()+8
while not (job/'finish').exists() and time.monotonic()<deadline: time.sleep(.02)
(job.parent/'status.new').write_text(json.dumps({'id':job.name,'phase':'success'}))
(job.parent/'status.new').replace(job.parent/'status.json')
os.close(fd)
''')
        parent = '''import sys
from unittest.mock import patch
from self_update import UpdateManager
with patch('self_update.check_environment'):
 print(UpdateManager(sys.argv[1],sys.argv[2],'0.3.1','x86_64').apply('v0.3.2')['id'],flush=True)
'''
        result = subprocess.run([sys.executable, '-c', parent, str(self.root), str(install)], cwd=ROOT,
                                capture_output=True, text=True, timeout=5, check=True)
        job = self.manager.directory / result.stdout.strip()
        deadline = time.monotonic() + 4
        try:
            while not (job / 'ready').exists() and time.monotonic() < deadline: time.sleep(.02)
            self.assertTrue((job / 'ready').exists())
            (install / 'self_update.py').unlink()
            with patch.object(update, 'check_environment'):
                self.assertTrue(self.manager.status()['busy'])
            self.assertTrue((job / 'self_update.py').is_file())
        finally:
            (job / 'finish').touch()
            while update.read_json(self.manager.state_file).get('phase') != 'success' and time.monotonic() < deadline: time.sleep(.02)
        self.assertEqual(update.read_json(self.manager.state_file)['phase'], 'success')


if __name__ == '__main__':
    unittest.main()
