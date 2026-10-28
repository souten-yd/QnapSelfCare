import tempfile
import unittest
from datetime import date, datetime, timedelta

import apple_health
import health_metrics as hm
from storage import Store

TODAY = date(2026, 10, 28)


def at(d, hour=7, minute=0):
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=hm.JST).isoformat()


def body(d, weight, fat=None):
    values = {'weight': weight}
    if fat is not None:
        values['body_fat'] = fat
    return {'measured_at': at(d), 'values': values}


def pressure(d, s, dia):
    return {'measured_at': at(d), 'values': {'systolic': s, 'diastolic': dia}}


class PaceTests(unittest.TestCase):
    def series(self, per_week, days=28, skip=()):
        return [body(TODAY - timedelta(days=i), 80 + per_week / 7 * (days - 1 - i) + (0.3 if i % 2 else -0.3))
                for i in range(days) if i not in skip]

    def test_needs_ten_days_over_three_weeks(self):
        m, pace = hm.weight_pace(self.series(-0.5, days=9), TODAY, 70)
        self.assertEqual(m['status'], 'unknown')
        self.assertIsNone(pace)

    def test_classifies_pace_as_percent_of_body_weight(self):
        cases = [(-0.5, 'good'), (-1.2, 'warn'), (-0.1, 'ok'), (0.5, 'warn')]
        for per_week, status in cases:
            m, pace = hm.weight_pace(self.series(per_week), TODAY, 70)
            self.assertEqual(m['status'], status, per_week)
            self.assertAlmostEqual(pace['slope'] * 7, per_week, places=1)
        # Without a lower goal a flat weight is good (stable), not a stalled diet.
        self.assertEqual(hm.weight_pace(self.series(0.0), TODAY, None)[0]['status'], 'good')

    def test_noise_without_trend_is_reported_as_unclear(self):
        noisy = [body(TODAY - timedelta(days=i), 75 + (1.5 if i % 3 == 0 else -0.8)) for i in range(0, 28, 2)]
        m, _ = hm.weight_pace(noisy, TODAY, 70)
        self.assertIn('はっきりしません', m['detail'])


class CompositionAndPlanTests(unittest.TestCase):
    def test_fat_share_needs_three_kg_and_reports_share(self):
        small = [body(TODAY - timedelta(days=i), 80 - 1 * (55 - i) / 55, 25) for i in range(56)]
        self.assertEqual(hm.fat_share(small, TODAY)['status'], 'unknown')
        rows = []
        for i in range(56):
            lost = 5 * (55 - i) / 55
            w = 80 - lost
            fat = 80 * 0.3 - 0.8 * lost
            rows.append(body(TODAY - timedelta(days=i), w, fat / w * 100))
        m = hm.fat_share(rows, TODAY)
        self.assertEqual(m['status'], 'good')
        self.assertTrue(m['value'].startswith('脂肪 80%'))

    def test_plan_gap_and_forecast(self):
        pace = {'slope': -0.5 / 7, 'se': 0.005, 'trend': 76.0, 'days': 20}
        plan = {'start_date': '2026-09-28', 'goal_date': '2026-12-27', 'start_weight': 78.0, 'goal_weight': 72.0}
        gap = hm.plan_gap(pace, plan, TODAY)
        self.assertEqual(gap['value'], '+0.0 kg')
        self.assertEqual(gap['status'], 'good')
        f = hm.goal_forecast(pace, 72.0, '2026-12-27', TODAY)
        self.assertEqual(f['value'], '2026/12/23ごろ')
        self.assertEqual(f['status'], 'good')
        flat = dict(pace, slope=-0.001, se=0.01)
        self.assertEqual(hm.goal_forecast(flat, 72.0, None, TODAY)['value'], '予測できません')

    def test_energy_consistency(self):
        pace = {'slope': -500 / 7700, 'se': 0.001, 'trend': 76, 'days': 20}
        history = [{'date': (TODAY - timedelta(days=i)).isoformat(), 'balance': -520} for i in range(1, 20)]
        self.assertEqual(hm.energy_consistency(history, pace, TODAY)['status'], 'good')
        history = [dict(h, balance=-1300) for h in history]
        m = hm.energy_consistency(history, pace, TODAY)
        self.assertEqual(m['status'], 'warn')
        self.assertIn('記録漏れ', m['detail'])
        self.assertEqual(hm.energy_consistency(history[:5], pace, TODAY)['status'], 'unknown')


