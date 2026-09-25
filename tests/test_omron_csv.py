import tempfile
import unittest

from storage import Store


BP = ('"測定日","タイムゾーン","最高血圧(mmHg)","最低血圧(mmHg)","脈拍(bpm)",'
      '"不規則脈波検出","不規則脈波検出回数(回)","体動検出","ぴったり巻き",'
      '"測定姿勢ガイド","室温(°C)","測定モード","機種"\n'
      '"2019/12/09 12:15","Asia/Tokyo","127","85","62","未検出","","未検出",'
      '"OK","低い","","","HEM-6232T"\n'
      '"2019/12/09 12:17","Asia/Tokyo","125","82","64","未検出","","未検出",'
      '"OK","正しい","","","HEM-6232T"\n')
WEIGHT = ('"測定日","タイムゾーン","体重(kg)","体脂肪(%)","体脂肪量(kg)",'
          '"内臓脂肪レベル","基礎代謝(kcal)","骨格筋(%)","骨格筋量(kg)",'
          '"BMI","体年齢(才)","機種"\n'
          '"2021/07/13 19:06","Asia/Tokyo","106.60","28.4","","17",'
          '"2157","30.0","","31.5","57","HBF-228T"\n')


class OmronHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.user = self.store.save_user({'name': '過去の利用者'})['id']

    def test_blood_pressure_preview_apply_duplicate_and_note(self):
        preview = self.store.omron_history(BP, self.user)
        self.assertEqual((preview['total'], preview['importable'], preview['duplicates']), (2, 2, 0))
        self.assertEqual(self.store.records()['total'], 0)
        applied = self.store.omron_history(BP, self.user, True, preview['preview_token'])
        self.assertEqual(applied['importable'], 2)
        rows = self.store.records()['records']
        self.assertEqual(rows[0]['values'], {'systolic': 125, 'diastolic': 82, 'pulse': 64})
        self.assertIn('測定姿勢ガイド: 低い', rows[1]['note'])
        self.assertEqual(rows[1]['measured_at'], '2019-12-09T03:15:00+00:00')
        self.assertEqual(self.store.omron_history(BP, self.user)['duplicates'], 2)
        other = self.store.save_user({'name': '別の利用者'})['id']
        self.assertEqual(self.store.omron_history(BP, other)['importable'], 2)

    def test_weight_values_and_existing_bluetooth_measurement_are_not_duplicated(self):
        self.store.add_records([{'user_id': self.user, 'kind': 'body_composition',
            'measured_at': '2021-07-13T19:06:00+09:00',
            'values': {'weight': 106.6, 'body_fat': 28.4}}], 'bluetooth')
        self.assertEqual(self.store.omron_history(WEIGHT, self.user)['duplicates'], 1)
        self.assertEqual(self.store.records()['total'], 1)
        other = self.store.save_user({'name': '別'})['id']
        preview = self.store.omron_history(WEIGHT, other)
        self.store.omron_history(WEIGHT, other, True, preview['preview_token'])
        row = self.store.records(user_id=other)['records'][0]
        self.assertEqual(row['values'], {'weight': 106.6, 'body_fat': 28.4,
            'visceral_fat': 17, 'bmr': 2157, 'muscle': 30, 'bmi': 31.5, 'body_age': 57})

    def test_invalid_row_or_changed_preview_is_atomic(self):
        preview = self.store.omron_history(WEIGHT, self.user)
        with self.assertRaisesRegex(ValueError, '確認をやり直して'):
            self.store.omron_history(WEIGHT + '\n', self.user, True, preview['preview_token'])
        bad = BP.replace('"125","82"', '"bad","82"')
        with self.assertRaisesRegex(ValueError, '3行目'):
            self.store.omron_history(bad, self.user, True, 'wrong')
        self.assertEqual(self.store.records()['total'], 0)
        with self.assertRaises(ValueError):
            self.store.omron_history(WEIGHT.replace('HBF-228T', 'HBF-999T'), self.user)


if __name__ == '__main__':
    unittest.main()
