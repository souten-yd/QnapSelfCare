import tempfile
import unittest
from unittest.mock import Mock, patch
from storage import Store
from collectors import CollectorManager, BluetoothFailure


class DailySyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.user = self.store.save_user({'name': 'test'})
        self.devices = []
        for i in range(2):
            d = self.store.save_device({'model': 'HBF-228T', 'address': f'AA:BB:CC:DD:EE:0{i}',
                'bindings': {'1': self.user['id']}, 'auto_sync': True, 'interval': 300})
            self.store.pairing_key(d['address'], '11'*16)
            self.devices.append(d)

    def sync(self, manager, d, automatic):
        manager.submit('sync', d['id'], automatic=automatic)
        manager.process(manager.queue.get_nowait())

    def test_per_device_success_persists_restart_and_manual_is_unlimited(self):
        d, other = self.devices
        with patch('storage.now', return_value='2026-09-25T14:59:00+00:00'):
            manager = CollectorManager(self.store, runner=Mock(return_value={'records': []}))
            self.sync(manager, d, False)
            self.assertFalse(self.store.device(d['id'])['automatic_synced_today'])
            self.sync(manager, d, True)
            self.assertTrue(self.store.device(d['id'])['automatic_synced_today'])
            self.assertFalse(self.store.device(other['id'])['automatic_synced_today'])
            reopened = Store(self.tmp.name)
            restarted = CollectorManager(reopened, runner=Mock(return_value={'records': []}))
            with self.assertRaisesRegex(ValueError, '本日の自動同期'):
                restarted.submit('sync', d['id'], automatic=True)
            for _ in range(3):
                self.sync(restarted, d, False)
            self.sync(restarted, other, True)
            self.assertEqual(restarted.runner.call_count, 4)
            self.assertEqual(set(restarted.listener_status()['completed_today']), {d['id'], other['id']})

    def test_midnight_reenables_listener_and_clears_cross_day_cooldown(self):
        d = self.devices[0]
        response = {'supported': True, 'ready': True, 'events': [{'address': d['address'], 'at': 1}]}
        with patch('storage.now', return_value='2026-09-25T14:59:59+00:00') as wall, patch('collectors.time.monotonic', return_value=100) as clock, patch('collectors.bridge_request', return_value=response) as bridge:
            manager = CollectorManager(self.store, runner=Mock(return_value={'records': []}))
            self.sync(manager, d, True)
            manager.schedule()
            self.assertNotIn(d['address'], bridge.call_args.args[1]['addresses'])
            self.assertNotIn(d['id'], manager.listen_delayed)
            # Exactly 00:00 JST; not midnight of the NAS host's timezone or a rolling 24-hour window.
            wall.return_value = '2026-09-25T15:00:00+00:00'
            clock.return_value = 101
            response['events'][0]['at'] = 2
            manager.schedule()
            self.assertIn(d['address'], bridge.call_args.args[1]['addresses'])
            self.assertFalse(self.store.device(d['id'])['automatic_synced_today'])
            self.assertEqual(manager.listener_status()['waiting'][0]['seconds'], 60)
            clock.return_value = 161
            manager.schedule()
            manager.process(manager.queue.get_nowait())
            self.assertEqual(manager.runner.call_count, 2)
            self.assertTrue(self.store.device(d['id'])['automatic_synced_today'])

    def test_interval_scan_stops_after_success_and_failures_do_not_consume_day(self):
        d = self.devices[0]
        self.store.save_device(dict(d, sync_mode='interval'))
        self.store.save_device(dict(self.devices[1], auto_sync=False))
        with patch('storage.now', return_value='2026-09-25T10:00:00+00:00'), patch('collectors.time.monotonic', return_value=100) as clock:
            runner = Mock(side_effect=BluetoothFailure('read timeout'))
            manager = CollectorManager(self.store, runner=runner)
            self.sync(manager, d, True)
            self.assertFalse(self.store.device(d['id'])['automatic_synced_today'])
            runner.side_effect = None
            runner.return_value = {'records': []}
            self.sync(manager, d, True)
            clock.return_value = 10000
            manager.schedule()
            self.assertEqual(runner.call_count, 2)  # No background search once today's sync succeeded.
            self.assertTrue(manager.queue.empty())

    def test_daily_marker_survives_backup_and_queued_job_rechecks_gate(self):
        d = self.devices[0]
        with patch('storage.now', return_value='2026-09-25T10:00:00+00:00'):
            runner = Mock(return_value={'records': []})
            manager = CollectorManager(self.store, runner=runner)
            job = manager.submit('sync', d['id'], automatic=True)
            # A completion recorded after enqueue must still prevent duplicate BLE work.
            self.store.update_job(job['id'], 'done', automatic=True)
            manager.process(manager.queue.get_nowait())
            runner.assert_not_called()
            self.assertEqual(self.store.jobs()[0]['state'], 'skipped')
            with tempfile.TemporaryDirectory() as dest:
                restored = Store(dest)
                restored.restore(self.store.backup())
                self.assertTrue(restored.device(d['id'])['automatic_synced_today'])


if __name__ == '__main__':
    unittest.main()