class BodyStatusTests(unittest.TestCase):
    def test_home_bp_uses_jsh_home_thresholds(self):
        def bp(s, d, n=7):
            return hm.home_bp([pressure(TODAY - timedelta(days=i), s, d) for i in range(n)], TODAY, {})
        self.assertEqual(bp(112, 70)['status'], 'good')
        self.assertIn('正常高値', bp(118, 72)['detail'])
        self.assertIn('高値血圧', bp(128, 72)['detail'])
        self.assertIn('高値血圧', bp(110, 78)['detail'])
        self.assertIn('高血圧の範囲', bp(136, 80)['detail'])
        self.assertEqual(bp(136, 80, n=2)['status'], 'unknown')
        self.assertEqual(bp(128, 72, n=3)['confidence'], 'medium')

    def test_bmi_and_visceral_fat(self):
        self.assertEqual(hm.bmi_status([body(TODAY, 72)], {'height_cm': 170})['status'], 'good')
        self.assertIn('肥満（1度）', hm.bmi_status([body(TODAY, 80)], {'height_cm': 170})['detail'])
        vf = lambda v: hm.visceral_fat([{'measured_at': at(TODAY), 'values': {'visceral_fat': v}}])['status']
        self.assertEqual([vf(9), vf(10), vf(15)], ['good', 'ok', 'warn'])

    def sample(self, metric, start, end, value, source='Watch'):
        return {'metric': metric, 'start_at': start, 'end_at': end, 'value': value, 'source': source}

    def test_sleep_duration_and_regularity(self):
        samples = []
        for i in range(14):
            d = TODAY - timedelta(days=i)
            shift = 150 if i % 2 else 0   # irregular schedule: bedtime alternates by 150 minutes
            start = datetime(d.year, d.month, d.day, 0, tzinfo=hm.JST) - timedelta(hours=1) + timedelta(minutes=shift)
            samples.append(self.sample('sleep', start.isoformat(), (start + timedelta(hours=5, minutes=30)).isoformat(), 'core'))
            samples.append(self.sample('sleep', (start + timedelta(hours=2)).isoformat(), (start + timedelta(hours=3)).isoformat(), 'awake'))
        nights = hm.sleep_nights(samples, TODAY, 14)
        self.assertEqual(round(nights[TODAY]['minutes']), 330)
        self.assertEqual(hm.sleep_duration(nights)['status'], 'warn')
        reg = hm.sleep_regularity(nights)
        self.assertEqual(reg['status'], 'warn')
        self.assertEqual(hm.sleep_regularity({}).get('status'), 'unknown')

    def test_steps_target_depends_on_age_and_ignores_duplicate_sources(self):
        samples = []
        for i in range(1, 8):
            d = TODAY - timedelta(days=i)
            samples.append(self.sample('steps', at(d, 12), at(d, 13), 7000))
            samples.append(self.sample('steps', at(d, 14), at(d, 15), 0))
            samples.append(self.sample('steps', at(d, 12), at(d, 13), 7000, 'Phone'))
        self.assertEqual(hm.daily_steps(samples, TODAY, 40)['status'], 'ok')
        self.assertEqual(hm.daily_steps(samples, TODAY, 40)['value'], '7,000 歩')
        self.assertEqual(hm.daily_steps(samples, TODAY, 70)['status'], 'good')

    def test_resting_hr_proxy_and_rise(self):
        samples = []
        for i in range(28):
            d = TODAY - timedelta(days=i)
            base = 70 if i < 7 else 62
            samples += [self.sample('heart_rate', at(d, h), at(d, h), base + h) for h in range(8, 16)]
        m = hm.resting_hr(samples, TODAY)
        self.assertEqual(m['value'], '78 bpm（前3週比 +8）')
        self.assertEqual(m['status'], 'ok')


class EndToEndTests(unittest.TestCase):
    def test_compute_from_store_with_utc_timestamps(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(tmp.name)
        user = store.save_user({'name': '利用者'})['id']
        store.save_wellness_profile(user, {'height_cm': 172, 'age': 42, 'sex': 'male', 'goal_weight_kg': 70})
        rows = []
        for i in range(28):
            d = TODAY - timedelta(days=i)
            rows.append({'user_id': user, 'kind': 'body_composition', 'measured_at': at(d, 6),
                         'values': {'weight': round(76 + 0.07 * i, 2)}})
            rows.append({'user_id': user, 'kind': 'blood_pressure', 'measured_at': at(d, 6, 5),
                         'values': {'systolic': 138, 'diastolic': 86, 'pulse': 65}})
        store.add_records(rows)
        samples = [{'metric': 'steps', 'start_at': at(TODAY - timedelta(days=i), 9), 'end_at': at(TODAY - timedelta(days=i), 10),
                    'value': 9000, 'unit': 'count', 'source': 'Watch'} for i in range(1, 8)]
        apple_health.ingest(store, {'schema': 1, 'user_id': user, 'samples': samples})
        result = hm.compute(store, user, TODAY)
        by = {m['id']: m for m in result['diet'] + result['health']}
        self.assertEqual(by['weight_pace']['status'], 'good')
        self.assertEqual(by['home_bp']['status'], 'warn')
        self.assertEqual(by['steps']['status'], 'good')
        self.assertEqual(result['summary'][0], '減量ペースは週0.49kg減で適正。')
        self.assertIn('家庭血圧の7日平均は138/86で高血圧の範囲。', result['summary'])
        with self.assertRaises(ValueError):
            hm.compute(store, 'missing', TODAY)


if __name__ == '__main__':
    unittest.main()
