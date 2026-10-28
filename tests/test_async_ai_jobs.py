import io
import json
import re
import tempfile
import unittest
from unittest.mock import patch

from storage import Store
from wellness_ai import Coach


class AsyncCoachTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.user = self.store.save_user({'name':'Async test user'})['id']
        self.coach = Coach(self.temp.name, self.store)
        self.coach.save({'provider':'qnapassistant','base_url':'','model':'auto'})

    def test_start_status_result_cancel(self):
        calls=[]
        job_id='req_'+('a'*48)

        class FakeResponse:
            def __init__(self,value,status): self.raw=json.dumps(value).encode(); self.status=status
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,size): return self.raw[:size]

        class FakeOpener:
            def open(self,request,timeout):
                calls.append((request,timeout))
                if request.get_method()=='POST':
                    payload=json.loads(request.data)
                    self_outer.assertEqual(payload['stream'],False)
                    self_outer.assertNotIn('model',payload)
                    self_outer.assertEqual(payload['messages'][1]['role'],'user')
                    return FakeResponse({'request_id':job_id,'status':'queued'},202)
                if request.get_method()=='DELETE':
                    return FakeResponse({'request_id':job_id,'status':'cancelled'},200)
                if request.full_url.endswith('/result'):
                    return FakeResponse({'choices':[{'message':{'content':'health advice'}}]},200)
                return FakeResponse({'request_id':job_id,'status':'generating',
                    'elapsed_seconds':120,'stage_elapsed_seconds':15,
                    'memory_available_bytes':8*1024**3},200)

        self_outer=self
        with patch('wellness_ai.build_opener',return_value=FakeOpener()):
            result=self.coach.start_consult(self.user,'health','最近の睡眠を分析して')
            self.assertEqual(result['request_id'],job_id)
            status=self.coach.consult_job_status(job_id)
            self.assertEqual(status['status'],'generating')
            self.assertEqual(self.coach.consult_job_result(job_id)['answer'],'health advice')
            self.assertEqual(self.coach.cancel_consult_job(job_id)['status'],'cancelled')
        self.assertEqual(len(calls),4)
        self.assertEqual(calls[0][0].full_url,'http://127.0.0.1:11435/api/llm/jobs')
        self.assertEqual(calls[1][0].full_url,'http://127.0.0.1:11435/api/llm/jobs/'+job_id)
        self.assertTrue(calls[2][0].full_url.endswith('/result'))
        self.assertEqual(calls[3][0].get_method(),'DELETE')
        self.assertTrue(all(timeout==15 for _,timeout in calls))

    def test_job_id_validation_and_remote_provider(self):
        for bad in ('', 'req_no', '../../etc/passwd', 'req_'+('a'*47), 'req_'+('Z'*48)):
            with self.assertRaisesRegex(ValueError,'受付ID'):
                self.coach.consult_job_status(bad)
        self.coach.save({'provider':'openrouter','base_url':'https://openrouter.ai/api/v1','model':'openai/gpt-4o-mini'})
        with self.assertRaisesRegex(ValueError,'QnapAssistant'):
            self.coach.start_consult(self.user,'health','相談です')

    def test_invalid_job_response_does_not_expose_raw_health_data(self):
        class FakeResponse:
            status=202
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,size): return b'{"request_id":"not-valid"}'
        with patch('wellness_ai.build_opener') as factory:
            factory.return_value.open.return_value=FakeResponse()
            with self.assertRaisesRegex(ValueError,'受付ID'):
                self.coach.start_consult(self.user,'health','相談内容は個人データです')
        self.assertNotIn('個人データ',str(factory.return_value.open.call_args))


if __name__=='__main__':
    unittest.main()
