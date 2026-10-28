import json
import tempfile
import unittest
from datetime import date

from storage import Store
from daily_advice import DailyAdvisor
from advice_diagnostics import sanitize_diagnostic, validate_advice_format, diagnose_failure


class DiagnosticCoach:
    def __init__(self):
        self.fail = False
        self.calls = 0
        self.id = 'req_' + ('a'*48)
    def settings(self):
        return {'provider':'qnapassistant','base_url':'http://127.0.0.1:11435/v1','model':'auto'}
    def interactive_waiting(self):
        return 0
    def ask(self,user_id,mode,question,progress=None,**kwargs):
        self.calls += 1
        if progress:progress({'request_id':self.id,'status':'generating','elapsed_seconds':12,'memory_available_bytes':2000000000})
        if self.fail:
            if progress:progress({'request_id':self.id,'status':'timed_out','elapsed_seconds':3600,'error_code':'generation_timeout_partial'})
            raise ValueError('QnapAssistant: generation timed out')
        return {'answer':'## 要点\n休む\n## 根拠になったデータ\n睡眠\n## 提案\n休養\n## 注意\n医療機関'}
    def job_diagnostics(self,job_id,include_output=False):
        assert job_id==self.id
        result={'request_id':job_id,'status':'timed_out' if self.fail else 'completed',
                'error_code':'generation_timeout_partial' if self.fail else '',
                'generation_seconds':3600 if self.fail else 11,
                'token_chunks_observed':123,'output_bytes':128,
                'raw_output_available':True,'raw_output_persisted':False}
        if include_output:result['raw_output']='途中生成した秘密の健康情報'
        return result


class AdviceDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name)
        self.user=self.store.save_user({'name':'Test'})['id']
        self.store.add_records([{'user_id':self.user,'kind':'body_composition',
            'measured_at':'2026-10-27T08:00:00+09:00','values':{'weight':77.5}}])
        self.coach=DiagnosticCoach()
        self.advisor=DailyAdvisor(self.store,self.coach)

    def test_valid_advice_has_separate_format_status_and_no_raw_in_history(self):
        entry=self.advisor.run(self.user,date(2026,10,27),'manual')
        self.assertEqual(entry['status'],'done')
        self.assertEqual(entry['diagnostics']['format_status'],'valid')
        self.assertEqual(entry['diagnostics']['format_warnings'],[])
        self.assertNotIn('request_id',json.dumps(entry))
        self.assertNotIn('diagnostic_job_id',json.dumps(entry))
        self.assertNotIn('秘密',json.dumps(entry))
        raw=self.advisor.raw_diagnostics(self.user,'2026-10-27')
        self.assertEqual(raw['raw_output'],'途中生成した秘密の健康情報')
        self.assertNotIn('raw_output',json.dumps(self.advisor.history(self.user)))

    def test_failure_remains_classified_and_redacted_after_restart(self):
        self.coach.fail=True
        entry=self.advisor.run(self.user,date(2026,10,27),'manual')
        self.assertEqual(entry['status'],'failed')
        self.assertEqual(entry['diagnostics']['error_code'],'generation_timeout_partial')
        self.assertEqual(entry['diagnostics']['output_bytes'],128)
        self.assertIn('途中出力',entry['diagnostics']['diagnosis'])
        reopened=DailyAdvisor(Store(self.tmp.name),self.coach)
        other=reopened.history(self.user)['latest']
        self.assertEqual(other['diagnostics']['generation_seconds'],3600)
        self.assertNotIn(self.coach.id,json.dumps(other))
        self.assertNotIn('秘密',json.dumps(other))
        with self.assertRaises(ValueError):
            reopened.raw_diagnostics(self.user,'not-a-day')

    def test_validation_warns_without_discarding_answer(self):
        warnings=validate_advice_format('## 要点\n本文だけ')
        self.assertEqual(len(warnings),3)
        self.assertEqual(validate_advice_format('## 要点\nA\n## 根拠になったデータ\nB\n## 提案\nC\n## 注意\nD'),[])
        self.assertEqual(diagnose_failure('generation timed out')['error_code'],'generation_timeout')
        sanitized=sanitize_diagnostic({'request_id':self.coach.id,'raw_output':'secret','prompt':'secret','generation_seconds':3})
        self.assertEqual(sanitized,{'generation_seconds':3})


if __name__=='__main__':
    unittest.main()
