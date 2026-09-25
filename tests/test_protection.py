import json
import os
from pathlib import Path
import re
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile
from urllib.request import urlopen

from collectors import CollectorManager
from durability import CorruptDatabase, DataUnavailable, check_database, file_lock
import protection
from storage import Store


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'data', state_directory=self.root)
        self.user = self.store.save_user({'name': 'Test', 'id': 'person'})
        self.record = {'user_id': self.user['id'], 'kind': 'body_composition',
                       'measured_at': '2026-09-25T09:00:00+09:00', 'values': {'weight': 70}}
        self.store.add_records([self.record])
        self.manager = protection.ProtectionManager(self.store)

    def backup(self):
        name = self.manager.run_backup()
        self.assertIsNotNone(name, self.manager.status()['error'])
        return self.manager.directory / name

    def reopen(self):
        return Store(self.root / 'data', state_directory=self.root)

    def test_snapshot_includes_wal_keys_and_deletions_without_secrets_in_status(self):
        device = self.store.save_device({'name': 'Cuff', 'model': 'HEM-6232T', 'address': 'AA:BB:CC:DD:EE:FF',
            'adapter': 'hci0', 'bindings': {'1': 'person'}, 'transport': 'homehub', 'auto_sync': True,
            'interval': 300, 'utc_offset_minutes': 540})
        self.store.pairing_key(device['address'], 'f' * 32)
        record_id = self.store.records()['records'][0]['id']
        self.store.delete_record(record_id)
        config = self.root / 'config'
        config.mkdir()
        (config / 'pair-attempt.json').write_text(json.dumps({'address': device['address'], 'key': 'e' * 32}))
        # Keep a live connection with committed records in the WAL.
        db = sqlite3.connect(self.store.path)
        self.addCleanup(db.close)
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute("INSERT INTO users VALUES ('wal-user', 'WAL')")
        db.commit()
        self.assertGreater(Path(str(self.store.path) + '-wal').stat().st_size, 0)
        archive = self.backup()
        db.close()
        with tempfile.TemporaryDirectory() as work:
            data = protection.unpack_verified(archive, work)
            self.assertEqual(data['counts'], {'records': 0, 'users': 2})
            with sqlite3.connect(Path(work) / 'measurements.sqlite3') as snapshot:
                self.assertEqual(snapshot.execute('SELECT key FROM pairing').fetchone()[0], 'f' * 32)
                self.assertEqual(snapshot.execute('SELECT COUNT(*) FROM deleted_measurements').fetchone()[0], 1)
            self.assertTrue((Path(work) / 'config/pair-attempt.json').exists())
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('f' * 32, json.dumps(self.manager.status()))
        self.assertNotIn('e' * 32, json.dumps(self.manager.status()))
        protection.restore(self.root, archive)
        restored = self.reopen()
        self.assertFalse(restored.devices()[0]['auto_sync'])
        self.assertEqual(restored.add_records([self.record])['inserted'], 0)
        self.assertEqual(restored.pairing_key(device['address']), 'f' * 32)

    def test_retention_only_after_success_preserves_bad_and_unrelated_files(self):
        self.manager.save_settings({'enabled': True, 'interval_hours': 24, 'keep': 2})
        oldest = self.backup()
        self.backup()
        bad = self.manager.directory / 'selfcare-20000101T000000Z-aaaaaaaa.zip'
        bad.write_bytes(b'broken')
        other = self.manager.directory / 'family.zip'
        other.write_bytes(b'not-owned')
        with patch.object(protection, 'snapshot', side_effect=OSError('full')):
            self.assertIsNone(self.manager.run_backup())
        self.assertTrue(oldest.exists())
        self.assertFalse(self.store.error)
        self.backup()
        self.assertFalse(oldest.exists())
        self.assertTrue(bad.exists())
        self.assertTrue(other.exists())
        self.assertEqual(len([b for b in self.manager.list_backups() if b['readable']]), 2)

    def test_failure_before_publication_leaves_no_completed_archive(self):
        with patch.object(protection, 'unpack_verified', side_effect=ValueError('verification failed')):
            self.assertIsNone(self.manager.run_backup())
        self.assertEqual(self.manager.list_backups(), [])
        self.assertEqual(list(self.manager.directory.glob('.working-*')), [])
        self.assertIn('verification failed', self.manager.status()['error'])
        with patch.object(protection, 'unpack_verified', side_effect=CorruptDatabase('bad destination')):
            self.assertIsNone(self.manager.run_backup())
        self.assertFalse(self.store.error)

    def test_disk_full_and_update_lock_do_not_destroy_backups(self):
        archive = self.backup()
        original = archive.read_bytes()
        with patch.object(protection.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100, 100, 0)):
            self.assertIsNone(self.manager.run_backup())
        with file_lock(self.root / 'updates/update.lock'):
            self.assertIsNone(self.manager.run_backup())
            self.assertEqual(self.manager.status()['phase'], 'waiting')
            self.assertIsNone(self.manager.status()['error'])
            self.assertGreater(self.manager.status()['next_at'], time.time() + 50)
        self.assertEqual(archive.read_bytes(), original)

    def test_corrupt_source_protected_across_restart_and_recoverable(self):
        archive = self.backup()
        self.store.path.write_bytes(b'corrupted original')
        with self.assertRaises(DataUnavailable):
            self.store.users()
        self.assertIsNone(self.manager.run_backup())
        self.assertEqual(self.store.path.read_bytes(), b'corrupted original')
        failed = self.reopen()
        self.assertTrue(failed.error)
        self.assertTrue(self.manager.diagnostics()['recovery_required'])
        with self.assertRaises(DataUnavailable):
            CollectorManager(failed).submit('scan')
        recovery = Path(protection.restore(self.root, archive))
        self.assertEqual((recovery / 'data/measurements.sqlite3').read_bytes(), b'corrupted original')
        self.assertEqual(self.reopen().records()['total'], 1)

    def test_missing_or_zero_byte_db_is_not_silently_recreated(self):
        self.store.path.unlink()
        failed = self.reopen()
        self.assertTrue(failed.error)
        self.assertFalse(failed.path.exists())
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'measurements.sqlite3'
            path.touch()
            failed = Store(root)
            self.assertTrue(failed.error)
            self.assertEqual(path.stat().st_size, 0)

    def test_schema_and_foreign_keys_are_checked(self):
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE measurements SET user_id='missing'")
        with self.assertRaises(CorruptDatabase):
            check_database(self.store.path)
        self.assertTrue(self.manager.diagnostics()['recovery_required'])

    def rewrite_archive(self, archive, mutate):
        with zipfile.ZipFile(archive) as source:
            data = {name: source.read(name) for name in source.namelist()}
        mutate(data)
        with zipfile.ZipFile(archive, 'w') as target:
            for name, value in data.items():
                target.writestr(name, value)

    def test_bad_hash_refuses_download_and_restore_without_touching_current_data(self):
        archive = self.backup()
        original = self.store.path.read_bytes()
        self.rewrite_archive(archive, lambda data: data.__setitem__('measurements.sqlite3', b'x' + data['measurements.sqlite3'][1:]))
        with self.assertRaises(ValueError):
            protection.restore(self.root, archive)
        with self.assertRaises(ValueError):
            with self.manager.download(archive.name):
                self.fail('invalid download must not open')
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertFalse((self.root / 'restore.pending.json').exists())

    def test_path_traversal_and_unknown_archive_names_rejected(self):
        archive = self.backup()
        self.rewrite_archive(archive, lambda data: data.__setitem__('../escape', b'bad'))
        with self.assertRaises(ValueError):
            protection.restore(self.root, archive)
        self.assertFalse((self.root / 'escape').exists())
        with self.assertRaises(ValueError):
            with self.manager.download('../status.json'):
                self.fail('invalid path must not open')

    def test_reverification_reports_bit_rot_without_blocking_healthy_database(self):
        archive = self.backup()
        self.assertTrue(self.manager.verify_backup(archive.name)['ok'])
        self.rewrite_archive(archive, lambda data: data.__setitem__('measurements.sqlite3', b'x' + data['measurements.sqlite3'][1:]))
        self.assertFalse(self.manager.verify_backup(archive.name)['ok'])
        self.assertFalse(self.store.error)
        self.assertTrue(archive.exists())
        restarted = protection.ProtectionManager(self.store)
        self.assertFalse(restarted.status()['verification']['ok'])
        self.assertEqual(next(c for c in restarted.diagnostics()['checks'] if c['id'] == 'verification')['level'], 'error')

    def test_restore_rejected_while_service_or_updater_is_running(self):
        archive = self.backup()
        for path in (self.root / 'service.lock', self.root / 'updates/update.lock'):
            with file_lock(path):
                with self.assertRaises(DataUnavailable):
                    protection.restore(self.root, archive)
        self.assertEqual(self.store.records()['total'], 1)

    def test_interrupted_restore_keeps_original_and_blocks_initialization_until_retry(self):
        archive = self.backup()
        original = self.store.path.read_bytes()
        real_replace = os.replace
        def interrupt(source, target):
            if Path(target) == self.root / 'data':
                raise OSError('simulated power loss')
            return real_replace(source, target)
        with patch.object(protection.os, 'replace', side_effect=interrupt):
            with self.assertRaises(OSError):
                protection.restore(self.root, archive)
        self.assertTrue(self.reopen().error)
        originals = list((self.root / 'recovery').glob('*/data/measurements.sqlite3'))
        self.assertEqual(originals[0].read_bytes(), original)
        protection.restore(self.root, archive)
        self.assertEqual(self.reopen().records()['total'], 1)
        self.assertEqual(originals[0].read_bytes(), original)

    def test_scheduler_first_backup_restart_persistence_and_failed_backoff(self):
        self.manager.start()
        try:
            deadline = time.monotonic() + 5
            while (not self.manager.status()['last_success'] or self.manager.busy) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(len(self.manager.list_backups()), 1)
        finally:
            self.manager.close()
        restarted = protection.ProtectionManager(self.store)
        self.assertGreater(restarted.status()['next_at'], time.time() + 6 * 24 * 3600)
        with patch.object(protection, 'snapshot', side_effect=OSError('full')):
            restarted.run_backup()
        self.assertGreater(restarted.status()['next_at'], time.time() + 3500)
        restarted.save_settings({'enabled': False, 'interval_hours': 6, 'keep': 7})
        restarted = protection.ProtectionManager(self.store)
        self.assertIsNone(restarted.status()['next_at'])
        self.assertEqual(restarted.config['keep'], 7)
        self.assertFalse(restarted.config['enabled'])

    def test_overdue_backup_clock_rollback_and_missing_archives(self):
        old_time = time.time() - 8 * 24 * 3600
        with patch.object(protection.time, 'time', return_value=old_time):
            archive = self.backup()
        self.assertLess(self.manager.status()['next_at'], time.time())
        with patch.object(protection.time, 'time', return_value=old_time - 3600):
            self.assertLess(self.manager.status()['next_at'], old_time - 3600)
        archive.unlink()
        self.assertIsNone(self.manager.status()['last_success'])

    def test_duplicate_request_and_broken_settings(self):
        self.manager.request_backup()
        with self.assertRaises(DataUnavailable):
            self.manager.request_backup()
        self.manager.config_path.write_text('broken')
        restarted = protection.ProtectionManager(self.store)
        self.assertTrue(restarted.status()['config_error'])
        self.assertIsNone(restarted.status()['next_at'])
        restarted.save_settings(protection.DEFAULTS)
        self.assertIsNone(restarted.status()['config_error'])
        with self.assertRaises(ValueError):
            restarted.save_settings({'enabled': True, 'interval_hours': 24, 'keep': 1})

    def test_missing_shared_radio_is_not_mislabeled_as_native(self):
        collector = CollectorManager(self.store)
        with patch('collectors.HOMEHUB_SOCKET', self.root / 'missing.sock'):
            status = collector.diagnostics()
        self.assertEqual(status['mode'], 'unavailable')
        self.assertEqual(status['expected_mode'], 'homehub')
        self.assertIn('追加は不要', status['guidance'][0])

    def test_real_web_process_boots_in_recovery_mode_without_replacing_corrupt_db(self):
        self.store.path.write_bytes(b'original corruption')
        process = subprocess.Popen([sys.executable, 'webapp.py', '--data-dir', str(self.root), '--port', '0'],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 8)[0], 'Web startup timed out')
            line = process.stdout.readline()
            port = re.search(r"listening on \('127\.0\.0\.1', (\d+)\)", line).group(1)
            with urlopen('http://127.0.0.1:' + port + '/api/status', timeout=3) as response:
                self.assertTrue(json.load(response)['recovery_required'])
            with urlopen('http://127.0.0.1:' + port + '/api/protection', timeout=3) as response:
                self.assertTrue(json.load(response)['recovery_required'])
            self.assertEqual(self.store.path.read_bytes(), b'original corruption')
        finally:
            process.terminate()
            process.wait(timeout=8)
            process.stdout.close()


if __name__ == '__main__':
    unittest.main()
