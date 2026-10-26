import unittest
from unittest.mock import patch
from health_debug import HealthDebug


class HealthDebugTests(unittest.TestCase):
    def setUp(self):
        self.debug = HealthDebug()
        self.addCleanup(lambda: self.debug.control('stop'))

    def test_default_off_bound_expiry_and_no_stale_session(self):
        self.debug.record(None, 400, {'error': 'bad'}, 'secret')
        self.assertEqual(self.debug.snapshot()['entries'], [])
        self.debug.control('start'); session = self.debug.begin()
        for i in range(25): self.debug.record(session, 200, {'inserted': i}, 'x' * 9000, 9000, True)
        data = self.debug.snapshot()
        self.assertEqual(len(data['entries']), 20)
        self.assertEqual(data['entries'][0]['result']['inserted'], 5)
        self.assertEqual(len(data['entries'][0]['body_preview']), 8192)
        self.debug.control('clear'); self.assertTrue(self.debug.snapshot()['enabled'])
        self.assertEqual(self.debug.snapshot()['entries'], [])
        self.debug.control('start')
        self.debug.record(session, 400, {'error': 'stale'}, 'secret')
        self.assertEqual(self.debug.snapshot()['entries'], [])
        with patch('health_debug.time.monotonic', return_value=self.debug.deadline + 1):
            self.assertFalse(self.debug.snapshot()['enabled'])
        self.assertEqual(self.debug.snapshot()['entries'], [])

    def test_timer_clears_even_without_requests(self):
        with patch('health_debug.threading.Timer') as timer:
            self.debug.control('start')
            session = self.debug.begin()
            self.debug.record(session, 400, {'error': 'invalid', 'Authorization': 'secret'}, 'body')
            self.assertNotIn('Authorization', self.debug.snapshot()['entries'][0]['result'])
            seconds, callback, args = timer.call_args.args
            self.assertEqual(seconds, 600)
            callback(*args)
            self.assertFalse(self.debug.snapshot()['enabled'])
            self.assertEqual(self.debug.snapshot()['entries'], [])


if __name__ == '__main__': unittest.main()
