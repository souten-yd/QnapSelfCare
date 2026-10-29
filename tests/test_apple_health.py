import copy
import json
from pathlib import Path
import tempfile
import unittest

import apple_health as ah
from storage import Store


def sample(metric='heart_rate', value=72, unit='bpm'):
    return dict(metric=metric, value=value, unit=unit, source='HUAWEIヘルスケア：グローバル',
                start_at='2026-10-26T07:00:00+09:00', end_at='2026-10-26T07:01:00+09:00')


class AppleHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.user = self.store.save_user({'name': 'Test'})['id']

    def send(self, samples, user=None):
        return ah.ingest(self.store, dict(schema=1, user_id=user or self.user, samples=samples))

    def test_six_metrics_units_durability_and_no_legacy_changes(self):
        self.store.add_records([dict(user_id=self.user, kind='body_composition',
            measured_at=sample()['start_at'], values={'weight': 80})])
        before = self.store.records()
        rows = [sample(), sample('steps', 10, 'count'), sample('walking_running_distance', 1, 'km'),
                sample('active_energy', 4.184, 'kJ'), sample('oxygen_saturation', .98, 'fraction'),
                sample('sleep', 'asleep_unspecified', None)]
        self.assertEqual(self.send(rows)['inserted'], 6)
        self.assertEqual(self.send(rows)['duplicates'], 6)
        canonical = copy.deepcopy(rows)
        canonical[2].update(value=1000, unit='m'); canonical[3].update(value=1, unit='kcal')
        canonical[4].update(value=98, unit='%')
        self.assertEqual(self.send(canonical)['duplicates'], 6)
        reopened = Store(self.tmp.name)
        self.assertEqual(ah.history(reopened, self.user)['total'], 6)
        self.assertEqual(before, self.store.records())

    def test_atomic_rejection_excluded_metrics_and_bad_values(self):
        for bad in [sample('weight', 80, 'kg'), sample('bmi', 23, 'count'),
                    sample('body_fat', 20, '%'), sample(value=float('nan')),
                    sample(value=True), sample('steps', 1.5, 'count'),
                    sample('oxygen_saturation', 101, '%'), sample('sleep', 'unknown', None),
                    dict(sample(), start_at='2026-10-26T07:00:00'),
                    dict(sample(), end_at='2026-10-25T07:00:00Z')]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.send([sample(), bad])
            self.assertEqual(ah.history(self.store, self.user)['total'], 0)
        with self.assertRaises(ValueError): self.send([sample()] * 50001)
        with self.assertRaises(ValueError): self.send([sample()], 'missing')

    def test_timezone_precision_source_and_user_identity(self):
        row = sample()
        self.send([row])
        utc = dict(row, start_at='2026-10-25T22:00:00Z', end_at='2026-10-25T22:01:00Z')
        self.assertEqual(self.send([utc])['duplicates'], 1)
        self.assertEqual(self.send([dict(row, start_at='2026-10-26T07:00:00.001+09:00')])['inserted'], 1)
        self.assertEqual(self.send([dict(row, source='iPhone')])['inserted'], 1)
        other = self.store.save_user({'name': 'Other'})['id']
        self.assertEqual(self.send([row], other)['inserted'], 1)
        self.assertEqual(ah.history(self.store, self.user, limit=1, offset=1)['total'], 3)

    def test_id_conflict_rolls_back_whole_batch_empty_does_not_delete(self):
        row = dict(sample(), source_id='original-1')
        self.send([row])
        with self.assertRaises(ValueError):
            self.send([sample('steps', 20, 'count'), dict(row, value=75)])
        self.assertEqual(self.send([]), {'inserted': 0, 'duplicates': 0, 'received': 0})
        self.assertEqual(ah.history(self.store, self.user)['total'], 1)

    def test_sleep_overlap_is_retained_and_interval_query(self):
        row = dict(sample('sleep', 'in_bed', None), start_at='2026-10-25T23:00:00+09:00', end_at='2026-10-26T07:00:00+09:00')
        self.send([row, dict(row, value='core')])
        self.assertEqual(ah.history(self.store, self.user, since='2026-10-26T00:00:00+09:00', until='2026-10-26T01:00:00+09:00')['total'], 2)

    def test_json_backup_restore_and_invalid_restore_rollback(self):
        self.send([sample()]); backup = self.store.backup()
        self.assertEqual(backup['schema'], 2)
        with tempfile.TemporaryDirectory() as d:
            truncated = copy.deepcopy(backup); truncated.pop('apple_health_samples')
            with self.assertRaises(ValueError): Store(d).restore(truncated)
        with tempfile.TemporaryDirectory() as d:
            restored = Store(d)
            invalid = copy.deepcopy(backup)
            invalid['apple_health_samples'][0]['sample']['metric'] = 'weight'
            with self.assertRaises(ValueError): restored.restore(invalid)
            self.assertEqual(restored.users(), [])
            restored.restore(backup)
            self.assertEqual(ah.history(restored, self.user), ah.history(self.store, self.user))
        with tempfile.TemporaryDirectory() as d:
            old = copy.deepcopy(backup); old['schema'] = 1; old.pop('apple_health_samples')
            restored = Store(d); restored.restore(old)
            self.assertEqual(ah.history(restored, self.user)['total'], 0)

    def test_migration_existing_database_and_guard(self):
        with self.store.connect() as db: db.execute('DROP TABLE apple_health_samples')
        reopened = Store(self.tmp.name)
        self.assertFalse(reopened.error)
        self.assertEqual(ah.history(reopened, self.user)['total'], 0)
        reopened.error = 'guard'
        from durability import DataUnavailable
        with self.assertRaises(DataUnavailable):
            ah.ingest(reopened, dict(schema=1, user_id=self.user, samples=[sample()]))

    def test_sqlite_snapshot_keeps_samples_and_user_cannot_be_deleted(self):
        from durability import snapshot
        import sqlite3
        self.send([sample()])
        with self.assertRaises(ValueError): self.store.delete_user(self.user)
        target = Path(self.tmp.name) / 'snapshot.sqlite3'
        snapshot(self.store.path, target)
        with sqlite3.connect(target) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM apple_health_samples').fetchone()[0], 1)





