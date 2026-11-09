"""Smaller Home payloads: daily watch aggregates, compact chart rows, latest records, gzip."""
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from urllib.request import Request, urlopen

import apple_health as ah
from storage import Store
import test_webapp


class DailyAggregateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.user = self.store.save_user({'name': 'T'})['id']

    def ingest(self, typ, unit, source, rows):
        ah.ingest(self.store, {'schema': 2, 'user_id': self.user, 'groups': [
            {'type': typ, 'source': source, 'unit': unit,
             'samples': [{'start': s, 'end': e, 'value': v} for s, e, v in rows]}]})

    def test_daily_groups_by_local_day_metric_and_source(self):
        # 23:30 JST on 11/7 is 14:30 UTC, 00:30 JST on 11/8 is 15:30 UTC on 11/7.
        self.ingest('heart_rate', 'bpm', 'Watch', [
            ('2026-11-07T23:30:00+09:00', '2026-11-07T23:31:00+09:00', '60'),
            ('2026-11-07T23:40:00+09:00', '2026-11-07T23:41:00+09:00', '80'),
            ('2026-11-08T00:30:00+09:00', '2026-11-08T00:31:00+09:00', '70')])
        self.ingest('steps', 'count', 'iPhone', [('2026-11-08T08:00:00+09:00', '2026-11-08T08:10:00+09:00', '500')])
        self.ingest('sleep', '', 'Watch', [('2026-11-08T01:00:00+09:00', '2026-11-08T02:00:00+09:00', 'Core')])
        result = ah.daily(self.store, self.user, '2026-11-06T15:00:00Z', '2026-11-08T15:00:00Z', 540)
        self.assertEqual(result['rows'], [
            ['2026-11-07', 'heart_rate', 'Watch', 2, 140.0, 60.0, 80.0],
            ['2026-11-08', 'heart_rate', 'Watch', 1, 70.0, 70.0, 70.0],
            ['2026-11-08', 'steps', 'iPhone', 1, 500.0, 500.0, 500.0]])   # sleep is never aggregated
        utc = ah.daily(self.store, self.user, '2026-11-06T15:00:00Z', '2026-11-08T15:00:00Z', 0)['rows']
        self.assertEqual([r[:4] for r in utc if r[1] == 'heart_rate'], [['2026-11-07', 'heart_rate', 'Watch', 3]])

    def test_daily_validates_input(self):
        for args in [('', '2026-11-01T00:00:00Z', '2026-11-02T00:00:00Z'),
                     (self.user, None, '2026-11-02T00:00:00Z'),
                     (self.user, '2026-11-02T00:00:00Z', '2026-11-01T00:00:00Z'),
                     (self.user, '2025-01-01T00:00:00Z', '2026-11-02T00:00:00Z')]:
            with self.assertRaises(ValueError):
                ah.daily(self.store, *args)
        with self.assertRaises(ValueError):
            ah.daily(self.store, self.user, '2026-11-01T00:00:00Z', '2026-11-02T00:00:00Z', metrics='sleep')

    def test_chart_rows_and_latest_records(self):
        other = self.store.save_user({'name': 'O'})['id']
        rec = lambda u, kind, at, values: {'user_id': u, 'kind': kind, 'measured_at': at, 'values': values, 'note': 'n'}
        self.store.add_records([
            rec(self.user, 'blood_pressure', '2026-01-01T07:00:00+09:00', {'systolic': 120, 'diastolic': 80, 'pulse': 60}),
            rec(self.user, 'blood_pressure', '2026-11-01T07:00:00+09:00', {'systolic': 125, 'diastolic': 82, 'pulse': 61}),
            rec(self.user, 'body_composition', '2026-10-01T07:00:00+09:00', {'weight': 70.0}),
            rec(other, 'body_composition', '2026-11-05T07:00:00+09:00', {'weight': 60.0})])
        chart = self.store.chart_records(user_id=self.user, since='2026-06-01T00:00:00+09:00')
        self.assertEqual(chart['total'], 2)
        self.assertEqual(chart['rows'][0][1:], [self.user, 'blood_pressure', {'systolic': 125, 'diastolic': 82, 'pulse': 61}])
        self.assertEqual(len(chart['rows'][0]), 4)   # no raw, note, device or ids
        latest = self.store.latest_records(self.user)
        self.assertEqual(latest['blood_pressure']['values']['systolic'], 125)   # outside the chart period still found
        self.assertEqual(latest['body_composition']['values']['weight'], 70.0)
        self.assertEqual(self.store.latest_records()['body_composition']['user_id'], other)


class GzipTests(unittest.TestCase):
    _base = test_webapp.WebAppTests
    setUp, tearDown, request = _base.setUp, _base.tearDown, _base.request

    def get(self, path, encoding):
        return urlopen(Request(self.base + path, headers={'Accept-Encoding': encoding} if encoding else {}), timeout=3)

    def test_json_and_assets_are_gzipped_only_when_accepted(self):
        with self.get('/app.js', 'gzip, deflate') as response:
            self.assertEqual(response.headers['Content-Encoding'], 'gzip')
            self.assertEqual(response.headers['Vary'], 'Accept-Encoding')
            body = gzip.decompress(response.read())
        self.assertEqual(body, Path('web/app.js').read_bytes())
        with self.get('/app.js', None) as response:
            self.assertIsNone(response.headers['Content-Encoding'])
            self.assertEqual(response.read(), body)
        user = json.load(self.request('/api/users', {'name': 'G'}))['id']
        self.request('/api/records', {'user_id': user, 'kind': 'blood_pressure', 'measured_at': '2026-11-01T07:00:00+09:00',
                                      'values': {'systolic': 120, 'diastolic': 80, 'pulse': 60}, 'note': 'x' * 900})
        with self.get('/api/records?user_id=' + user, 'gzip') as response:
            self.assertEqual(response.headers['Content-Encoding'], 'gzip')
            self.assertEqual(json.loads(gzip.decompress(response.read()))['total'], 1)
        with self.get('/api/status', 'gzip') as response:   # small bodies stay plain
            self.assertIsNone(response.headers['Content-Encoding'])
        with self.get('/api/apple-health/daily?user_id=%s&since=2026-11-01T00:00:00Z&until=2026-11-08T00:00:00Z&tz_offset=540' % user, None) as response:
            self.assertEqual(json.load(response)['rows'], [])


if __name__ == '__main__':
    unittest.main()
