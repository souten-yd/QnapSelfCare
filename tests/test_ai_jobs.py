import tempfile
import threading
import unittest
from unittest.mock import patch

import ai_jobs
import wellness_ai
from storage import Store
from wellness_ai import Coach

REQ = 'req_' + 'a' * 48


class AssistantRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.user = self.store.save_user({'name': '利用者'})['id']
        self.coach = Coach(self.tmp.name, self.store)
        self.coach.save({'provider': 'qnapassistant', 'base_url': 'http://127.0.0.1:11435/v1', 'model': 'auto'})
        self.coach._sleep = lambda seconds: None
        self.calls = []

    def fake(self, states, result='## 要点\nOK'):
        states = list(states)
        def request(url, payload=None, method='GET'):
            self.calls.append((method, url.rsplit('/', 1)[-1]))
            if method == 'POST':
                return {'request_id': REQ, 'status': 'queued'}
            if method == 'DELETE':
                return {'status': 'cancelled'}
            if url.endswith('/result'):
                return {'choices': [{'message': {'content': result}}]}
            state = states.pop(0)
            if isinstance(state, Exception):
                raise state
            return {'request_id': REQ, 'status': state, 'memory_available_bytes': 4 * 1073741824}
        return patch.object(self.coach, '_assistant_job_request', side_effect=request)

    def test_long_model_load_is_not_cut_off_at_300_seconds(self):
        # 200 polls x 3 s = 10 minutes of loading/generation before completion.
        states = ['queued'] + ['loading_model'] * 120 + ['generating'] * 79 + ['completed']
        seen = []
        with self.fake(states):
            result = self.coach.ask(self.user, 'health', '今週は？', progress=seen.append)
        self.assertIn('OK', result['answer'])
        self.assertEqual([c for c in self.calls if c[0] == 'POST'], [('POST', 'jobs')])
        self.assertEqual(seen[-1]['status'], 'completed')

    def test_transient_status_errors_are_retried_but_lost_jobs_are_not_resubmitted(self):
        with self.fake(['queued', ValueError('QnapAssistantと通信できません'), 'generating', 'completed']):
            self.assertIn('OK', self.coach.ask(self.user, 'health', 'q')['answer'])
        self.calls.clear()
        with self.fake(['queued', ValueError('受付IDが見つかりません。サービスが再起動したか、保存期間を超えました')]):
            with self.assertRaises(ValueError):
                self.coach.ask(self.user, 'health', 'q')
        self.assertEqual(sum(1 for c in self.calls if c[0] == 'POST'), 1)

    def test_deadline_failure_and_cancel_send_delete(self):
        with patch.object(wellness_ai, 'ASSISTANT_DEADLINE_SECONDS', -1), self.fake(['generating']):
            with self.assertRaisesRegex(ValueError, '45分'):
                self.coach.ask(self.user, 'health', 'q')
        self.assertIn('DELETE', [c[0] for c in self.calls])
        with self.fake(['timed_out']):
            with self.assertRaisesRegex(ValueError, '時間切れ'):
                self.coach.ask(self.user, 'health', 'q')
        self.calls.clear()
        with self.fake(['generating'] * 5):
            with self.assertRaisesRegex(ValueError, 'キャンセル'):
                self.coach.ask(self.user, 'health', 'q', cancelled=lambda: True)
        self.assertIn('DELETE', [c[0] for c in self.calls])

    def test_runs_are_serialised(self):
        self.coach._serial.acquire()   # e.g. the nightly advice is running
        done = threading.Event()
        with self.fake(['completed']):
            t = threading.Thread(target=lambda: (self.coach.ask(self.user, 'health', 'q'), done.set()), daemon=True)
            t.start()
            self.assertFalse(done.wait(0.3))           # waits instead of failing
            self.coach._serial.release()
            self.assertTrue(done.wait(10))


class FakeCoach:
    def __init__(self):
        self.asked = []

    def _consult_payload(self, user_id, mode, question, history=None):
        if mode == 'bad':
            raise ValueError('相談内容または種類が不正です')

    def _meal_payload(self, meals):
        return {}, {}

    def _test_payload(self):
        return {}, {}

    def ask(self, user_id, mode, question, history=None, progress=None, cancelled=None):
        progress({'status': 'loading_model', 'memory_available_bytes': 2 * 1073741824, 'stage_elapsed_seconds': 40})
        self.asked.append((mode, question, history))
        return {'answer': 'consult answer'}

    def run_prompt(self, config, prompt, progress=None, cancelled=None):
        return {'answer': 'other answer'}


class JobQueueTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.jobs = ai_jobs.AIJobs(FakeCoach(), clock=lambda: self.now)
        self.jobs._ensure_worker = lambda: None   # run jobs explicitly in the test thread

    def test_submit_run_and_status(self):
        job = self.jobs.submit({'kind': 'consult', 'user_id': 'u', 'mode': 'health', 'question': 'q', 'history': []})
        self.assertEqual(job['state'], 'queued')
        self.now += 400   # far beyond the old 300 s limit
        self.jobs.run(job['id'])
        status = self.jobs.status(job['id'])
        self.assertEqual(status['state'], 'completed')
        self.assertEqual(status['result']['answer'], 'consult answer')
        self.assertEqual(status['stage'], 'loading_model')
        self.assertIn('空きRAM 2.0 GiB', status['detail'])
        self.assertIsNone(self.jobs._jobs[job['id']]['params'])   # question dropped from memory
        meal = self.jobs.submit({'kind': 'meal', 'meals': {'date': '', 'breakfast': 'パン', 'lunch': '', 'dinner': '', 'snacks': ''}})
        self.jobs.run(meal['id'])
        self.assertEqual(self.jobs.status(meal['id'])['result']['answer'], 'other answer')

    def test_validation_cancel_limits_and_expiry(self):
        for bad in ({'kind': 'x'}, {'kind': 'consult', 'user_id': 'u'}, {'kind': 'test', 'extra': 1},
                    {'kind': 'consult', 'user_id': 'u', 'mode': 'bad', 'question': 'q'}):
            with self.assertRaises(ValueError):
                self.jobs.submit(bad)
        first = self.jobs.submit({'kind': 'test'})
        self.assertEqual(self.jobs.cancel(first['id'])['state'], 'cancelled')
        self.jobs.run(first['id'])   # a cancelled queued job never runs
        self.assertEqual(self.jobs.status(first['id'])['state'], 'cancelled')
        for _ in range(ai_jobs.MAX_PENDING):
            self.jobs.submit({'kind': 'test'})
        with self.assertRaisesRegex(ValueError, '多すぎます'):
            self.jobs.submit({'kind': 'test'})
        self.now += ai_jobs.KEEP_SECONDS + 1
        self.jobs._expire()
        with self.assertRaisesRegex(ValueError, '見つかりません'):
            self.jobs.status(first['id'])


if __name__ == '__main__':
    unittest.main()
