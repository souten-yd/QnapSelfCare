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
        with self.assertRaises(ValueError): self.send([sample()] * 1001)
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


if __name__ == '__main__': unittest.main()
