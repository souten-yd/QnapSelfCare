"""Once-a-day review of each user's day with AI advice, run on the NAS at a configured time.

The review itself is deterministic (health_metrics.day_review) and always stored. AI advice is
added when an AI connection is configured. QnapAssistant uses its asynchronous job API; other
providers use the synchronous OpenAI-compatible call. Nothing is sent to an external provider
(OpenRouter) unless the user explicitly allowed it for the schedule.
"""
from datetime import datetime, timedelta
import json
import re
import threading
import time

import energy
import health_metrics

JST = energy.JST
DEFAULT = {'enabled': True, 'time': '02:00', 'allow_external': False}
MAX_ATTEMPTS = 3
RETRY_SECONDS = 600
POLL_SECONDS = 15
JOB_DEADLINE_SECONDS = 45 * 60   # QnapAssistant: load 600 s + generation 1800 s by default
TERMINAL = {'completed', 'failed', 'timed_out', 'cancelled'}


def now_iso():
    return datetime.now(JST).isoformat(timespec='seconds')


def validate(config):
    if not isinstance(config, dict) or set(config) - set(DEFAULT):
        raise ValueError('毎日のアドバイス設定の項目が不正です')
    result = dict(DEFAULT, **config)
    if not isinstance(result['enabled'], bool) or not isinstance(result['allow_external'], bool):
        raise ValueError('有効・無効の指定が不正です')
    if not isinstance(result['time'], str) or not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', result['time']):
        raise ValueError('実行時刻はHH:MM（00:00〜23:59）で指定してください')
    return result


def target_day(moment, scheduled):
    """Morning runs review the day that just ended; afternoon/evening runs review the same day."""
    hour = int(scheduled.split(':')[0])
    return moment.date() - timedelta(days=1) if hour < 12 else moment.date()


def due(moment, scheduled):
    hour, minute = map(int, scheduled.split(':'))
    return (moment.hour, moment.minute) >= (hour, minute)


