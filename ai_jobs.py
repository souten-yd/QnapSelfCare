"""Background AI jobs so the browser never waits on a long model run.

The browser submits a job and polls its state every few seconds. Jobs run one at a time on a worker
thread (Coach.run_prompt serialises with the nightly advice too). State lives only in memory: question
and answer are not written to disk, and finished jobs are forgotten after 30 minutes.
"""
import queue
import secrets
import threading
import time

KINDS = {'consult': {'user_id', 'mode', 'question', 'history'}, 'meal': {'meals'}, 'test': set()}
REQUIRED = {'consult': {'user_id', 'mode', 'question'}, 'meal': {'meals'}, 'test': set()}
STAGES = {'waiting': '前の処理の完了を待っています', 'queued': 'AIの順番待ち', 'loading_model': 'モデルを読み込み中',
          'ready': '推論の準備完了', 'generating': '回答を生成中'}
KEEP_SECONDS = 30 * 60
MAX_PENDING = 5


class AIJobs:
    def __init__(self, coach, clock=time.monotonic):
        self.coach, self.clock = coach, clock
        self._jobs, self._lock = {}, threading.Lock()
        self._queue = queue.Queue()
        self._worker = None

    def submit(self, body):
        kind = body.get('kind') if isinstance(body, dict) else None
        if kind not in KINDS:
            raise ValueError('AI処理の種類が不正です')
        params = {k: v for k, v in body.items() if k != 'kind'}
        if not REQUIRED[kind] <= set(params) <= KINDS[kind]:
            raise ValueError('AI処理の内容が不正です')
        if kind == 'consult':  # validate before queueing so mistakes fail fast
            self.coach._consult_payload(params['user_id'], params['mode'], params['question'], history=params.get('history'))
        elif kind == 'meal':
            self.coach._meal_payload(params['meals'])
        with self._lock:
            self._expire()
            if sum(1 for j in self._jobs.values() if j['state'] in ('queued', 'running')) >= MAX_PENDING:
                raise ValueError('AIの処理待ちが多すぎます。完了してから再実行してください')
            job_id = 'job_' + secrets.token_hex(16)
            now = self.clock()
            self._jobs[job_id] = {'id': job_id, 'kind': kind, 'state': 'queued', 'stage': 'queued', 'created': now,
                                  'started': None, 'finished': None, 'error': None, 'result': None, 'detail': None,
                                  'cancel': False, 'params': params}
        self._ensure_worker()
        self._queue.put(job_id)
        return self.status(job_id)

    def status(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ValueError('AI処理が見つかりません。SelfCareが再起動したか、保存期間（30分）を過ぎました')
            now = self.clock()
            end = job['finished'] or now
            return {'id': job['id'], 'kind': job['kind'], 'state': job['state'], 'stage': job['stage'],
                    'stage_label': STAGES.get(job['stage'], ''), 'elapsed_seconds': int(end - job['created']),
                    'detail': job['detail'], 'error': job['error'], 'result': job['result']}

    def cancel(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ValueError('AI処理が見つかりません')
            job['cancel'] = True
            if job['state'] == 'queued':
                job.update(state='cancelled', finished=self.clock())
        return self.status(job_id)

    def _expire(self):
        now = self.clock()
        for key in [k for k, j in self._jobs.items() if j['finished'] and now - j['finished'] > KEEP_SECONDS]:
            del self._jobs[key]

    def _ensure_worker(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._work, name='ai-jobs', daemon=True)
            self._worker.start()

    def _work(self):
        while True:
            try:
                job_id = self._queue.get(timeout=60)
            except queue.Empty:
                return  # idle worker exits; the next submit starts a new one
            self.run(job_id)

    def run(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job['state'] != 'queued':
                return
            job.update(state='running', started=self.clock())
        def progress(state):
            with self._lock:
                job['stage'] = state.get('status') or job['stage']
                parts = []
                if state.get('memory_available_bytes'):
                    parts.append(f"空きRAM {state['memory_available_bytes'] / 1073741824:.1f} GiB")
                if state.get('stage_elapsed_seconds') is not None:
                    parts.append(f"この段階 {state['stage_elapsed_seconds']}秒")
                job['detail'] = '・'.join(parts) or None
        cancelled = lambda: job['cancel']
        try:
            p, coach = job['params'], self.coach
            if job['kind'] == 'consult':
                result = coach.ask(p['user_id'], p['mode'], p['question'], history=p.get('history'),
                                   progress=progress, cancelled=cancelled)
            else:
                config, prompt = coach._meal_payload(p['meals']) if job['kind'] == 'meal' else coach._test_payload()
                result = coach.run_prompt(config, prompt, progress, cancelled)
            outcome = {'state': 'cancelled'} if job['cancel'] else {'state': 'completed', 'result': result}
        except ValueError as error:
            outcome = {'state': 'cancelled' if job['cancel'] else 'failed', 'error': str(error)}
        except Exception:
            outcome = {'state': 'failed', 'error': 'AI処理で予期しないエラーが発生しました'}
        with self._lock:
            job.update(outcome, finished=self.clock(), params=None)
