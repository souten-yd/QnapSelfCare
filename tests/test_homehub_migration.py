import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import homehub_migration as h
from durability import DataUnavailable, file_lock


class FakeMigration(h.Migration):
    def __init__(self, root):
        self.root = Path(root)
        self.compose_file = self.root / 'compose.yaml'
        self.calls = []
        self.fail = None
        self.healthy = True
        self.exists = False

    def command(self, args, timeout=60):
        self.calls.append(args)
        if args == self.fail:
            raise RuntimeError('injected Docker failure')
        if args[:2] == ['ps', '-a']:
            return 'qnaphomehub\nqnaphomehub-updater\n' + ('qnaphomehub-radio\n' if self.exists else '')
        return ''

    def inspect(self, service):
        return {'Config': {'Image': h.IMAGE + (':updater' if service == 'updater' else ':server'),
            'Labels': {'com.docker.compose.project': 'qnaphomehub', 'com.docker.compose.service': service,
                'com.docker.compose.project.working_dir': str(self.root),
                'com.docker.compose.project.config_files': str(self.compose_file),
                'org.opencontainers.image.version': '0.2.6'}},
            'State': {'Running': True}, 'Image': 'sha256:old-' + ('updater' if service == 'updater' else 'server'),
            'Mounts': [{'Type': 'bind', 'Destination': '/data', 'Source': str(self.root / 'data/homehub')}]}

    def updater_idle(self):
        pass

    def wait_healthy(self, shared, timeout=90):
        self.calls.append(['healthy', shared])
        if shared and not self.healthy:
            raise RuntimeError('radio unavailable')


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'homehub'
        self.root.mkdir()
        self.before = (h.ROOT / 'homehub/compose-legacy.yaml').read_bytes()
        (self.root / 'compose.yaml').write_bytes(self.before)
        for name in ('data/homehub', 'data/updater', 'data/matterbridge', 'secrets'):
            (self.root / name).mkdir(parents=True)
            (self.root / name / 'retain.json').write_text('{"original":true}')
        self.job = Path(self.tmp.name) / 'jobs' / ('a' * 32)
        self.job.mkdir(parents=True)
        self.phases = []
        self.migration = FakeMigration(self.root)
        self.patch_root = patch.object(h.os, 'geteuid', return_value=0)
        self.patch_arch = patch.object(h.platform, 'machine', return_value='x86_64')
        self.patch_root.start(); self.patch_arch.start()
        self.addCleanup(self.patch_root.stop); self.addCleanup(self.patch_arch.stop)

    def phase(self, phase, message, **extra):
        self.phases.append(phase)

    def test_standard_migration_preserves_data_and_stops_old_owner_before_new_radio(self):
        self.migration.run(self.job, self.phase)
        self.assertEqual((self.root / 'compose.yaml').read_bytes(), (h.ROOT / 'homehub/compose-shared.yaml').read_bytes())
        self.assertEqual((self.job / 'compose-before.yaml').read_bytes(), self.before)
        for name in ('data/homehub', 'data/updater', 'data/matterbridge', 'secrets'):
            self.assertEqual((self.root / name / 'retain.json').read_text(), '{"original":true}')
        self.assertFalse((self.job.parent / 'pending.json').exists())
        calls = self.migration.calls
        stop = next(i for i,c in enumerate(calls) if c[-2:] == ['stop', 'homehub'])
        up = next(i for i,c in enumerate(calls) if c[-2:] == ['radio', 'homehub'] and 'up' in c)
        self.assertLess(stop, up)
        self.assertNotIn('down', [x for c in calls for x in c])
        self.assertFalse(any('matterbridge' in c for c in calls))
        self.assertEqual((self.job / 'secrets/retain.json').stat().st_mode & 0o777, 0o600)

    def test_unknown_compose_and_override_abort_before_stop(self):
        (self.root / 'compose.yaml').write_bytes(self.before.replace(b'8787', b'9999'))
        with self.assertRaises(ValueError): self.migration.run(self.job, self.phase)
        self.assertFalse(any('stop' in c for c in self.migration.calls))
        (self.root / 'compose.yaml').write_bytes(self.before)
        (self.root / 'compose.override.yaml').write_text('services: {}')
        with self.assertRaises(ValueError): self.migration.run(self.job, self.phase)
        self.assertFalse(any('stop' in c for c in self.migration.calls))

    def test_wrong_project_and_newer_version_refuse_migration(self):
        item = self.migration.inspect('homehub')
        item['Config']['Labels']['com.docker.compose.project'] = 'other'
        with patch.object(self.migration, 'inspect', return_value=item):
            with self.assertRaises(ValueError): self.migration.preflight()
        item['Config']['Labels']['com.docker.compose.project'] = 'qnaphomehub'
        item['Config']['Labels']['org.opencontainers.image.version'] = '0.4.0'
        with patch.object(self.migration, 'inspect', return_value=item):
            with self.assertRaises(ValueError): self.migration.preflight()

    def test_pull_failure_leaves_running_services_and_config_untouched(self):
        self.migration.fail = ['pull', h.IMAGE + ':server-' + h.REVISION]
        with self.assertRaises(RuntimeError): self.migration.run(self.job, self.phase)
        self.assertEqual((self.root / 'compose.yaml').read_bytes(), self.before)
        self.assertFalse(any('stop' in c for c in self.migration.calls))
        self.assertFalse((self.job.parent / 'pending.json').exists())

    def test_radio_failure_restores_original_config_and_images(self):
        self.migration.healthy = False
        with self.assertRaises(RuntimeError): self.migration.run(self.job, self.phase)
        self.assertEqual((self.root / 'compose.yaml').read_bytes(), self.before)
        self.assertIn(['image', 'tag', 'sha256:old-server', h.IMAGE + ':server'], self.migration.calls)
        self.assertIn('rollback', self.phases)
        self.assertFalse((self.job.parent / 'pending.json').exists())

    def test_failed_rollback_keeps_recovery_marker(self):
        self.migration.healthy = False
        self.migration.fail = ['image', 'tag', 'sha256:old-server', h.IMAGE + ':server']
        with self.assertRaises(RuntimeError): self.migration.run(self.job, self.phase)
        self.assertTrue((self.job.parent / 'pending.json').exists())
        self.assertEqual((self.job / 'compose-before.yaml').read_bytes(), self.before)

    def test_new_compose_with_missing_radio_can_be_completed(self):
        (self.root / 'compose.yaml').write_bytes((h.ROOT / 'homehub/compose-shared.yaml').read_bytes())
        self.migration.run(self.job, self.phase)
        self.assertFalse((self.job.parent / 'pending.json').exists())

    def test_active_updater_or_low_space_refuses_mutation(self):
        with patch.object(self.migration, 'updater_idle', side_effect=DataUnavailable('busy')):
            with self.assertRaises(DataUnavailable): self.migration.run(self.job, self.phase)
        with patch.object(h.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(1, 1, 0)):
            with self.assertRaises(ValueError): self.migration.run(self.job, self.phase)
        self.assertFalse(any('stop' in c for c in self.migration.calls))

    def test_manager_interruption_exposes_restore_and_rejects_blind_retry(self):
        manager = h.MigrationManager(Path(self.tmp.name) / 'selfcare', self.root)
        manager.pending_file.write_text(json.dumps({'job': 'a' * 32}))
        manager.state_file.write_text(json.dumps({'phase': 'applying', 'backup': 'saved'}))
        restarted = h.MigrationManager(manager.data_root, self.root)
        self.assertTrue(restarted.status()['can_restore'])
        self.assertEqual(restarted.status()['phase'], 'failed')
        with self.assertRaises(DataUnavailable): restarted.apply('migrate')
        with self.assertRaises(ValueError): restarted.apply('arbitrary-command')

    def test_selfcare_update_lock_blocks_migration_before_docker(self):
        manager = h.MigrationManager(Path(self.tmp.name) / 'selfcare', self.root)
        with file_lock(manager.data_root / 'updates/update.lock'):
            with patch.object(h, 'Migration') as migration:
                manager.worker('migrate')
                migration.assert_not_called()
        self.assertEqual(manager.status()['phase'], 'failed')

    def test_explicit_restore_after_interruption_uses_retained_snapshot(self):
        self.migration.healthy = False
        self.migration.fail = ['image', 'tag', 'sha256:old-server', h.IMAGE + ':server']
        with self.assertRaises(RuntimeError): self.migration.run(self.job, self.phase)
        manager = h.MigrationManager(Path(self.tmp.name) / 'selfcare', self.root)
        shutil.copytree(self.job, manager.directory / self.job.name)
        manager.pending_file.write_text(json.dumps({'job': self.job.name}))
        self.migration.fail = None
        with patch.object(h, 'Migration', return_value=self.migration):
            manager.worker('restore')
        self.assertEqual(manager.status()['phase'], 'restored')
        self.assertFalse(manager.status()['can_restore'])
        self.assertEqual((self.root / 'compose.yaml').read_bytes(), self.before)

    def test_damaged_recovery_compose_is_rejected_before_stopping_services(self):
        self.migration.run(self.job, self.phase)
        (self.job / 'compose-before.yaml').write_text('damaged')
        self.migration.calls.clear()
        with self.assertRaises(ValueError):
            self.migration.rollback(self.job, h.read_json(self.job / 'original.json'))
        self.assertEqual(self.migration.calls, [])


if __name__ == '__main__': unittest.main()