class ShortcutsGroupsTests(unittest.TestCase):
    setUp = AppleHealthTests.setUp
    send = AppleHealthTests.send
    
    def grouped(self, rows, kind='心拍数', unit='拍/分'):
        return dict(schema=2, user_id=self.user, groups=[
            dict(type=kind, source=sample()['source'], unit=unit, samples=rows)])

    def row(self, **extra):
        return dict(start=sample()['start_at'], end=sample()['end_at'], value=72, **extra)

    def test_groups_normalize_and_cross_schema_deduplicate(self):
        body = self.grouped([self.row()])
        result = ah.ingest(self.store, body)
        self.assertEqual(result['counts']['heart_rate'], 1)
        self.assertGreaterEqual(result['processing_ms'], 0)
        self.assertEqual(self.send([sample()])['duplicates'], 1)
        cases = [('歩数', '歩', 0, 'count', 0), ('ウォーキング＋ランニングの距離', 'km', .02, 'm', 20),
                 ('アクティブエネルギー', 'キロカロリー', .01, 'kcal', .01),
                 ('取り込まれた酸素のレベル', 'fraction', .96, '%', 96),
                 ('睡眠', '', 'レム', None, 'rem')]
        for kind, unit, value, normalized_unit, normalized_value in cases:
            row = self.row(); row['value'] = value
            ah.ingest(self.store, self.grouped([row], kind, unit))
        values = ah.history(self.store, self.user)['samples']
        self.assertIn(('sleep', 'rem', None), [(r['metric'], r['value'], r['unit']) for r in values])
        self.assertIn(('walking_running_distance', 20, 'm'), [(r['metric'], r['value'], r['unit']) for r in values])
        row = self.row(unit='m'); row['value'] = 20
        self.assertEqual(ah.ingest(self.store, self.grouped([row], 'walking_running_distance', 'km'))['duplicates'], 1)

    def test_groups_validation_and_conflict_are_atomic(self):
        for change in [dict(value='72 bpm'), dict(unit='unknown'),
                       dict(value=True), dict(end='bad')]:
            bad = self.row(); bad.update(change)
            with self.assertRaisesRegex(ValueError, r'groups\[1\] samples\[2\]'):
                ah.ingest(self.store, self.grouped([self.row(), bad]))
            self.assertEqual(ah.history(self.store, self.user)['total'], 0)
        with self.assertRaises(ValueError):
            ah.ingest(self.store, self.grouped([], '体重', 'kg'))
        with self.assertRaises(ValueError):
            ah.ingest(self.store, self.grouped([], 'sleep', 'minutes'))
        row = self.row(source_id='stable')
        ah.ingest(self.store, self.grouped([row]))
        bad = dict(row, value=73)
        with self.assertRaises(ValueError):
            ah.ingest(self.store, self.grouped([dict(row, source_id='new'), bad]))
        self.assertEqual(ah.history(self.store, self.user)['total'], 1)

    def test_50000_unique_samples_replay_and_limits(self):
        from datetime import datetime, timedelta, timezone
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        rows = [dict(start=(start + timedelta(minutes=i)).isoformat(),
                     end=(start + timedelta(minutes=i+1)).isoformat(), value=72) for i in range(50000)]
        body = self.grouped(rows)
        self.assertLess(len(json.dumps(body).encode()), 32 * 1024 * 1024)
        first = ah.ingest(self.store, body)
        self.assertEqual(first['inserted'], 50000)
        self.assertEqual(ah.ingest(self.store, body)['duplicates'], 50000)
        self.assertEqual(ah.history(Store(self.tmp.name), self.user)['total'], 50000)
        rows.append(self.row())
        with self.assertRaises(ValueError): ah.ingest(self.store, body)
        rows.pop()
        rows[-1]['value'] = 'invalid'
        with self.assertRaises(ValueError): ah.ingest(self.store, body)
        self.assertEqual(ah.history(self.store, self.user)['total'], 50000)

    def test_shortcuts_text_containers_and_numeric_text(self):
        rows = [dict(self.row(), value='72'), dict(self.row(), value='73')]
        for separator in ('\n', r'\n'):
            group = self.grouped([])['groups'][0]
            group['samples'] = separator.join(json.dumps(r) for r in rows)
            body = dict(schema=2, user_id=self.user, groups=json.dumps(group))
            result = ah.ingest(self.store, body)
            self.assertEqual(result['received'], 2)
        body = self.grouped([])
        body['groups'][0]['samples'] = [json.dumps(rows[0])]
        body['groups'] = [json.dumps(body['groups'][0])]
        self.assertEqual(ah.ingest(self.store, body)['duplicates'], 1)
        self.assertEqual(self.send([sample()])['duplicates'], 1)

    def test_extra_sample_braces_preserve_values_and_replay(self):
        row = dict(self.row(), value='72', device='literal {{braces}}')
        wrapped = '{' + json.dumps(row) + '}'
        group = self.grouped([])['groups'][0]
        for payload in (wrapped, wrapped + '\n' + wrapped,
                        wrapped + r'\n' + wrapped, [wrapped]):
            group['samples'] = payload
            result = ah.ingest(self.store, dict(schema=2, user_id=self.user, groups=group))
            self.assertEqual(result['received'], 2 if isinstance(payload, str) and payload.count('start') == 2 else 1)
        saved = ah.history(self.store, self.user)['samples']
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['device'], row['device'])
        self.assertEqual(saved[0]['value'], 72)
        self.assertEqual(saved[0]['start_at'], ah.instant(row['start']))
        self.assertEqual(saved[0]['end_at'], ah.instant(row['end']))

    def test_malformed_extra_braces_are_atomic(self):
        valid = '{' + json.dumps(self.row()) + '}'
        for bad in (valid[:-1], valid + '}', '{' + valid + '}',
                    valid + 'garbage', '{{"value":"72"}}'):
            group = self.grouped([])['groups'][0]
            group['samples'] = valid + '\n' + bad
            with self.assertRaises(ValueError):
                ah.ingest(self.store, dict(schema=2, user_id=self.user, groups=group))
            self.assertEqual(ah.history(self.store, self.user)['total'], 0)

    def test_shortcuts_missing_time_and_ambiguous_text_rejected(self):
        for value in ('70\n72\n69', '', '[70,72]', '"70"', 'null'):
            with self.assertRaises(ValueError):
                ah.ingest(self.store, dict(schema=2, user_id=self.user, groups=value))
        for value in ('NaN', 'Infinity', '72 bpm', '1,200', '1e999'):
            with self.assertRaises(ValueError):
                ah.ingest(self.store, self.grouped([dict(self.row(), value=value)]))
        with self.assertRaises(ValueError):
            ah.ingest(self.store, self.grouped([{'value': '72'}]))
        group = self.grouped([])['groups'][0]
        group['samples'] = json.dumps(self.row()) + '\nBROKEN'
        with self.assertRaises(ValueError):
            ah.ingest(self.store, dict(schema=2, user_id=self.user, groups=json.dumps(group)))
        self.assertEqual(ah.history(self.store, self.user)['total'], 0)

    def test_empty_groups_are_not_completion_proof(self):
        result = ah.ingest(self.store, self.grouped([]))
        self.assertEqual(result['received'], 0)
        self.assertIsNone(result['start_at'])

