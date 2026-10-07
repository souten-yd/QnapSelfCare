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

    def test_listener_survives_restart_and_dedupes_same_advertisement_burst(self):
        d = self.devices[0]
        # Simulate a completion marker left by an older release. Listener mode
        # must ignore it after restart and remain eligible without a browser.
        legacy = self.store.create_job(d['id'], 'sync')
        self.store.update_job(legacy, 'done', automatic=True)
        reopened = Store(self.tmp.name)
        self.assertFalse(reopened.device(d['id'])['automatic_synced_today'])
        runner = Mock(return_value={'records': []})
        manager = CollectorManager(reopened, runner=runner)
        advert = 1_790_000_000_000
        response = {'supported': True, 'ready': True,
                    'events': [{'address': d['address'], 'at': advert}]}
        with patch('collectors.bridge_request', return_value=response), \
                patch('collectors.time.monotonic', return_value=100) as clock, \
                patch('collectors.time.time', return_value=advert / 1000 + 1):
            manager.schedule()
            manager.process(manager.queue.get_nowait())
            self.assertEqual(runner.call_count, 1)
            self.assertFalse(reopened.device(d['id'])['automatic_synced_today'])
            self.assertEqual(manager.listener_status()['completed_today'], [])

            # Repeated advertisements in the same measurement burst do not
            # re-read all history.
            response['events'][0]['at'] = advert + 5000
            clock.return_value = 105
            manager.schedule()
            self.assertTrue(manager.queue.empty())
            self.assertEqual(runner.call_count, 1)

            # A 90-second quiet gap opens a new burst on the same day.
            response['events'][0]['at'] = advert + 95000
            clock.return_value = 195
            manager.schedule()
            manager.process(manager.queue.get_nowait())
            self.assertEqual(runner.call_count, 2)
            self.assertEqual([j['state'] for j in reopened.jobs()[:2]], ['done', 'done'])

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
