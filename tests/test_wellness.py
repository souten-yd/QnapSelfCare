import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from storage import Store
from wellness import summarize
from wellness_ai import Coach, validate


class WellnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.user = self.store.save_user({'name': '利用者'})['id']

    def record(self, date, weight):
        return {'user_id': self.user, 'kind': 'body_composition', 'measured_at': date,
                'values': {'weight': weight}}

    def test_same_day_multiple_readings_and_trend_bmr_bmi(self):
        rows = [self.record('2026-09-01T08:00:00+09:00', 80),
                self.record('2026-09-01T20:00:00+09:00', 82),
                self.record('2026-09-10T08:00:00+09:00', 79)]
        self.assertEqual(self.store.add_records(rows)['inserted'], 3)
        profile = self.store.save_wellness_profile(self.user, {'height_cm': 180, 'age': 40,
            'sex': 'male', 'goal_weight_kg': 76, 'goal_date': '2026-12-01'})
        summary = summarize(profile, self.store.records(user_id=self.user)['records'],
                            current=datetime(2026, 9, 25, tzinfo=timezone.utc))
        self.assertEqual(summary['daily_weights'][0]['readings'], 2)
        self.assertEqual(summary['weight_change_7d_kg'], -2)
        self.assertEqual(summary['bmi'], 24.4)
        self.assertEqual(summary['estimated_bmr_kcal'], 1720)
        self.assertEqual(self.store.add_records([rows[1]]), {'inserted': 0, 'duplicates': 1})
        with self.assertRaises(ValueError):
            self.store.save_wellness_profile(self.user, {'bmi_low': 27, 'bmi_high': 24})

    def test_backup_restores_meals_profile_ai_without_secret(self):
        self.store.save_wellness_profile(self.user, {'plan_text': '週に一度確認'})
        self.store.add_meal(self.user, {'note': '夕食：魚と野菜', 'calories': 550})
        coach = Coach(self.tmp.name, self.store)
        coach.save({'provider': 'controldeck', 'base_url': 'http://ubuntu.ts.net:8765/api/v1/llm/v1', 'model': 'auto'})
        coach.set_key('controldeck', 'test-secret')
        backup = self.store.backup()
        self.assertNotIn('test-secret', json.dumps(backup))
        with tempfile.TemporaryDirectory() as empty:
            restored = Store(empty)
            restored.restore(backup)
            self.assertEqual(restored.wellness_profile(self.user)['plan_text'], '週に一度確認')
            self.assertEqual(restored.meals(self.user)[0]['calories'], 550)
            self.assertEqual(Coach(empty, restored).settings()['provider'], 'controldeck')
            self.assertFalse(Coach(empty, restored).status()['key_configured'])

    def test_ai_requires_consent_and_sends_aggregate_only(self):
        coach = Coach(self.tmp.name, self.store)
        coach.save({'provider': 'local', 'base_url': 'http://localhost:8765/v1', 'model': 'auto'})
        with self.assertRaisesRegex(ValueError, '確認'):
            coach.consult(self.user, 'meal', '献立を考える', False)
        requests = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'{"choices":[{"message":{"content":"balanced meal"}}]}'
        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                self_timeout = timeout
                return Response()
        with patch('wellness_ai.build_opener', return_value=Opener()):
            self.assertEqual(coach.consult(self.user, 'meal', '献立を考える', True)['answer'], 'balanced meal')
        data = json.loads(requests[0].data)
        self.assertEqual(data['messages'][1]['role'], 'user')
        self.assertNotIn(self.user, json.dumps(data))
        self.assertEqual(requests[0].full_url, 'http://localhost:8765/v1/chat/completions')
        with self.assertRaises(ValueError):
            validate({'provider':'local','base_url':'http://example.com:8765/v1','model':'auto'})


if __name__ == '__main__': unittest.main()
