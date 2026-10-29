import unittest

import prompt_text


def context(**extra):
    ctx = {'measurements': {'latest_weight': 74.4, 'bmi': 25.1, 'estimated_bmr_kcal': 1614, 'weight_change_7d_kg': -0.5,
                            'latest_pressure': {'systolic': 124.0, 'diastolic': 79.0}, 'notices': []},
           'goal': {'weight_kg': 71.0, 'date': '2026-12-27'},
           'evaluations': [{'label': '減量ペース', 'value': '-0.42 kg/週', 'status': 'good', 'basis': '長い説明' * 50},
                           {'label': 'BMI', 'value': '25.2', 'status': 'ok', 'basis': '長い説明' * 50},
                           {'label': '体脂肪', 'value': '—', 'status': 'unknown', 'basis': ''}],
           'recent_activity': [{'date': f'2026-10-{d:02d}', 'steps': 6000.0, 'minutes': 20.0, 'source': 'entered'} for d in range(1, 29)]}
    ctx.update(extra)
    return ctx


class PromptTextTests(unittest.TestCase):
    def test_daily_review_is_short_and_keeps_the_key_numbers(self):
        review = {'date': '2026-10-27', 'items': [
            {'id': 'steps', 'label': '歩数', 'unit': '歩', 'value': 20721.0, 'avg7': 5986.0, 'trend': 'up'},
            {'id': 'weight', 'label': '体重', 'unit': 'kg', 'value': 74.3, 'avg7': 74.8, 'trend': 'down'},
            {'id': 'sleep', 'label': '睡眠時間', 'unit': '時間', 'value': None, 'avg7': 7.5, 'trend': 'none'}]}
        text = prompt_text.render(context(day_review=review), 'daily')
        self.assertTrue(text.startswith('10/27の記録'))
        self.assertIn('- 歩数: 20721歩（7日平均5986歩・多め）', text)
        self.assertIn('- 体重: 74.3kg（7日平均74.8kg・低め）', text)
        self.assertIn('記録なし: 睡眠時間', text)
        self.assertIn('体重74.4kg', text)
        self.assertNotIn('長い説明', text)          # reference texts are not sent
        self.assertNotIn('BMI: 25.2', text)        # nightly: only flagged indicators
        self.assertNotIn('最近の活動', text)        # covered by the day review
        self.assertNotIn('体脂肪', text)            # no data, no line

    def test_budget_drops_low_priority_sections(self):
        text = prompt_text.render(context(), 'health', budget=200)
        self.assertIn('現在:', text)
        self.assertNotIn('最近の活動', text)
        self.assertLess(len(prompt_text.render(context(), 'health')), prompt_text.BUDGET_CHARS + 200)
        self.assertIn('記録: まだほとんどありません', prompt_text.render({}, 'health'))

    def test_plan_review_options(self):
        review = {'reasons': ['計画線より0.6kg重い状態です。'],
                  'options': [{'id': 'A', 'title': '目標日を見直す', 'changes': ['目標日: 2026-12-27 → 2027-01-20'],
                               'warnings': [], 'weekly_loss_kg': 0.4, 'planned_intake_kcal': None, 'projected_goal_weight': 71.0}]}
        text = prompt_text.render(context(plan_review=review), 'review')
        self.assertTrue(text.startswith('計画を見直す理由: 計画線より0.6kg重い状態です。'))
        self.assertIn('案A 目標日を見直す: 目標日: 2026-12-27 → 2027-01-20、週0.4kg減、目標日の試算71kg', text)


if __name__ == '__main__':
    unittest.main()
