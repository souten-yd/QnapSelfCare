import unittest
from datetime import date
from unittest.mock import patch

import energy


class WeightPlanModelTests(unittest.TestCase):
    def setUp(self):
        self.profile = {'height_cm': 180, 'age': 40, 'sex': 'male', 'bmi_low': 18.5}
        self.records = [{'kind':'body_composition','measured_at':'2026-09-26T07:00:00+09:00',
                         'values':{'weight':101.2}}]

    def test_net_exercise_energy_uses_met_above_rest(self):
        self.assertEqual(energy.exercise_kcal(100, 60, 4), 315)
        self.assertEqual(energy.exercise_kcal(100, 60, 1), 0)

    def test_plan_analysis_explains_when_safety_floor_prevents_required_intake(self):
        plan = energy.plan({'start_date':'2026-09-26','start_weight':101.2,'goal_date':'2027-03-31','goal_weight':80,
                            'pal':1.2,'deficit_kcal':750,'target_kcal':None,'adaptation_pct':0,
                            'exercise_minutes':0,'exercise_met':1,'reason':''})
        report = energy.report(self.profile, self.records, [], [plan], current=date(2026,9,26))
        analysis = report['plan_analysis']
        self.assertGreater(analysis['required_daily_deficit_kcal'], 800)
        self.assertTrue(analysis['floor_blocks_required_intake'])
        self.assertGreater(analysis['projected_goal_weight'], 80)
        self.assertGreater(analysis['projected_gap_kg'], 0)

    def test_standard_exercise_changes_projection_and_old_plan_defaults_work(self):
        base = {'start_date':'2026-09-26','start_weight':101.2,'goal_date':'2027-03-31','goal_weight':90,
                'pal':1.2,'deficit_kcal':400,'target_kcal':1900,'adaptation_pct':0,'reason':''}
        with patch('energy.today', return_value=date(2026,9,26)):
            old = energy.plan(base)
            active = energy.plan(dict(base, exercise_minutes=60, exercise_met=4))
        self.assertEqual(old['exercise_minutes'], 0)
        plain = energy.report(self.profile, self.records, [], [old], current=date(2026,9,26))
        moved = energy.report(self.profile, self.records, [], [active], current=date(2026,9,26))
        self.assertGreater(moved['plan_analysis']['start_total_kcal'], plain['plan_analysis']['start_total_kcal'])
        self.assertLess(moved['plan_analysis']['projected_goal_weight'], plain['plan_analysis']['projected_goal_weight'])


if __name__ == '__main__':
    unittest.main()
