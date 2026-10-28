import tempfile
import unittest
from datetime import date, datetime, timedelta

import daily_advice as da
from storage import Store

JST = da.JST


class FakeCoach:
    def __init__(self, provider='local', base_url='http://localhost:8765/v1'):
        self.config = {'provider': provider, 'base_url': base_url, 'model': 'auto'}
        self.calls, self.fail = [], None
        self.job_states = ['queued', 'generating', 'completed']

    def settings(self):
        return dict(self.config)

    def consult(self, user_id, mode, question, extra=None):
        self.calls.append(('consult', mode, extra))
        if self.fail:
            raise ValueError(self.fail)
        return {'answer': '## 要点\n良い1日でした'}

    def start_consult(self, user_id, mode, question, extra=None):
        self.calls.append(('start', mode, extra))
        return {'request_id': 'req_' + '0' * 48, 'status': 'queued'}

    def consult_job_status(self, request_id):
        return {'request_id': request_id, 'status': self.job_states.pop(0)}

    def consult_job_result(self, request_id):
        return {'answer': '## 要点\nQnapAssistantの回答'}

    def cancel_consult_job(self, request_id):
        self.calls.append(('cancel', request_id))


class DailyAdviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.user = self.store.save_user({'name': '利用者'})['id']
        self.store.save_wellness_profile(self.user, {'height_cm': 170, 'age': 40, 'sex': 'male'})
        self.now = datetime(2026, 10, 28, 1, 59, tzinfo=JST)
        self.coach = FakeCoach()
        self.advisor = da.DailyAdvisor(self.store, self.coach, clock=lambda: self.now, sleep=lambda s: None)

    def add_day(self, d, weight=75.0):
        self.store.add_records([{'user_id': self.user, 'kind': 'body_composition',
                                 'measured_at': datetime(d.year, d.month, d.day, 7, tzinfo=JST).isoformat(),
                                 'values': {'weight': weight}}])

    def test_settings_validation_and_target_day(self):
        self.assertEqual(self.advisor.settings(), da.DEFAULT)
        self.assertEqual(self.advisor.save({'time': '05:30'})['time'], '05:30')
        self.assertEqual(da.DailyAdvisor(self.store, self.coach).settings()['time'], '05:30')  # persisted in DB
        for bad in ({'time': '24:00'}, {'time': '2:00'}, {'enabled': 'yes'}, {'unknown': 1}):
            with self.assertRaises(ValueError):
                self.advisor.save(bad)
        moment = datetime(2026, 10, 28, 2, 0, tzinfo=JST)
        self.assertEqual(da.target_day(moment, '02:00'), date(2026, 10, 27))
        self.assertEqual(da.target_day(moment, '21:00'), date(2026, 10, 28))
        self.assertFalse(da.due(datetime(2026, 10, 28, 1, 59, tzinfo=JST), '02:00'))
        self.assertTrue(da.due(datetime(2026, 10, 28, 2, 0, tzinfo=JST), '02:00'))

    def test_tick_runs_once_per_day_after_the_scheduled_time(self):
        for i in range(8):
            self.add_day(date(2026, 10, 27) - timedelta(days=i), 75 + i * 0.1)
        self.assertEqual(self.advisor.tick(), [])               # 01:59, not yet due
        self.now = self.now.replace(hour=2, minute=0)
        rows = self.advisor.tick()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['day'], '2026-10-27')
        self.assertEqual(rows[0]['status'], 'done')
        self.assertIn('良い1日', rows[0]['answer'])
        kind, mode, extra = self.coach.calls[0]
        self.assertEqual((kind, mode), ('consult', 'daily'))
        self.assertEqual(extra['day_review']['date'], '2026-10-27')
        weight = next(i for i in extra['day_review']['items'] if i['id'] == 'weight')
        self.assertEqual(weight['value'], 75.0)
        self.assertEqual(self.advisor.tick(), [])               # already done today
        self.advisor.save({'enabled': False})
        self.now = self.now + timedelta(days=1)
        self.assertEqual(self.advisor.tick(), [])               # disabled

    def test_no_records_or_no_ai_does_not_send_anything(self):
        row = self.advisor.run(self.user, date(2026, 10, 27), 'schedule')
        self.assertEqual(row['status'], 'skipped')
        self.add_day(date(2026, 10, 27))
        self.coach.config['base_url'] = ''
        row = self.advisor.run(self.user, date(2026, 10, 27), 'schedule')
        self.assertEqual(row['status'], 'review_only')
        self.assertIsNotNone(row['review'])
        self.assertEqual(self.coach.calls, [])

    def test_external_provider_needs_explicit_permission_for_the_schedule(self):
        self.add_day(date(2026, 10, 27))
        self.coach.config['provider'] = 'openrouter'
        self.assertEqual(self.advisor.run(self.user, date(2026, 10, 27), 'schedule')['status'], 'review_only')
        self.assertIn('外部サービス', self.advisor.status()['ai_blocked'])
        self.assertEqual(self.advisor.run(self.user, date(2026, 10, 27), 'manual')['status'], 'done')
        self.advisor.save({'allow_external': True})
        self.assertEqual(self.advisor.run(self.user, date(2026, 10, 27), 'schedule')['status'], 'done')

    def test_busy_ai_is_retried_then_fails(self):
        self.add_day(date(2026, 10, 27))
        self.coach.fail = 'AI相談が実行中です。完了してから再試行してください'
        statuses = []
        for _ in range(da.MAX_ATTEMPTS):
            statuses.append(self.advisor.run(self.user, date(2026, 10, 27), 'schedule')['status'])
        self.assertEqual(statuses, ['retry', 'retry', 'failed'])
        self.coach.fail = 'AI HTTP 401: APIキーを確認してください'
        self.add_day(date(2026, 10, 26))
        self.assertEqual(self.advisor.run(self.user, date(2026, 10, 26), 'schedule')['status'], 'failed')

    def test_qnapassistant_uses_async_jobs(self):
        self.add_day(date(2026, 10, 27))
        self.coach.config['provider'] = 'qnapassistant'
        row = self.advisor.run(self.user, date(2026, 10, 27), 'manual')
        self.assertEqual(row['status'], 'done')
        self.assertIn('QnapAssistant', row['answer'])
        self.assertEqual(self.coach.calls[0][0], 'start')
        self.coach.job_states = ['queued', 'failed']
        self.assertEqual(self.advisor.run(self.user, date(2026, 10, 27), 'manual')['status'], 'failed')

    def test_history_and_one_run_at_a_time(self):
        self.add_day(date(2026, 10, 27))
        self.advisor.run(self.user, date(2026, 10, 27), 'manual')
        data = self.advisor.history(self.user)
        self.assertEqual(data['latest']['day'], '2026-10-27')
        self.assertEqual(data['settings']['next_run'], '2026-10-28T02:00+09:00')
        self.assertEqual(data['settings']['reviews_day'], '2026-10-27')
        self.advisor._lock.acquire()
        try:
            with self.assertRaises(ValueError):
                self.advisor.run(self.user, date(2026, 10, 27), 'manual')
        finally:
            self.advisor._lock.release()
        with self.assertRaises(ValueError):
            self.advisor.history('missing')

    def test_restart_marks_running_rows_failed(self):
        self.add_day(date(2026, 10, 27))
        self.advisor._put(self.user, '2026-10-27', status='running', trigger='schedule')
        restarted = Store(self.tmp.name)
        row = da.DailyAdvisor(restarted, self.coach)._get(self.user, '2026-10-27')
        self.assertEqual(row['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
