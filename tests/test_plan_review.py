import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import energy
import plan_review
from plan_review import PlanReviewer
from storage import Store

JST = energy.JST


class FakeCoach:
    def __init__(self, answer='推奨: B\n期日を守るなら摂取量の調整が現実的です。', base_url='http://localhost/v1'):
        self.answer, self.base_url, self.asked = answer, base_url, []

    def settings(self):
        return {'provider': 'local', 'base_url': self.base_url, 'model': 'auto'}

    def ask(self, user_id, mode, question, extra=None, cancelled=None, poll=3, **kwargs):
        self.asked.append((mode, extra))
        if isinstance(self.answer, Exception):
            raise self.answer
        return {'answer': self.answer}


class PlanReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.user = self.store.save_user({'name': '利用者'})['id']
        self.store.save_wellness_profile(self.user, {'height_cm': 172, 'age': 42, 'sex': 'male'})
        self.today = energy.today()

    def plateau_behind_plan(self):
        """Plan: 80 → 72 kg over 120 days, started 42 days ago; the weight stalled at ~79.4 kg."""
        start = self.today - timedelta(days=42)
        rows = []
        for i in range(43):
            d = start + timedelta(days=i)
            w = 80 - min(i, 14) * 0.04 + (0.15 if i % 2 else -0.15)
            rows.append({'user_id': self.user, 'kind': 'body_composition',
                         'measured_at': datetime(d.year, d.month, d.day, 7, tzinfo=JST).isoformat(), 'values': {'weight': round(w, 2)}})
        self.store.add_records(rows)
        self.store.save_weight_plan(self.user, {'start_date': start.isoformat(), 'start_weight': 80, 'goal_weight': 72,
                                                'goal_date': (start + timedelta(days=120)).isoformat(), 'pal': 1.5,
                                                'deficit_kcal': 400, 'exercise_minutes': 0, 'exercise_met': 1})

    def test_on_track_or_no_plan_creates_nothing(self):
        reviewer = PlanReviewer(self.store, FakeCoach())
        self.assertIsNone(reviewer.review(self.user))
        self.assertEqual(reviewer.proposals(self.user)['pending'], None)

    def test_behind_plan_creates_safe_options_and_ai_recommendation(self):
        self.plateau_behind_plan()
        coach = FakeCoach()
        proposal = PlanReviewer(self.store, coach).review(self.user, 'schedule')
        self.assertEqual(proposal['status'], 'pending')
        self.assertTrue(any('計画線より' in r for r in proposal['reasons']))
        kinds = [o['kind'] for o in proposal['options']]
        self.assertIn('extend', kinds)
        for option in proposal['options']:
            self.assertLessEqual(option['weekly_pct'], plan_review.MAX_WEEKLY_PCT)
            energy.plan(option['entry'])   # every option is a valid plan
            if option['kind'] == 'intake':
                floor = self.store.energy_report(self.user, extra_plan=option['entry'])['plan_analysis']['safety_floor_kcal']
                self.assertGreaterEqual(option['entry']['target_kcal'], floor)
        for option in proposal['options']:
            if (option['projected_gap_kg'] or 0) > 1.0:
                self.assertTrue(any('届かない見込み' in w for w in option['warnings']))
        extend = next(o for o in proposal['options'] if o['kind'] == 'extend')
        self.assertGreater(extend['entry']['goal_date'], self.store.energy_report(self.user)['active_plan']['goal_date'])
        self.assertEqual(coach.asked[0][0], 'review')
        self.assertEqual(len(coach.asked[0][1]['plan_review']['options']), len(proposal['options']))
        if any(o['id'] == 'B' for o in proposal['options']):
            self.assertEqual((proposal['recommended'], proposal['recommended_by']), ('B', 'ai'))
        # weekly limit: no second automatic proposal while one is pending
        self.assertIsNone(PlanReviewer(self.store, coach).review(self.user, 'schedule'))

    def test_new_plan_gets_a_grace_period_after_applying(self):
        self.plateau_behind_plan()
        reviewer = PlanReviewer(self.store, FakeCoach(base_url=''))
        proposal = reviewer.review(self.user, 'schedule')
        reviewer.apply(self.user, proposal['id'], proposal['options'][0]['id'])
        self.assertIsNone(reviewer.review(self.user, 'schedule'))   # the new plan starts today
        with patch.object(plan_review, 'NEW_PLAN_GRACE_DAYS', -1):
            self.assertIsNone(reviewer.review(self.user, 'schedule'))   # and the last decision was today

    def test_apply_needs_explicit_choice_and_keeps_history(self):
        self.plateau_behind_plan()
        reviewer = PlanReviewer(self.store, FakeCoach(base_url=''))   # rule-based recommendation, no AI
        proposal = reviewer.review(self.user)
        self.assertEqual(proposal['recommended_by'], 'rule')
        before = self.store.energy_report(self.user)['plans']
        option = proposal['options'][0]
        reviewer.apply(self.user, proposal['id'], option['id'])
        after = self.store.energy_report(self.user)
        self.assertEqual(len(after['plans']), len(before) + 1)          # new revision, old plan kept
        self.assertEqual(after['active_plan']['goal_date'], option['entry']['goal_date'])
        self.assertIn('修正案' + option['id'], after['active_plan']['reason'])
        with self.assertRaisesRegex(ValueError, '処理済み'):
            reviewer.apply(self.user, proposal['id'], option['id'])

    def test_outdated_dismissed_and_ai_failure(self):
        self.plateau_behind_plan()
        reviewer = PlanReviewer(self.store, FakeCoach(answer=ValueError('AI HTTP 503')))
        proposal = reviewer.review(self.user)
        self.assertIn('503', proposal['error'])
        self.assertEqual(proposal['recommended_by'], 'rule')
        # The user edits the plan by hand: the proposal is outdated and cannot be applied.
        plan = self.store.energy_report(self.user)['active_plan']
        edited = {k: plan[k] for k in ('start_date', 'start_weight', 'goal_weight', 'pal', 'deficit_kcal', 'exercise_minutes', 'exercise_met')}
        edited.update({'goal_date': (self.today + timedelta(days=200)).isoformat(), 'reason': '手動で延長'})
        self.store.save_weight_plan(self.user, edited)
        self.assertEqual(reviewer.proposals(self.user)['history'][0]['status'], 'outdated')
        with self.assertRaisesRegex(ValueError, '計画が変更されました'):
            reviewer.apply(self.user, proposal['id'], proposal['options'][0]['id'])
        fresh = reviewer.review(self.user)
        self.assertEqual(reviewer.dismiss(self.user, fresh['id']), {'dismissed': fresh['id']})
        self.assertIsNone(reviewer.proposals(self.user)['pending'])


if __name__ == '__main__':
    unittest.main()
