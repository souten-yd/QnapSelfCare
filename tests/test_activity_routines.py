import tempfile
import unittest
from datetime import date
from unittest.mock import patch
from storage import Store


class ActivityRoutineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.uid = self.store.save_user({'name': 'A'})['id']
        self.other = self.store.save_user({'name': 'B'})['id']
        self.clock = patch('energy.today', return_value=date(2026, 9, 1)).start()
        self.addCleanup(patch.stopall)
        self.store.save_wellness_profile(self.uid, {'height_cm': 180, 'age': 40, 'sex': 'male'})
        self.store.add_records([{'user_id':self.uid, 'kind':'body_composition', 'measured_at':'2026-09-01T01:00:00+09:00', 'values':{'weight':100}}])

    def report(self):
        return self.store.energy_report(self.uid)

    def test_next_day_reuse_is_assumption_not_inserted_as_daily_actual(self):
        self.store.save_activity(self.uid, {'date':'2026-09-01', 'pal':1.5, 'steps':8000, 'minutes':40, 'intake_kcal':2100}, reuse=True)
        self.clock.return_value = date(2026, 9, 2)
        r = self.report()
        self.assertEqual(len(r['diaries']), 1)
        self.assertEqual(r['current']['intake_kcal'], 2100)
        self.assertEqual(r['current']['source'], 'routine')
        self.assertEqual(r['entries'][-1]['steps'], 8000)
        self.assertEqual(r['entries'][-1]['minutes'], 40)
        self.assertEqual(r['history'][-1]['balance_kcal'], 2100-1930*1.5)
        self.assertEqual(r['history'][-1]['source'], 'routine')
        self.assertIsNone(self.store.energy_report(self.other)['routine'])
        # Reopening and body weight changes still use the routine's PAL, not a frozen BMR/TDEE.
        reopened = Store(self.tmp.name)
        reopened.add_records([{'user_id':self.uid, 'kind':'body_composition', 'measured_at':'2026-09-02T01:00:00+09:00', 'values':{'weight':90}}])
        r = reopened.energy_report(self.uid)
        self.assertEqual(r['current']['total_kcal'], 1830*1.5)
        self.assertEqual(r['current']['intake_kcal'], 2100)

    def test_exception_does_not_change_default_and_delete_reveals_default(self):
        self.store.save_activity(self.uid, {'date':'2026-09-01', 'pal':1.5, 'intake_kcal':2100}, reuse=True)
        self.clock.return_value = date(2026, 9, 2)
        self.store.save_activity(self.uid, {'date':'2026-09-02', 'pal':1.2, 'intake_kcal':2500}, reuse=False)
        self.assertEqual(self.report()['current']['source'], 'entered')
        self.assertEqual(self.report()['current']['intake_kcal'], 2500)
        self.store.delete_activity(self.uid, '2026-09-02')
        self.assertEqual(self.report()['current']['intake_kcal'], 2100)
        self.clock.return_value = date(2026, 9, 3)
        self.assertEqual(self.report()['current']['pal'], 1.5)

    def test_revision_applies_from_today_without_rewriting_past_and_can_stop(self):
        self.store.save_activity(self.uid, {'date':'2026-09-01', 'pal':1.5, 'intake_kcal':2100}, reuse=True)
        self.clock.return_value = date(2026, 9, 3)
        self.store.save_activity(self.uid, {'date':'2026-09-03', 'pal':1.75, 'intake_kcal':2300}, reuse=True)
        self.clock.return_value = date(2026, 9, 4)
        r = self.report()
        entries = {x['date']:x for x in r['entries']}
        self.assertEqual(entries['2026-09-02']['intake_kcal'], 2100)
        self.assertEqual(entries['2026-09-04']['intake_kcal'], 2300)
        self.store.save_activity_routine(self.uid, None)
        self.assertIsNone(self.report()['routine'])
        self.assertIsNone(self.report()['current']['intake_kcal'])
        self.assertEqual(len(self.report()['diaries']), 2)
        self.assertEqual(next(x for x in self.report()['entries'] if x['date']=='2026-09-02')['intake_kcal'], 2100)

    def test_backdated_save_does_not_backfill_and_backup_roundtrip(self):
        self.clock.return_value = date(2026, 9, 5)
        self.store.save_activity(self.uid, {'date':'2026-09-01', 'pal':1.5, 'intake_kcal':0}, reuse=True)
        r = self.report()
        self.assertEqual([x['date'] for x in r['entries']], ['2026-09-01','2026-09-05'])
        self.assertEqual(r['current']['intake_kcal'], 0)
        with tempfile.TemporaryDirectory() as dest:
            restored = Store(dest)
            restored.restore(self.store.backup())
            self.clock.return_value = date(2026, 9, 6)
            self.assertEqual(restored.energy_report(self.uid)['current']['intake_kcal'], 0)
            self.assertEqual(len(restored.energy_report(self.uid)['diaries']), 1)
        with self.assertRaises(ValueError):
            self.store.save_activity(self.uid, {'date':'2026-09-05'}, reuse='true')
        self.assertEqual(len(self.report()['diaries']), 1)


if __name__ == '__main__':
    unittest.main()
