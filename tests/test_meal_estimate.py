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

    def test_meal_estimate_returns_prose_without_parsing_or_health_context(self):
        captured = {}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size):
                return json.dumps({'choices':[{'message':{'content':
                    '朝食: トースト 200 kcal、卵 80 kcal。小計 280 kcal。\n昼食: 700 kcal。\n夕食: 600 kcal。\n間食: 100 kcal。\n合計 1680 kcal。'}}]}, ensure_ascii=False).encode()
        class Opener:
            def open(self, request, timeout):
                captured['payload'] = json.loads(request.data)
                return Response()
        with patch('wellness_ai.build_opener', return_value=Opener()):
            result = self.coach.estimate_meal({'date':'2026-09-26','breakfast':'トーストと卵',
                'lunch':'カレーライス','dinner':'焼き魚定食','snacks':'コーヒー'})
        self.assertIn('トースト 200 kcal', result['answer'])
        self.assertIn('合計 1680 kcal', result['answer'])
        wire = json.dumps(captured['payload'], ensure_ascii=False)
        self.assertIn('トーストと卵', wire)
        self.assertNotIn('measurements', wire)
        self.assertIn('品目', wire)
        self.assertNotIn('required_json_shape', wire)

    def test_meal_estimate_rejects_empty(self):
        with self.assertRaises(ValueError):
            self.coach.estimate_meal({'date':'2026-09-26','breakfast':'','lunch':'','dinner':'','snacks':''})


if __name__ == '__main__':
    unittest.main()
