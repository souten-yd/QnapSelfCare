import copy
import tempfile
import unittest
from unittest.mock import patch
import apple_health as ah
from storage import Store

class ColumnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.user = self.store.save_user({'name': 'Columns'})['id']
        self.row = {'start': ['2026-10-30T01:00:00+09:00','2026-10-30T02:00:00+09:00'],
                    'end': ['2026-10-30T01:01:00+09:00','2026-10-30T02:01:00+09:00'], 'value': ['72','80']}
    def body(self, row, metric='heart_rate', unit='bpm'):
        return dict(schema=2, user_id=self.user, groups=dict(type=metric, source='unknown', unit=unit, samples=row))
    def test_formats_replay_and_persistence(self):
        for separator in (None, '\n', '\r\n', '\\n'):
            row = self.row if separator is None else {k:separator.join(v) for k,v in self.row.items()}
            result = ah.ingest(self.store, self.body(row))
            self.assertEqual(result['received'],2)
            self.assertEqual(result['inserted'],2 if separator is None else 0)
        self.assertEqual(ah.history(Store(self.tmp.name),self.user)['total'],2)
        rows=[dict(zip(self.row, v)) for v in zip(*self.row.values())]
        self.assertEqual(ah.ingest(self.store,self.body(rows))['duplicates'],2)
    def test_invalid_columns_are_atomic(self):
        variants=[]
        for key in self.row:
            a=copy.deepcopy(self.row);a[key]=a[key][:1];variants.append(a)
            a=copy.deepcopy(self.row);a[key][1]='';variants.append(a)
        variants += [dict(self.row, value=['72','bad']), dict(self.row, value=['72',True]),
                     dict(self.row, source_id='same'), dict(self.row, value=['72',None]),
                     dict(self.row, start=[self.row['start'][0],'2026-10-30T02:00:00'])]
        for row in variants:
            with self.subTest(row=row), self.assertRaises(ValueError):
                ah.ingest(self.store,self.body(row))
            self.assertEqual(ah.history(self.store,self.user)['total'],0)
    def test_six_metrics_and_limits(self):
        for metric,unit,values in [('steps','count',['1','2']),('active_energy','kcal',['1','2']),
            ('walking_running_distance','m',['1','2']),('oxygen_saturation','%',['98','97']),
            ('heart_rate','bpm',['72','80']),('sleep','',['レム','深い'])]:
            self.assertEqual(ah.ingest(self.store,self.body(dict(self.row,value=values),metric,unit))['received'],2)
        with patch.object(ah,'MAX_SAMPLES',1),self.assertRaises(ValueError):
            ah.ingest(self.store,self.body(self.row))
        self.assertEqual(ah.ingest(self.store,self.body(dict(start=[],end=[],value=[])))['received'],0)

    def test_english_sleep_labels_replay_and_atomic_unknown(self):
        row = dict(self.row, value=['REM', 'Core'])
        first = ah.ingest(self.store, self.body(row, 'sleep', ''))
        self.assertEqual(first['inserted'], 2)
        for values in (['rem', 'core'], ['レム', 'コア']):
            self.assertEqual(ah.ingest(self.store, self.body(dict(row,value=values),'sleep',''))['duplicates'], 2)
        deep = dict(start=self.row['start'][0], end=self.row['end'][0], value='Deep')
        self.assertEqual(ah.ingest(self.store,self.body([deep],'sleep',''))['inserted'],1)
        self.assertEqual(ah.ingest(self.store,self.body([dict(deep,value='deep')],'sleep',''))['duplicates'],1)
        with self.assertRaises(ValueError):
            ah.ingest(self.store,self.body(dict(row,value=['Deep','Unrecognized']),'sleep',''))
        self.assertEqual(ah.history(self.store,self.user)['total'],3)
