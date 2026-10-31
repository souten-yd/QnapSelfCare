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

    def test_timer_stops_and_retains_without_requests(self):
        with patch('health_debug.threading.Timer') as timer:
            self.debug.control('start')
            session = self.debug.begin()
            self.debug.record(session, 400, {'error': 'invalid', 'Authorization': 'secret'}, 'body')
            self.assertNotIn('Authorization', self.debug.snapshot()['entries'][0]['result'])
            seconds, callback, args = timer.call_args.args
            self.assertEqual(seconds, 600)
            callback(*args)
            self.assertFalse(self.debug.snapshot()['enabled'])
            self.assertEqual(len(self.debug.snapshot()['entries']), 1)


if __name__ == '__main__': unittest.main()

class FullExportTests(unittest.TestCase):
    def test_full_export_expiry_and_invalid_utf8(self):
        debug = HealthDebug()
        self.addCleanup(lambda: debug.control('stop'))
        debug.control('start')
        session = debug.begin()
        raw = ('心拍' * 6000).encode()
        for i in range(25):
            entry = debug.start_request(session, len(raw))
            debug.capture(session, entry, raw)
            debug.record(session, 400, {'error': 'bad'}, 'preview', len(raw), True, entry)
        self.assertEqual(len(debug.snapshot()['entries']), 20)
        self.assertNotIn('body', debug.snapshot()['entries'][0])
        debug._expire(session)
        data = debug.snapshot(full=True)
        self.assertFalse(data['enabled'])
        self.assertEqual(len(data['entries']), 25)
        self.assertTrue(all(e['body'].encode() == raw for e in data['entries']))
        self.assertTrue(data['complete'])
        debug.control('start'); session = debug.begin()
        entry = debug.start_request(session)
        debug.capture(session, entry, b'\xff')
        self.assertEqual(debug.snapshot(full=True)['entries'][0]['body_base64'], '/w==')

    def test_limits_and_inflight_clear(self):
        debug = HealthDebug()
        self.addCleanup(lambda: debug.control('stop'))
        debug.control('start'); session = debug.begin()
        debug.max_bytes = 3
        entry = debug.start_request(session)
        debug.capture(session, entry, b'1234')
        self.assertEqual(debug.snapshot()['omitted'], 1)
        self.assertFalse(debug.snapshot()['complete'])
        debug.control('clear')
        debug.capture(session, entry, b'12')
        debug.record(session, 200, {}, entry=entry)
        self.assertEqual(debug.snapshot(full=True)['entries'], [])
        session = debug.begin()
        debug.max_entries = 1
        entry = debug.start_request(session)
        debug._expire(session)
        debug.capture(session, entry, b'123')
        debug.record(session, 400, {}, entry=entry)
        self.assertEqual(debug.snapshot(full=True)['entries'][0]['body'], '123')
        debug.control('start'); session = debug.begin()
        debug.start_request(session); debug.start_request(session)
        self.assertEqual(debug.snapshot()['omitted'], 1)
