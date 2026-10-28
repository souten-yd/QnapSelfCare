"""Privacy-preserving diagnostics for local AI advice jobs.

Only allowlisted numeric/status data is persisted; prompts, model output,
tokens, and bearer-capability job IDs are never included in the summary.
"""
import re

SAFE_FIELDS = (
    'status','error_code','elapsed_seconds','generation_seconds',
    'first_token_ms','since_last_token_seconds','token_chunks_observed',
    'prompt_tokens','completion_tokens_reported','output_bytes',
    'partial_truncated','finish_reason','context_size','input_bytes',
    'raw_output_available','raw_output_persisted','diagnostic_version',
)
VALID_STATUS = {'queued','loading_model','ready','generating','completed','failed','timed_out','cancelled'}
ERROR_CODES = {
    'generation_timeout_partial': ('生成時間切れ（途中出力あり）', '生成速度・モデルサイズ・出力上限を確認してください。'),
    'generation_first_token_timeout': ('最初の生成トークンが出る前に時間切れ', '入力サイズとCPU・RAM負荷を確認してください。'),
    'load_timeout': ('モデル読み込み時間切れ', 'モデルファイル、空きRAMとストレージ速度を確認してください。'),
    'invalid_backend_response': ('バックエンドの応答形式が不正', 'llama.cppの互換APIと出力形式を確認してください。'),
    'response_read_error': ('バックエンドの応答が途中で終了', 'llama.cppの稼働状態と通信を確認してください。'),
    'backend_unreachable': ('バックエンド通信失敗', 'llama.cppの起動状態と接続を確認してください。'),
    'generation_timeout': ('回答生成時間切れ', '生成時間の上限とCPU負荷を確認してください。'),
}

def sanitize_diagnostic(value):
    if not isinstance(value, dict):
        return {}
    data = {key:value[key] for key in SAFE_FIELDS if key in value}
    for k in ('status','error_code','finish_reason'):
        if k in data:
            if not isinstance(data[k],str) or len(data[k])>80: data.pop(k,None)
    for k in SAFE_FIELDS:
        if k not in ('status','error_code','finish_reason','partial_truncated','raw_output_available','raw_output_persisted'):
            if k in data and (type(data[k]) is not int or not -1 <= data[k] <= 1000000000):
                data.pop(k,None)
    for k in ('partial_truncated','raw_output_available','raw_output_persisted'):
        if k in data and type(data[k]) is not bool: data.pop(k,None)
    code=data.get('error_code','')
    if code:
        title,remedy=ERROR_CODES.get(code,('AI処理に失敗しました','診断データを確認してください。'))
        data['diagnosis']=title
        data['recommendation']=remedy
    return data

def validate_advice_format(answer):
    """Advisory format warning, never turn valid free text into a failed job."""
    if not isinstance(answer,str) or not answer.strip():
        return ['回答が空です']
    headings={
        '要点':r'(?m)^\s*(?:#{1,4}\s*|\*\*)?要点',
        '根拠になったデータ':r'(?m)^\s*(?:#{1,4}\s*|\*\*)?根拠(?:になったデータ|となるデータ)?',
        '提案':r'(?m)^\s*(?:#{1,4}\s*|\*\*)?提案',
        '注意':r'(?m)^\s*(?:#{1,4}\s*|\*\*)?注意',
    }
    return [f'「{title}」の見出しが見つかりません' for title,pattern in headings.items()
            if not re.search(pattern,answer)]

def diagnose_failure(message, last=None):
    """Error fallback when the QnapAssistant job diagnostic is unavailable."""
    last=last if isinstance(last,dict) else {}
    data=sanitize_diagnostic(last)
    if not data.get('error_code'):
        text=str(message).lower()
        if 'generation timed out' in text or '生成が時間切れ' in text:
            data['error_code']='generation_timeout'
        elif '読込' in text and '時間切れ' in text:
            data['error_code']='load_timeout'
        elif '404' in text or '受付IDが見つかりません' in text:
            data['error_code']='job_lost'
        else:
            data['error_code']='unknown'
    title,remedy=ERROR_CODES.get(data['error_code'],('原因を確定できません','診断記録とQnapAssistantの状態を確認してください。'))
    data['diagnosis']=title;data['recommendation']=remedy
    return data