class DailyAdvisor:
    def __init__(self, store, coach, clock=None, sleep=time.sleep):
        self.store, self.coach = store, coach
        self.clock = clock or (lambda: datetime.now(JST))
        self.sleep = sleep
        self._lock = threading.Lock()        # one run at a time (QnapAssistant runs one job)
        self._stop = threading.Event()
        self._thread = None
        self._config = None
        self._retry_at = {}

    # ---- settings ----
    def settings(self):
        if self._config is None:  # cached: the minute tick must not wake the NAS disk
            with self.store.connect() as db:
                row = db.execute('SELECT config FROM ai_daily_schedule WHERE id=1').fetchone()
            try:
                self._config = validate(json.loads(row[0])) if row else dict(DEFAULT)
            except (ValueError, json.JSONDecodeError):
                self._config = dict(DEFAULT)
        return dict(self._config)

    def save(self, payload):
        config = validate(dict(self.settings(), **payload) if isinstance(payload, dict) else payload)
        with self.store.connect() as db:
            db.execute('INSERT INTO ai_daily_schedule(id,config) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config',
                       (json.dumps(config),))
        self._config = config
        return self.status()

    def status(self):
        config = self.settings()
        moment = self.clock()
        hour, minute = map(int, config['time'].split(':'))
        nxt = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if nxt <= moment:
            nxt += timedelta(days=1)
        provider = self.coach.settings()['provider']
        blocked = None
        if not self.coach.settings()['base_url']:
            blocked = 'AIの接続先が未設定のため、記録の比較だけを作成します'
        elif provider == 'openrouter' and not config['allow_external']:
            blocked = '外部サービス（OpenRouter）への自動送信が許可されていないため、記録の比較だけを作成します'
        return dict(config, next_run=nxt.isoformat(timespec='minutes'), provider=provider,
                    reviews_day=target_day(nxt, config['time']).isoformat(), ai_blocked=blocked,
                    running=self._lock.locked())

    # ---- results ----
    def _row(self, row):
        if not row:
            return None
        item = dict(row)
        item['review'] = json.loads(item['review']) if item['review'] else None
        return item

    def history(self, user_id, limit=14):
        with self.store.connect() as db:
            if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
                raise ValueError('利用者が見つかりません')
            rows = db.execute('SELECT * FROM ai_daily_advice WHERE user_id=? ORDER BY day DESC LIMIT ?', (user_id, limit)).fetchall()
        items = [self._row(r) for r in rows]
        return {'latest': items[0] if items else None, 'history': items, 'settings': self.status()}

    def _get(self, user_id, day):
        with self.store.connect() as db:
            return self._row(db.execute('SELECT * FROM ai_daily_advice WHERE user_id=? AND day=?', (user_id, day)).fetchone())

    def _put(self, user_id, day, **fields):
        stamp = now_iso()
        with self.store.connect() as db:
            if not db.execute('SELECT 1 FROM ai_daily_advice WHERE user_id=? AND day=?', (user_id, day)).fetchone():
                db.execute("INSERT INTO ai_daily_advice(user_id,day,created_at,updated_at,status,trigger) VALUES(?,?,?,?,?,?)",
                           (user_id, day, stamp, stamp, fields.get('status', 'running'), fields.get('trigger', 'schedule')))
            if 'review' in fields:
                fields['review'] = json.dumps(fields['review'], ensure_ascii=False)
            sets = ', '.join(f'{k}=?' for k in fields)
            db.execute(f'UPDATE ai_daily_advice SET {sets}, updated_at=? WHERE user_id=? AND day=?',
                       list(fields.values()) + [stamp, user_id, day])

    # ---- running ----
    def run(self, user_id, day, trigger='manual'):
        """Create (or recreate) the review and advice for one user and day. Blocks until done."""
        if not self._lock.acquire(blocking=False):
            raise ValueError('毎日のアドバイスを作成中です。完了してから再実行してください')
        try:
            return self._run(user_id, day, trigger)
        finally:
            self._lock.release()

    def start(self, user_id, day, trigger='manual'):
        """Run in the background for the web UI; the caller polls history()."""
        if self._lock.locked():
            raise ValueError('毎日のアドバイスを作成中です。完了してから再実行してください')
        self.store.wellness_profile(user_id)  # validates the user before accepting
        threading.Thread(target=self._safe_run, args=(user_id, day, trigger), daemon=True).start()
        return {'accepted': True, 'day': day.isoformat()}

    def _safe_run(self, user_id, day, trigger):
        try:
            self.run(user_id, day, trigger)
        except Exception:
            pass  # status is stored on the row; nothing else to report from a background thread

    def _run(self, user_id, day, trigger):
        key = day.isoformat()
        previous = self._get(user_id, key)
        attempts = (previous['attempts'] if previous and trigger == 'schedule' else 0) + 1
        review = health_metrics.day_review(self.store, user_id, day)
        self._put(user_id, key, status='running', trigger=trigger, attempts=attempts, review=review,
                  answer=None, error=None, provider=None)
        if review['recorded'] == 0:
            self._put(user_id, key, status='skipped', error='この日の記録がないため、AIには送信していません')
            return self._get(user_id, key)
        config, ai = self.settings(), self.coach.settings()
        if not ai['base_url']:
            self._put(user_id, key, status='review_only', error='AIの接続先が未設定です（記録の比較のみ）')
            return self._get(user_id, key)
        if ai['provider'] == 'openrouter' and trigger == 'schedule' and not config['allow_external']:
            self._put(user_id, key, status='review_only', error='外部サービスへの自動送信は許可されていません（記録の比較のみ）')
            return self._get(user_id, key)
        question = (f'{key}の1日を、直前7日間の傾向（context.day_review）と比べて評価し、'
                    '翌日に向けた生活・食事・運動・睡眠・減量のアドバイスをください。')
        extra = {'day_review': review, 'review_date': key}
        try:
            if ai['provider'] == 'qnapassistant':
                answer = self._run_job(user_id, question, extra)
            else:
                answer = self.coach.consult(user_id, 'daily', question, extra=extra)['answer']
        except ValueError as error:
            busy = re.search(r'実行中|混雑|429', str(error))
            status = 'retry' if busy and trigger == 'schedule' and attempts < MAX_ATTEMPTS else 'failed'
            if status == 'retry':
                self._retry_at[(user_id, key)] = time.monotonic() + RETRY_SECONDS
            self._put(user_id, key, status=status, error=str(error), provider=ai['provider'])
            return self._get(user_id, key)
        self._put(user_id, key, status='done', answer=answer, provider=ai['provider'])
        return self._get(user_id, key)

    def _run_job(self, user_id, question, extra):
        accepted = self.coach.start_consult(user_id, 'daily', question, extra=extra)
        request_id = accepted['request_id']
        waited = 0
        while waited < JOB_DEADLINE_SECONDS and not self._stop.is_set():
            self.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            state = self.coach.consult_job_status(request_id)
            if state.get('status') == 'completed':
                return self.coach.consult_job_result(request_id)['answer']
            if state.get('status') in TERMINAL:
                raise ValueError('QnapAssistantで処理が完了しませんでした: ' + str(state.get('error') or state.get('status')))
        try:
            self.coach.cancel_consult_job(request_id)
        except ValueError:
            pass
        raise ValueError('QnapAssistantの処理が45分以内に完了しませんでした')

    # ---- schedule ----
    def tick(self):
        """Run anything that is due. Called every minute by the background thread (and by tests)."""
        config = self.settings()
        if not config['enabled']:
            return []
        moment = self.clock()
        if not due(moment, config['time']):
            return []
        day = target_day(moment, config['time'])
        done = []
        for user in self.store.users():
            row = self._get(user['id'], day.isoformat())
            if row and row['status'] not in ('retry',):
                continue
            if row and time.monotonic() < self._retry_at.get((user['id'], day.isoformat()), 0):
                continue
            try:
                done.append(self.run(user['id'], day, 'schedule'))
            except ValueError:
                break  # a manual run is in progress; try again on the next tick
        return done

    def _loop(self):
        while not self._stop.wait(60):
            try:
                self.tick()
            except Exception:
                pass  # storage protection or transient errors: retried next minute

    def start_scheduler(self):
        self._thread = threading.Thread(target=self._loop, name='daily-advice', daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
