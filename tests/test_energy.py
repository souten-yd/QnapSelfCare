import tempfile
import unittest
from datetime import date
from unittest.mock import patch
from storage import Store
import energy


class EnergyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.uid = self.store.save_user({'name': 'A'})['id']
        self.other = self.store.save_user({'name': 'B'})['id']
        self.profile = self.store.save_wellness_profile(self.uid, {'height_cm': 180, 'age': 40, 'sex': 'male'})
        self.plan = dict(start_date='2026-08-01', start_weight=100, goal_date='2027-03-01', goal_weight=85,
                         pal=1.5, deficit_kcal=300, target_kcal=None, adaptation_pct=0, reason='初回')

    def record(self, when, weight):
        self.store.add_records([{'user_id': self.uid, 'kind': 'body_composition', 'measured_at': when,
                                 'values': {'weight': weight}}])

    def test_day_upsert_isolation_and_override_not_double_counted(self):
        self.record('2026-08-01T23:30:00+09:00', 100)
        self.record('2026-08-01T08:00:00+09:00', 102)
        self.store.save_activity(self.uid, dict(date='2026-08-01', pal=1.5, steps=10000, minutes=60,
                                               total_kcal=2400, intake_kcal=2100))
        h = self.store.energy_report(self.uid)['history'][0]
        self.assertEqual(h['weight'], 101)
        self.assertEqual(h['bmr'], 1940)
        self.assertEqual(h['total_kcal'], 2400)
        self.assertEqual(h['balance_kcal'], -300)
        self.assertEqual(self.store.energy_report(self.other)['history'], [])
        self.store.save_activity(self.uid, dict(date='2026-08-01', pal=1.2, steps=500))
        r = self.store.energy_report(self.uid)
        self.assertEqual(len(r['diaries']), 1)
        self.assertEqual(r['history'][0]['total_kcal'], 2328)
        self.assertIsNone(r['history'][0]['intake_kcal'])
        with self.assertRaises(ValueError):
            self.store.delete_activity(self.other, '2026-08-01')
        self.store.delete_activity(self.uid, '2026-08-01')
        self.assertEqual(self.store.energy_report(self.uid)['diaries'], [])

    def test_no_future_weight_leak_or_missing_as_zero(self):
        self.store.save_activity(self.uid, dict(date='2026-08-01', pal=1.5))
        self.record('2026-08-02T01:00:00+09:00', 90)
        r = self.store.energy_report(self.uid)
        self.assertIsNone(r['history'][0]['bmr'])
        self.assertIsNone(r['history'][0]['balance_kcal'])
        self.assertEqual(r['history'][1]['date'], '2026-08-02')

    def test_weekly_means_deviation_and_sparse_weeks(self):
        self.record('2026-08-01T01:00:00+09:00', 100)
        self.record('2026-08-01T23:00:00+09:00', 102)
        self.record('2026-08-02T01:00:00+09:00', 99)
        self.store.save_weight_plan(self.uid, self.plan)
        r = self.store.energy_report(self.uid)
        first, missing = r['weeks'][:2]
        self.assertEqual(first['actual'], 100)
        self.assertEqual(first['days'], 2)
        self.assertEqual(first['remaining'], 15)
        self.assertAlmostEqual(first['deviation'], 100-first['planned'], places=2)
        self.assertIsNone(missing['actual'])
        self.assertFalse(any('横ばい' in n for n in r['notices']))

    def test_dynamic_metabolism_and_scenario_slowdown(self):
        original = energy.report(self.profile, [], [], [self.plan], date(2026, 9, 25))['projection']
        fixed = dict(self.plan, target_kcal=2400)
        normal = energy.report(self.profile, [], [], [fixed], date(2026, 9, 25))['projection']
        adapted = energy.report(self.profile, [], [], [dict(fixed, adaptation_pct=10)], date(2026, 9, 25))['projection']
        self.assertLess(original[-1]['bmr'], original[0]['bmr'])
        self.assertLess(original[-1]['total_kcal'], original[0]['total_kcal'])
        self.assertGreater(adapted[-1]['weight'], normal[-1]['weight'])
        self.assertLess(normal[-2]['weight']-normal[-1]['weight'], normal[0]['weight']-normal[1]['weight'])

    def test_revision_retained_and_restore_transactional(self):
        self.store.save_activity(self.uid, dict(date='2026-08-01', pal=1.5, intake_kcal=2000))
        self.store.save_weight_plan(self.uid, self.plan)
        revised = dict(self.plan, start_date='2026-09-01', start_weight=97, goal_date='2027-05-01', reason='期限を延長')
        self.store.save_weight_plan(self.uid, revised)
        self.assertEqual(self.store.wellness_profile(self.uid)['goal_date'], '2027-05-01')
        with self.assertRaises(ValueError):
            self.store.save_wellness_profile(self.uid, {'goal_date': '2027-06-01'})
        with self.assertRaises(ValueError):
            self.store.save_weight_plan(self.uid, dict(revised, reason=''))
        with self.assertRaises(ValueError):
            self.store.save_weight_plan(self.uid, self.plan)
        backup = self.store.backup()
        with tempfile.TemporaryDirectory() as dest:
            restored = Store(dest)
            restored.restore(backup)
            r = restored.energy_report(self.uid)
            self.assertEqual(len(r['plans']), 2)
            self.assertEqual(r['plans'][0]['goal_date'], '2027-03-01')
            self.assertEqual(r['active_plan']['reason'], '期限を延長')
            self.assertEqual(r['diaries'][0]['intake_kcal'], 2000)
        backup['activity_days'][0]['pal'] = float('nan')
        with tempfile.TemporaryDirectory() as dest:
            restored = Store(dest)
            with self.assertRaises(ValueError):
                restored.restore(backup)
            self.assertEqual(restored.users(), [])

    def test_plateau_requires_three_consecutive_supported_weeks(self):
        records = []
        for week in range(3):
            for n in range(3):
                records.append({'kind': 'body_composition', 'measured_at': f'2026-08-{1+week*7+n:02d}T01:00:00+09:00', 'values': {'weight': 100}})
        report = energy.report(self.profile, records, [], [self.plan], date(2026, 8, 22))
        self.assertTrue(any('横ばい' in n for n in report['notices']))
        report = energy.report(self.profile, records[:6], [], [self.plan], date(2026, 8, 22))
        self.assertFalse(any('横ばい' in n for n in report['notices']))

    def test_invalid_inputs_and_low_targets(self):
        for entry in [dict(date='bad'), dict(date='2026-08-01', pal=True), dict(date='2099-01-01'),
                      dict(date='2026-08-01', minutes=1441), dict(date='2026-08-01', target_kcal=500)]:
            with self.assertRaises(ValueError):
                self.store.save_activity(self.uid, entry)
        self.record('2026-08-01T01:00:00+09:00', 100)
        self.store.save_weight_plan(self.uid, dict(self.plan, pal=1.2, deficit_kcal=750))
        r = self.store.energy_report(self.uid)
        self.assertIsNone(r['current']['target_kcal'])
        self.assertTrue(any('低すぎる' in x for x in r['notices']))


if __name__ == '__main__':
    unittest.main()
