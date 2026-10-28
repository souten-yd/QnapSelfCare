"""Bounded, user-initiated health coaching through configured OpenAI-compatible APIs."""
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

import health_metrics

DEFAULT = {'provider': 'controldeck', 'base_url': '', 'model': 'auto'}
PROVIDERS = ('controldeck', 'qnapassistant', 'local', 'openrouter')
MODES = {'health': '健康記録の整理', 'meal': '食事の振り返り',
         'diet': '無理のない目標検討', 'review': '計画の見直し', 'lifestyle': '運動・睡眠の振り返り',
         'daily': '1日の振り返りと翌日のアドバイス'}
# What each theme should focus on; appended to the shared system prompt.
MODE_GUIDE = {
    'health': '体重・血圧・睡眠・活動の変化を整理し、良い点と気になる点を分けて示してください。',
    'meal': '食事メモと摂取量から、続けやすい改善を具体的な料理や量の例で示してください。',
    'diet': '目標体重と期日が無理のない範囲か、減量ペースと安全な下限に照らして検討してください。',
    'review': '計画線・試算・週次実績の差を数値で示し、目標日の延長・摂取・生活活動・運動の調整候補とトレードオフを比較してください。',
    'lifestyle': '歩数・運動時間・睡眠時間と規則性・安静時心拍の目安から、日常に取り入れやすい工夫を示してください。',
    'daily': 'context.day_reviewは対象日の記録と直前7日平均の比較です。対象日の良かった点と気になる点を週の傾向と比べて評価し、'
             '翌日に試せる生活・食事・運動・睡眠・減量の行動を最大3つ、具体的に提案してください。記録がない項目は推測しないでください。',
}
SYSTEM_PROMPT = (
    'あなたは個人の健康記録・食事・減量計画の振り返りを支援するアシスタントです。'
    '診断、治療、薬の変更、断定的なカロリー処方はしません。気になる症状や続く異常値は医療機関への相談を勧めます。'
    'contextの数値だけを根拠にし、ない数値を作りません。測定値・推定値・いつもの設定による仮定を区別します。'
    'evaluationsは公的ガイドライン等の目安と比べた判定で、status=unknownはデータ不足です。'
    '無理な減量やBMI下限を下回る目標は勧めません。'
    '回答は日本語で、次の見出しを使った簡潔なMarkdownにしてください：'
    '「## 要点」（2〜3行）、「## 根拠になったデータ」（箇条書き、数値つき）、「## 提案」（すぐ試せる行動を最大3つ）、「## 注意」（必要な場合のみ）。'
)
MAX_HISTORY = 3
ASSISTANT_DEADLINE_SECONDS = 120 * 60  # long enough for QnapAssistant model load + generation up to 3600 s
INTERACTIVE_WAIT_SECONDS = 120 * 60    # a request from the screen gives up after waiting this long for the AI slot
STALL_SECONDS = 180                    # no successful QnapAssistant status for this long is shown as a warning


def clean_history(history):
    """Previous turns for a follow-up question: at most 3, each trimmed."""
    if history is None:
        return []
    if not isinstance(history, list) or len(history) > MAX_HISTORY:
        raise ValueError('前の相談は3件までです')
    turns = []
    for turn in history:
        if not isinstance(turn, dict) or set(turn) != {'question', 'answer'} or \
                not all(isinstance(turn[k], str) for k in turn):
            raise ValueError('前の相談の形式が不正です')
        turns.append({'question': turn['question'].strip()[:2000], 'answer': turn['answer'].strip()[:4000]})
    return turns


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def validate(settings):
    if not isinstance(settings, dict) or set(settings) != set(DEFAULT):
        raise ValueError('AI設定の形式が不正です')
    provider, base, model = (settings[key] for key in ('provider', 'base_url', 'model'))
    if provider not in PROVIDERS or not isinstance(base, str) or not isinstance(model, str):
        raise ValueError('AI接続先が不正です')
    if not model or len(model) > 150 or not re.fullmatch(r'[A-Za-z0-9_./:+-]+', model):
        raise ValueError('モデル名が不正です')
    if provider == 'openrouter':
        base = 'https://openrouter.ai/api/v1'
    elif provider == 'qnapassistant' and not base:
        base = 'http://127.0.0.1:11435/v1'
    if provider != 'openrouter' and base:
        url = urlsplit(base)
        host = url.hostname or ''
        try:
            private = ipaddress.ip_address(host).is_private or ipaddress.ip_address(host) in ipaddress.ip_network('100.64.0.0/10')
        except ValueError:
            private = host in ('localhost',) or host.endswith(('.local', '.ts.net'))
        if (url.scheme not in ('http', 'https') or not private or not url.port
                or url.username or url.password or url.query or url.fragment
                or not url.path.rstrip('/').endswith('/v1')):
            raise ValueError('ローカル/TailscaleのOpenAI互換URL（末尾 /v1）を指定してください')
        base = base.rstrip('/')
    if len(base) > 300:
        raise ValueError('接続先URLが長すぎます')
    return {'provider': provider, 'base_url': base, 'model': model}


