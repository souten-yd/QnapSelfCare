import json
import tempfile
import unittest
from unittest.mock import patch

from storage import Store
from wellness_ai import Coach


class MealEstimateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.save_user({'name':'A'})
        self.coach = Coach(self.tmp.name, self.store)
        self.coach.save({'provider':'local','base_url':'http://localhost:8765/v1','model':'auto'})

    def test_meal_estimate_sends_only_meal_text_and_validates_json(self):
        captured = {}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size):
                return json.dumps({'choices':[{'message':{'content':json.dumps({
                    'total_kcal': 1820,
                    'meals':[{'name':'朝食','kcal':420,'basis':'一般的な一人前'}],
                    'assumptions':['量は標準量を仮定'],
                    'web_research_used':False,
                    'summary':'概算です'
                }, ensure_ascii=False)}}]}).encode()
        class Opener:
            def open(self, request, timeout):
                captured['payload'] = json.loads(request.data)
                return Response()
        with patch('wellness_ai.build_opener', return_value=Opener()):
            result = self.coach.estimate_meal({'date':'2026-09-26','breakfast':'トーストと卵',
                'lunch':'カレーライス','dinner':'焼き魚定食','snacks':'コーヒー'})
        self.assertEqual(result['total_kcal'], 1820)
        wire = json.dumps(captured['payload'], ensure_ascii=False)
        self.assertIn('トーストと卵', wire)
        self.assertNotIn('measurements', wire)
        self.assertFalse(result['web_research_used'])

    def test_meal_estimate_rejects_empty(self):
        with self.assertRaises(ValueError):
            self.coach.estimate_meal({'date':'2026-09-26','breakfast':'','lunch':'','dinner':'','snacks':''})


if __name__ == '__main__':
    unittest.main()
