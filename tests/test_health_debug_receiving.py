import unittest

import health_debug


class ReceivingEntryTests(unittest.TestCase):
    def test_request_is_logged_on_arrival_and_completed_in_place(self):
        debug = health_debug.HealthDebug(duration=60)
        debug.control('start')
        session = debug.begin()
        entry = debug.start_request(session, 2_500_000)
        snap = debug.snapshot()['entries']
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]['status'], 'receiving')
        self.assertEqual(snap[0]['declared_bytes'], 2_500_000)
        self.assertIn('elapsed_ms', snap[0])
        self.assertNotIn('_started', snap[0])
        debug.record(session, 200, {'inserted': 3, 'received': 3}, '{}', 2_500_000, False, entry)
        snap = debug.snapshot()['entries']
        self.assertEqual(len(snap), 1)                       # same entry, not a second one
        self.assertEqual((snap[0]['status'], snap[0]['result']['inserted']), (200, 3))
        debug.control('stop')

    def test_nothing_is_logged_when_debug_is_off(self):
        debug = health_debug.HealthDebug(duration=60)
        self.assertIsNone(debug.start_request(debug.begin(), 10))
        self.assertEqual(debug.snapshot()['entries'], [])


if __name__ == '__main__':
    unittest.main()
