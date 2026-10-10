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

    def test_polling_reads_are_cached_but_job_writes_are_immediate(self):
        from unittest.mock import patch
        d = self.devices[0]
        first = self.store.devices()
        with patch.object(self.store, 'connect', side_effect=AssertionError('unnecessary database read')):
            self.assertEqual(self.store.devices(), first)
        job = self.store.create_job(d['id'], 'sync')
        self.assertEqual(self.store.jobs()[0]['state'], 'queued')
        self.store.update_job(job, 'done', 'completed')
        self.assertEqual(self.store.jobs()[0]['state'], 'done')
        self.assertEqual(self.store.device(d['id'])['id'], d['id'])

    def test_interval_success_persists_restart_and_manual_is_unlimited(self):
        d, other = self.devices
        d = self.store.save_device(dict(d, sync_mode='interval'))
        other = self.store.save_device(dict(other, sync_mode='interval'))
        with patch('storage.now', return_value='2026-09-25T14:59:00+00:00'):
            manager = CollectorManager(self.store, runner=Mock(return_value={'records': []}))
            self.sync(manager, d, False)
            self.assertFalse(self.store.device(d['id'])['automatic_synced_today'])
            self.assertNotIn('diagnostic', manager.runner.call_args.args[0])
            self.sync(manager, d, True)
            self.assertTrue(manager.runner.call_args.args[0]['diagnostic'])
            self.assertTrue(self.store.device(d['id'])['automatic_synced_today'])
            self.assertFalse(self.store.device(other['id'])['automatic_synced_today'])
            reopened = Store(self.tmp.name)
            restarted = CollectorManager(reopened, runner=Mock(return_value={'records': []}))
            with self.assertRaisesRegex(ValueError, '本日の周期検索'):
                restarted.submit('sync', d['id'], automatic=True)
            for _ in range(3):
                self.sync(restarted, d, False)
            self.sync(restarted, other, True)
            self.assertEqual(restarted.runner.call_count, 4)
            self.assertEqual(set(restarted.listener_status()['completed_today']), {d['id'], other['id']})

    def test_hbf_success_only_cooldown_survives_restart(self):
        from datetime import datetime, timezone
        d = self.devices[0]
        runner = Mock(return_value={'records': []})
        manager = CollectorManager(self.store, runner=runner)
        advert = 1790000000000
        response = {'supported': True, 'ready': True, 'events': [{'address': d['address'], 'at': advert}]}
        stamped = datetime.fromtimestamp((advert + 1000)/1000, timezone.utc).isoformat()
        with patch('collectors.bridge_request', return_value=response), \
             patch('collectors.time.monotonic', return_value=100) as clock, \
             patch('storage.now', return_value=stamped):
            manager.schedule()
            manager.process(manager.queue.get_nowait())
            self.assertEqual(runner.call_count, 1)
        restarted_store = Store(self.tmp.name)
        restarted = CollectorManager(restarted_store, runner=Mock(return_value={'records': []}))
        with patch('collectors.bridge_request', return_value=response), \
             patch('collectors.time.monotonic', return_value=200) as clock:
            response['events'][0]['at'] = advert + 95000
            restarted.schedule()
            self.assertTrue(restarted.queue.empty())
            response['events'][0]['at'] = advert + 3599000
            clock.return_value = 400
            restarted.schedule()
            self.assertTrue(restarted.queue.empty())
            response['events'][0]['at'] = advert + 3602000
            clock.return_value = 402
            restarted.schedule()
            self.assertEqual(restarted.queue.qsize(), 1)
        with tempfile.TemporaryDirectory() as dest:
            restored = Store(dest)
            restored.restore(self.store.backup())
            self.assertEqual(restored.last_listener_sync(d['id']), stamped)

    def test_hbf_failure_has_no_success_cooldown_or_retry_count_limit(self):
        d = self.devices[0]
        runner = Mock(side_effect=BluetoothFailure('機器が見つかりません。機器を通信可能な状態にし、NASへ近づけてください'))
        manager = CollectorManager(self.store, runner=runner)
        response = {'supported': True, 'ready': True, 'events': [{'address': d['address'], 'at': 1790000000000}]}
        with patch('collectors.bridge_request', return_value=response), \
             patch('collectors.time.monotonic', return_value=100) as clock:
            for i in range(4):
                response['events'][0]['at'] = 1790000000000 + i * 10000
                clock.return_value = 100 + i * 10
                manager.schedule()
                manager.process(manager.queue.get_nowait())
            self.assertEqual(runner.call_count, 4)
            self.assertIsNone(self.store.last_listener_sync(d['id']))

    def test_interval_midnight_releases_daily_gate(self):
        d = self.store.save_device(dict(self.devices[0], sync_mode='interval'))
        with patch('storage.now', return_value='2026-09-25T14:59:59+00:00') as wall:
            manager = CollectorManager(self.store, runner=Mock(return_value={'records': []}))
            self.sync(manager, d, True)
            self.assertTrue(self.store.device(d['id'])['automatic_synced_today'])
            manager.next_attempt[d['id']] = 999999
            wall.return_value = '2026-09-25T15:00:00+00:00'
            with patch('collectors.time.monotonic', return_value=100):
                manager.schedule()
            self.assertFalse(self.store.device(d['id'])['automatic_synced_today'])
            self.assertEqual(manager.runner.call_count, 2)
            self.assertLess(manager.next_attempt[d['id']], 999999)

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
        d = self.store.save_device(dict(self.devices[0], sync_mode='interval'))
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