if __name__ == '__main__': unittest.main()


class ReceiveStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.user = self.store.save_user({'name': 'Test'})['id']

    def test_counts_recent_items_and_reports_missing_ones(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        def at(hours):
            t = (now - timedelta(hours=hours)).replace(microsecond=0)
            return t.isoformat().replace('+00:00', 'Z')
        samples = [dict(metric='steps', start_at=at(2), end_at=at(1), value=500, unit='count', source='iPhone'),
                   dict(metric='steps', start_at=at(3), end_at=at(2), value=300, unit='count', source='Watch'),
                   dict(metric='heart_rate', start_at=at(24 * 10), end_at=at(24 * 10), value=70, unit='bpm', source='Watch')]
        ah.ingest(self.store, dict(schema=1, user_id=self.user, samples=samples))
        status = {i['metric']: i for i in ah.receive_status(self.store, self.user)['items']}
        self.assertEqual(set(status), set(ah.METRICS))
        self.assertEqual(status['steps']['count'], 2)
        self.assertEqual(status['steps']['sources'], ['Watch', 'iPhone'])
        self.assertEqual(status['heart_rate']['count'], 0)                 # only older than 7 days
        self.assertIsNotNone(status['heart_rate']['last_sample_at'])       # but we still say when it last came
        self.assertEqual((status['sleep']['count'], status['sleep']['last_sample_at']), (0, None))
        with self.assertRaises(ValueError):
            ah.receive_status(self.store, 'missing')