class Coach:
    def __init__(self, root, store):
        self.root = Path(root) / 'ai'
        self.root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.store = store
        self._limit = threading.BoundedSemaphore(2)
        self._serial = threading.Lock()   # one long AI run at a time (jobs, nightly advice)
        self._sleep = time.sleep
        self._state = threading.Lock()    # guards _current / _interactive_waiting
        self._current = None              # what holds the AI slot right now (shown on the AI tab)
        self._interactive_waiting = 0     # screen requests waiting; nightly work yields to them

    def settings(self):
        with self.store.connect() as db:
            row = db.execute('SELECT config FROM ai_settings WHERE id=1').fetchone()
        return validate(json.loads(row[0]) if row else dict(DEFAULT))

    def status(self):
        settings = self.settings()
        return dict(settings, key_configured=self._key_path(settings['provider']).exists())

    def save(self, payload):
        settings = validate(payload)
        with self.store.connect() as db:
            db.execute('INSERT INTO ai_settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config',
                       (json.dumps(settings),))
        return self.status()

    def _key_path(self, provider):
        if provider not in PROVIDERS:
            raise ValueError('AI接続先が不正です')
        return self.root / (provider + '.key')

    def set_key(self, provider, key):
        path = self._key_path(provider)
        if not isinstance(key, str) or len(key) > 300 or '\n' in key or '\r' in key:
            raise ValueError('APIキーが不正です')
        if not key:
            path.unlink(missing_ok=True)
        else:
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    os.chmod(temporary, 0o600)
                    stream.write(key.encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
        return {'provider': provider, 'key_configured': path.exists()}

    def _consult_payload(self, user_id, mode, question, consent=None, history=None, extra=None):
        if mode not in MODES or not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
            raise ValueError('相談内容または種類が不正です')
        config = self.settings()
        if not config['base_url']:
            raise ValueError('ControlDeckのNASから到達できる接続先を設定してください')
        summary = self.store.wellness_summary(user_id)
        profile = summary.pop('profile')
        recent_meals = [{'created_at': item['created_at'], 'note': item['note'][:300], 'calories': item['calories']}
                        for item in self.store.meals(user_id, 5)]
        context = {'measurements': {key: summary[key] for key in ('latest_weight', 'latest_pressure', 'bmi',
                   'estimated_bmr_kcal', 'weight_change_7d_kg', 'weekly_change_needed_kg', 'notices')},
                   'goal': {'weight_kg': profile['goal_weight_kg'], 'date': profile['goal_date']},
                   'saved_plan': profile['plan_text'][:3000], 'recent_meals': recent_meals}
        activity = self.store.energy_report(user_id)
        context['energy'] = activity['current']
        context['recent_activity'] = activity['entries'][-7:]
        context['usual_activity'] = activity['routine']
        context['activity_source_note'] = 'source=routineはいつもの設定による仮定、enteredはその日の入力。仮定を実績と断定しない。'
        context['weekly_review'] = [w for w in activity['weeks'] if not w['future']][-4:]
        context['energy_notices'] = activity['notices']
        context['weight_plan'] = ({k: v for k, v in activity['active_plan'].items() if k not in ('id', 'created_at')} if activity['active_plan'] else None)
        context['plan_analysis'] = activity.get('plan_analysis')
        context['evaluations'] = self._evaluations(user_id)
        if extra:
            context.update(extra)
        turns = clean_history(history)
        messages = [{'role': 'system', 'content': SYSTEM_PROMPT + MODE_GUIDE[mode]}]
        for turn in turns:
            messages += [{'role': 'user', 'content': turn['question']}, {'role': 'assistant', 'content': turn['answer']}]
        messages.append({'role': 'user', 'content': json.dumps({'topic': MODES[mode], 'context': context,
                         'question': question.strip()}, ensure_ascii=False)})
        prompt = {'model': config['model'], 'stream': False,
                  'max_tokens': 1400 if mode in ('diet', 'review') else 900, 'messages': messages}
        return config, prompt

    def _evaluations(self, user_id):
        """Compact indicator list for the prompt; a failure here must not block the consultation."""
        try:
            result = health_metrics.compute(self.store, user_id)
        except Exception:
            return None
        return [{'group': group, 'label': m['label'], 'value': m['value'], 'status': m['status'], 'basis': m['reference']}
                for group in ('diet', 'health') for m in result[group]]

    def consult(self, user_id, mode, question, consent=None, history=None, extra=None):
        config, prompt = self._consult_payload(user_id, mode, question, consent, history, extra)
        return self.run_prompt(config, prompt)

    def _assistant_job_url(self, config, job_id=''):
        if config['provider'] != 'qnapassistant':
            raise ValueError('QnapAssistantを相談先に選択してください')
        base = config['base_url'].rstrip('/')
        if not base.endswith('/v1'):
            raise ValueError('QnapAssistantの接続先は /v1 で終わる必要があります')
        prefix = base[:-3] + '/api/llm/jobs'
        if job_id:
            if not re.fullmatch(r'req_[0-9a-f]{48}', job_id):
                raise ValueError('受付IDが不正です')
            prefix += '/' + job_id
        return prefix

    def _assistant_job_request(self, url, payload=None, method='GET'):
        headers = {'Content-Type': 'application/json'}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode()
        else:
            body = None
        if self._key_path('qnapassistant').exists():
            headers['Authorization'] = 'Bearer ' + self._key_path('qnapassistant').read_text().strip()
        request = Request(url, data=body, headers=headers, method=method)
        opener = build_opener(NoRedirect(), ProxyHandler({}))
        try:
            with opener.open(request, timeout=15) as response:
                if response.status not in (200, 202):
                    raise ValueError(f'QnapAssistant HTTP {response.status}')
                raw = response.read(256 * 1024 + 1)
                if len(raw) > 256 * 1024:
                    raise ValueError('AIジョブの応答が大きすぎます')
                return json.loads(raw)
        except HTTPError as error:
            if error.code == 404:
                raise ValueError('受付IDが見つかりません。サービスが再起動したか、保存期間を超えました') from error
            if error.code == 409:
                raise ValueError('QnapAssistantでローカルLLMを選択してください') from error
            if error.code == 429:
                raise ValueError('ローカルLLMが混雑しています。しばらくしてから再試行してください') from error
            raise ValueError(f'QnapAssistant ジョブAPI HTTP {error.code}') from error
        except (TimeoutError, socket.timeout, URLError, OSError) as error:
            raise ValueError('QnapAssistantと通信できません。受付IDを保持して再確認できます') from error
        except (TypeError, KeyError, json.JSONDecodeError) as error:
            raise ValueError('QnapAssistantから不正なジョブ応答を受信しました') from error

    def start_consult(self, user_id, mode, question, history=None, extra=None):
        config, prompt = self._consult_payload(user_id, mode, question, history=history, extra=extra)
        endpoint = self._assistant_job_url(config)
        if config['model'] == 'auto':
            prompt.pop('model', None)
        result = self._assistant_job_request(endpoint, prompt, 'POST')
        request_id = result.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'req_[0-9a-f]{48}', request_id):
            raise ValueError('QnapAssistantから有効な受付IDが返りませんでした')
        return {'request_id': request_id, 'status': 'queued'}

    def consult_job_status(self, request_id):
        config = self.settings()
        endpoint = self._assistant_job_url(config, request_id)
        result = self._assistant_job_request(endpoint)
        if result.get('request_id') != request_id:
            raise ValueError('受付IDが一致しません')
        return result

    def consult_job_result(self, request_id):
        config = self.settings()
        endpoint = self._assistant_job_url(config, request_id)
        data = self._assistant_job_request(endpoint + '/result')
        try:
            answer = data['choices'][0]['message']['content']
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError('AIの回答形式が不正です') from error
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError('AIの回答が空です')
        return {'answer': answer[:10000], 'provider': 'qnapassistant', 'model': config['model']}

    def job_diagnostics(self, request_id, include_output=False):
        """Read a local job's short-lived private evidence; never log its raw text."""
        config = self.settings()
        url = self._assistant_job_url(config, request_id) + '/diagnostics'
        if include_output:
            url += '?include_output=1'
        return self._assistant_job_request(url)

    def cancel_consult_job(self, request_id):
        config = self.settings()
        return self._assistant_job_request(self._assistant_job_url(config, request_id), None, 'DELETE')

    def estimate_meal(self, payload):
        return self.run_prompt(*self._meal_payload(payload))

    def _meal_payload(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'date', 'breakfast', 'lunch', 'dinner', 'snacks'}:
            raise ValueError('食事試算の項目が不正です')
        cleaned = {}
        for key in ('date', 'breakfast', 'lunch', 'dinner', 'snacks'):
            value = payload.get(key)
            if not isinstance(value, str):
                raise ValueError('食事内容は文字列で指定してください')
            value = value.strip()
            if len(value) > (10 if key == 'date' else 1200):
                raise ValueError('食事内容が長すぎます')
            cleaned[key] = value
        if cleaned['date'] and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', cleaned['date']):
            raise ValueError('日付はYYYY-MM-DDで指定してください')
        if not any(cleaned[key] for key in ('breakfast', 'lunch', 'dinner', 'snacks')):
            raise ValueError('朝食・昼食・夕食・間食のいずれかを入力してください')
        config = self.settings()
        if not config['base_url']:
            raise ValueError('AI接続先を設定してください')
        prompt = {'model': config['model'], 'stream': False, 'max_tokens': 1400,
                  'messages': [{'role': 'system', 'content':
                      'あなたは食事内容から1日摂取カロリーを概算する補助者です。医療診断や断定的な栄養処方はしません。'
                      '量が不明なら一般的な一人前を仮定し、幅が大きい場合は前提を明示してください。'
                      '接続先にWeb検索機能が実際にある場合だけ、必要に応じてメーカー・飲食店・公的栄養情報を参照してください。'
                      'Webを使っていないのに使ったと主張しないでください。日本語の読みやすい文章で答え、JSONは出力しないでください。'
                      '入力された食事ごとに各品目の量と推定kcalを列挙し、食事ごとの小計と1日の合計kcalを最後に示してください。'
                      '量が不明な品目の仮定と、推定値の不確かさを簡潔に示してください。食べていない食事は追加しないでください。'},
                    {'role': 'user', 'content': json.dumps({'task':'品目別、食事別、1日合計のカロリーを文章で概算',
                       'input': cleaned}, ensure_ascii=False)}]}
        return config, prompt

    def _test_payload(self):
        config = self.settings()
        return config, {'model': config['model'], 'stream': False, 'max_tokens': 16,
                        'messages': [{'role': 'user', 'content': 'Reply with OK.'}]}

    def test_connection(self):
        return self._complete(*self._test_payload())

    def ask(self, user_id, mode, question, history=None, extra=None, progress=None, cancelled=None, poll=3,
            label='AI相談', interactive=True):
        """Consultation without an HTTP time limit for QnapAssistant (used by background jobs)."""
        config, prompt = self._consult_payload(user_id, mode, question, history=history, extra=extra)
        return self.run_prompt(config, prompt, progress, cancelled, poll, label=label, interactive=interactive)

    def interactive_waiting(self):
        with self._state:
            return self._interactive_waiting

    def activity(self):
        """What is using the AI right now, how long, and when QnapAssistant last answered."""
        now = time.monotonic()
        with self._state:
            current = dict(self._current) if self._current else None
            waiting = self._interactive_waiting
        if current:
            current.pop('cancel_event', None)
            current['elapsed_seconds'] = int(now - current.pop('started'))
            current['since_update_seconds'] = int(now - current.pop('updated'))
            current['stalled'] = current['provider'] == 'qnapassistant' and current['since_update_seconds'] >= STALL_SECONDS
        return {'busy': current is not None, 'current': current, 'waiting_interactive': waiting,
                'deadline_seconds': ASSISTANT_DEADLINE_SECONDS, 'stall_after_seconds': STALL_SECONDS}

    def cancel_current(self):
        """Stop whatever holds the AI slot (QnapAssistant jobs are cancelled on the NAS too)."""
        with self._state:
            if not self._current:
                raise ValueError('実行中のAI処理はありません')
            self._current['cancel_event'].set()
            self._current['stage'] = 'cancelling'
        return self.activity()

    def run_prompt(self, config, prompt, progress=None, cancelled=None, poll=3, label='AI処理', interactive=True):
        """Run one prompt to completion. QnapAssistant uses its async job API, so model loading and long
        generation are not cut off by the 300 s HTTP timeout; other providers use the synchronous API.
        Calls are serialised so the NAS model is never shared; nightly work (interactive=False) waits while
        a request from the screen is waiting, and screen requests give up after INTERACTIVE_WAIT_SECONDS."""
        waited = time.monotonic()
        if interactive:
            with self._state:
                self._interactive_waiting += 1
        try:
            while True:
                if (interactive or not self.interactive_waiting()) and self._serial.acquire(timeout=5):
                    break
                if not interactive:
                    self._sleep(5)
                if cancelled and cancelled():
                    raise ValueError('キャンセルしました')
                if interactive and time.monotonic() - waited > INTERACTIVE_WAIT_SECONDS:
                    holder = (self.activity()['current'] or {}).get('label', '前の処理')
                    raise ValueError(f'{holder}が{INTERACTIVE_WAIT_SECONDS // 60}分以上終わらないため、この依頼を取り消しました。'
                                     'AIタブの「AIの稼働状況」で確認・中止できます')
                if progress:
                    progress({'status': 'waiting', 'elapsed_seconds': int(time.monotonic() - waited),
                              'blocking': self.activity()['current']})
        finally:
            if interactive:
                with self._state:
                    self._interactive_waiting -= 1
        cancel_event = threading.Event()
        now = time.monotonic()
        with self._state:
            self._current = {'label': label, 'interactive': interactive, 'provider': config['provider'], 'stage': 'queued',
                             'detail': None, 'started': now, 'updated': now, 'errors': 0, 'cancel_event': cancel_event}
        stop = lambda: cancel_event.is_set() or bool(cancelled and cancelled())
        def track(state):
            with self._state:
                if self._current:
                    self._current['stage'] = state.get('status') or self._current['stage']
                    self._current['updated'] = time.monotonic()
                    self._current['errors'] = 0
                    if state.get('memory_available_bytes'):
                        self._current['detail'] = f"空きRAM {state['memory_available_bytes'] / 1073741824:.1f} GiB"
            if progress:
                progress(state)
        try:
            if config['provider'] != 'qnapassistant':
                track({'status': 'generating'})
                return self._complete(config, prompt)
            return self._run_assistant_job(config, prompt, track, stop, poll)
        finally:
            with self._state:
                self._current = None
            self._serial.release()

    def _note_error(self):
        with self._state:
            if self._current:
                self._current['errors'] += 1

    def _run_assistant_job(self, config, prompt, progress, cancelled, poll):
        if config['model'] == 'auto':
            prompt.pop('model', None)
        accepted = self._assistant_job_request(self._assistant_job_url(config), prompt, 'POST')
        request_id = accepted.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'req_[0-9a-f]{48}', request_id):
            raise ValueError('QnapAssistantから有効な受付IDが返りませんでした')
        url = self._assistant_job_url(config, request_id)
        deadline = time.monotonic() + ASSISTANT_DEADLINE_SECONDS
        failures = 0
        while True:
            if cancelled and cancelled():
                self._cancel_quietly(url)
                raise ValueError('キャンセルしました')
            if time.monotonic() > deadline:
                self._cancel_quietly(url)
                raise ValueError(f'QnapAssistantの処理が{ASSISTANT_DEADLINE_SECONDS // 60}分以内に完了しませんでした。モデル設定とNASの空きメモリを確認してください')
            self._sleep(poll)
            try:
                state = self._assistant_job_request(url)
                failures = 0
            except ValueError as error:
                if '見つかりません' in str(error):
                    raise  # restarted: never resubmit health data automatically
                failures += 1
                self._note_error()
                if failures >= 10:
                    raise
                continue
            if progress:
                progress(state)
            status = state.get('status')
            if status == 'completed':
                data = self._assistant_job_request(url + '/result')
                try:
                    answer = data['choices'][0]['message']['content']
                except (KeyError, IndexError, TypeError) as error:
                    raise ValueError('AIの回答形式が不正です') from error
                if not isinstance(answer, str) or not answer.strip():
                    raise ValueError('AIの回答が空です')
                return {'answer': answer[:10000], 'provider': 'qnapassistant', 'model': config['model']}
            if status in ('failed', 'timed_out', 'cancelled'):
                names = {'failed': '処理に失敗しました', 'timed_out': 'モデルの読込または生成が時間切れになりました', 'cancelled': 'キャンセルされました'}
                raise ValueError('QnapAssistant: ' + names[status] + (f"（{state['error']}）" if state.get('error') else ''))

    def _cancel_quietly(self, url):
        try:
            self._assistant_job_request(url, None, 'DELETE')
        except ValueError:
            pass

    def _complete(self, config, prompt):
        if not config['base_url']:
            raise ValueError('NASから到達できるAI接続先URLを保存してください')
        if config['provider'] == 'qnapassistant' and config['model'] == 'auto':
            # QnapAssistant owns provider/model selection; do not override it with a literal "auto".
            prompt.pop('model', None)
        key_path = self._key_path(config['provider'])
        headers = {'Content-Type': 'application/json'}
        if key_path.exists():
            headers['Authorization'] = 'Bearer ' + key_path.read_text().strip()
        elif config['provider'] == 'openrouter':
            raise ValueError('OpenRouterのAPIキーを設定してください')
        request = Request(config['base_url'] + '/chat/completions', data=json.dumps(prompt).encode(), headers=headers)
        opener = build_opener(NoRedirect(), ProxyHandler({}) if config['provider'] != 'openrouter' else ProxyHandler())
        if not self._limit.acquire(blocking=False):
            raise ValueError('AI相談が実行中です。完了してから再試行してください')
        try:
            with opener.open(request, timeout=300) as response:
                raw = response.read(128 * 1024 + 1)
            if len(raw) > 128 * 1024:
                raise ValueError('AIの応答が大きすぎます')
            answer = json.loads(raw)['choices'][0]['message']['content']
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError('AIの回答が空です')
            return {'answer': answer[:10000], 'provider': config['provider'], 'model': config['model']}
        except HTTPError as error:
            hints = {400: 'モデル名とリクエスト対応を確認してください', 401: 'APIキーを確認してください',
                     403: 'APIキーの権限を確認してください', 404: 'URLの /v1 とモデル名を確認してください',
                     429: '利用上限または混雑です。時間を置いて再試行してください',
                     502: 'QnapAssistant側のLLM接続・モデル起動状態を確認してください',
                     503: 'モデルの準備またはサービス起動を待ってください'}
            raise ValueError(f"AI HTTP {error.code}: {hints.get(error.code, '接続先サービスの稼働状態を確認してください')}") from error
        except (TimeoutError, socket.timeout) as error:
            raise ValueError('AI応答が300秒以内に完了しませんでした。モデルの起動状態・処理速度を確認してください') from error
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                message = 'AI接続がタイムアウトしました。NASからの到達性とモデル起動状態を確認してください'
            elif isinstance(error.reason, ConnectionRefusedError):
                message = 'AI接続が拒否されました。QnapAssistantの起動と待受ポートを確認してください'
            elif isinstance(error.reason, socket.gaierror):
                message = 'AI接続先の名前を解決できません。ホスト名とDNSを確認してください'
            else:
                message = 'AIへの通信に失敗しました。NASからの到達性・TLS設定を確認してください'
            raise ValueError(message) from error
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise ValueError('AIの応答がOpenAI互換の回答形式ではありません。接続先URLを確認してください') from error
        except OSError as error:
            raise ValueError('AIへの通信が切断されました。接続先のログとモデル起動状態を確認してください') from error
        finally:
            self._limit.release()
