"""Bounded, user-initiated health coaching through configured OpenAI-compatible APIs."""
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

DEFAULT = {'provider': 'controldeck', 'base_url': '', 'model': 'auto'}
PROVIDERS = ('controldeck', 'qnapassistant', 'local', 'openrouter')
MODES = {'health': '健康記録の整理', 'meal': '食事の振り返り',
         'diet': '無理のない目標検討', 'review': '計画の見直し'}


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

    def consult(self, user_id, mode, question, consent=None):
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
        prompt = {'model': config['model'], 'stream': False, 'max_tokens': 600,
                  'messages': [{'role': 'system', 'content':
                      'あなたは健康記録・食事・減量計画の整理を支援します。診断、治療、薬の変更や断定的なカロリー処方はしません。'
                      '測定値と推定値を区別し、個人目標は本人の確認後にだけ適用します。気になる症状や継続する異常値は医療者への相談を促します。'
                      '無理な減量やBMI下限を下回る目標を勧めません。簡潔な日本語で答えてください。'},
                    {'role': 'user', 'content': json.dumps({'topic': MODES[mode], 'context': context,
                       'question': question.strip()}, ensure_ascii=False)}]}
        return self._complete(config, prompt)

    def test_connection(self):
        config = self.settings()
        return self._complete(config, {'model': config['model'], 'stream': False, 'max_tokens': 16,
                              'messages': [{'role': 'user', 'content': 'Reply with OK.'}]})

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
