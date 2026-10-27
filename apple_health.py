"""Versioned, sample-only Apple Health receiver. No HealthKit/network side effects."""
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time

MAX_SAMPLES = 50000

METRICS = {
    'steps': ('count', 0, 1000000),
    'walking_running_distance': ('m', 0, 1000000),
    'active_energy': ('kcal', 0, 100000),
    'heart_rate': ('bpm', 1, 400),
    'oxygen_saturation': ('%', 0, 100),
    'sleep': (None, None, None),
}
STAGES = {'awake', 'in_bed', 'asleep_unspecified', 'core', 'deep', 'rem'}
SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS apple_health_samples(
    user_id TEXT NOT NULL REFERENCES users(id),
    sample_key TEXT NOT NULL, metric TEXT NOT NULL,
    start_at TEXT NOT NULL, end_at TEXT NOT NULL,
    payload TEXT NOT NULL, received_at TEXT NOT NULL,
    PRIMARY KEY(user_id, sample_key));
CREATE INDEX IF NOT EXISTS apple_health_history
    ON apple_health_samples(user_id, start_at, sample_key);
'''


def string(value, name, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(name + 'が不正です')
    return value.strip()


def instant(value):
    value = string(value, '日時', 64)
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None or not 2000 <= date.year <= 2100:
            raise ValueError()
        return date.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except ValueError:
        raise ValueError('日時はタイムゾーン付きISO 8601で指定してください') from None


def normalize(sample):
    required = {'metric', 'start_at', 'end_at', 'value', 'unit', 'source'}
    if not isinstance(sample, dict) or not required <= set(sample) or set(sample) - required - {'source_id', 'device'}:
        raise ValueError('サンプルの項目が不正です')
    metric = sample['metric']
    if not isinstance(metric, str) or metric not in METRICS:
        raise ValueError('許可されていない項目です（体重・BMI・体組成は対象外）')
    result = {k: sample[k] for k in required}
    result['source'] = string(sample['source'], '記録元')
    for field in ('source_id', 'device'):
        if field in sample:
            result[field] = string(sample[field], field)
    result['start_at'], result['end_at'] = instant(sample['start_at']), instant(sample['end_at'])
    if result['end_at'] < result['start_at']:
        raise ValueError('終了日時が開始日時より前です')
    value, unit = sample['value'], sample['unit']
    if metric == 'sleep':
        if unit is not None or not isinstance(value, str) or value not in STAGES or result['end_at'] == result['start_at']:
            raise ValueError('睡眠は有効な区分と開始・終了日時、unit:nullが必要です')
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('有限の数値が必要です')
        value = float(value)
        # Conversions are explicit; never guess percent from the magnitude.
        conversions = {('walking_running_distance', 'km'): 1000,
                       ('active_energy', 'kJ'): 1 / 4.184,
                       ('oxygen_saturation', 'fraction'): 100}
        if isinstance(unit, str) and (metric, unit) in conversions:
            value *= conversions[(metric, unit)]
            unit = METRICS[metric][0]
        canonical, low, high = METRICS[metric]
        if unit != canonical or not low <= value <= high or (metric == 'steps' and not value.is_integer()):
            raise ValueError('単位または値が不正です')
        result['value'], result['unit'] = round(value, 8), unit
    return result


def encoded(sample):
    return json.dumps(sample, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def insert(db, user_id, sample, received_at):
    payload = encoded(sample)
    identity = ({k: sample[k] for k in ('metric', 'source', 'source_id')}
                if 'source_id' in sample else sample)
    key = hashlib.sha256(encoded(identity).encode()).hexdigest()
    old = db.execute('SELECT payload FROM apple_health_samples WHERE user_id=? AND sample_key=?', (user_id, key)).fetchone()
    if old:
        if old[0] != payload:
            raise ValueError('同じ元IDの内容が変わっています。自動上書きせず再照合してください')
        return 0
    db.execute('INSERT INTO apple_health_samples VALUES(?,?,?,?,?,?,?)',
               (user_id, key, sample['metric'], sample['start_at'], sample['end_at'], payload, received_at))
    return 1


# Explicit aliases only: unknown units/categories must never be guessed.
TYPE_NAMES = {'歩数': 'steps', 'ウォーキング＋ランニングの距離': 'walking_running_distance',
              'アクティブエネルギー': 'active_energy', '心拍数': 'heart_rate',
              '取り込まれた酸素のレベル': 'oxygen_saturation', '睡眠': 'sleep'}
UNIT_NAMES = {'歩': 'count', '拍/分': 'bpm', '拍／分': 'bpm',
              'パーセント': '%', 'キロカロリー': 'kcal'}
SLEEP_NAMES = {'覚醒': 'awake', 'ベッドにいた時間': 'in_bed', '睡眠': 'asleep_unspecified',
               'コア': 'core', '深い': 'deep', 'レム': 'rem'}


def text_records(value, name, maximum):
    """Decode only known container positions; bounded JSON/JSON-lines, never eval."""
    for _ in range(4):
        if not isinstance(value, str):
            break
        text = value.strip()
        if not text:
            raise ValueError(name + 'が空文字です。データなしの場合は空配列を送ってください')
        try:
            value = json.loads(text)
            continue
        except (ValueError, RecursionError):
            pass
        # Shortcuts may join serialized dictionaries using real or literal newlines.
        decoder = json.JSONDecoder()
        rows = []
        while text:
            try:
                row, end = decoder.raw_decode(text)
            except (ValueError, RecursionError):
                raise ValueError(name + 'はJSON辞書またはJSON辞書の行一覧で送ってください') from None
            if not isinstance(row, dict):
                raise ValueError(name + 'に日時・項目情報がありません。数値だけでは復元できません')
            rows.append(row)
            if len(rows) > maximum:
                raise ValueError(name + 'の件数上限を超えています')
            rest = text[end:]
            if rest and not rest[0].isspace() and not rest.startswith('\\n'):
                raise ValueError(name + 'のレコード間には改行が必要です')
            text = rest.lstrip()
            if text.startswith('\\n'):
                text = text[2:].lstrip()
        value = rows
        break
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(name + 'に日時・項目情報がありません。数値だけでは復元できません')
    if len(value) > maximum:
        raise ValueError(name + 'の件数上限を超えています')
    result = []
    for row in value:
        for _ in range(4):
            if not isinstance(row, str):
                break
            try:
                row = json.loads(row)
            except (ValueError, RecursionError):
                raise ValueError(name + 'の各要素はJSON辞書で送ってください') from None
        if not isinstance(row, dict):
            raise ValueError(name + 'の各要素には辞書が必要です。数値だけでは日時を復元できません')
        result.append(row)
    return result


def shortcuts_body(body):
    if set(body) != {'schema', 'user_id', 'groups'}:
        raise ValueError('schema:2, user_id, groupsを指定してください')
    groups = []
    total = 0
    for group in text_records(body['groups'], 'groups', 6):
        group = dict(group)
        if 'samples' not in group:
            raise ValueError('groupにはtype, source, unit, samplesが必要です')
        group['samples'] = text_records(group['samples'], 'samples', MAX_SAMPLES)
        total += len(group['samples'])
        if total > MAX_SAMPLES:
            raise ValueError('1リクエストは最大50000サンプルです')
        groups.append(group)
    return dict(body, groups=groups)


def unpack_groups(body):
    if set(body) != {'schema', 'user_id', 'groups'} or not isinstance(body['groups'], list):
        raise ValueError('schema:2, user_id, groupsを指定してください')
    if len(body['groups']) > 6:
        raise ValueError('groupsは最大6項目です')
    total = 0
    seen = set()
    for group in body['groups']:
        if (not isinstance(group, dict) or set(group) != {'type', 'source', 'unit', 'samples'}
                or not isinstance(group['samples'], list)):
            raise ValueError('groupにはtype, source, unit, samplesが必要です')
        name = string(group['type'], 'type')
        metric = TYPE_NAMES.get(name, name)
        if metric not in METRICS or metric in seen:
            raise ValueError('対象外または重複したtypeです')
        seen.add(metric)
        string(group['source'], 'source')
        unit = group['unit']
        if not isinstance(unit, str):
            raise ValueError('groupのunitは文字列です（睡眠は空文字）')
        unit = UNIT_NAMES.get(unit, unit)
        allowed = {METRICS[metric][0]}
        allowed.update({'walking_running_distance': {'km'}, 'active_energy': {'kJ'},
                        'oxygen_saturation': {'fraction'}, 'sleep': {''}}.get(metric, set()))
        if unit not in allowed:
            raise ValueError('groupの単位が不正です')
        total += len(group['samples'])
        if total > MAX_SAMPLES:
            raise ValueError('1リクエストは最大50000サンプルです。期間を分けて送信してください')
    rows = []
    for gi, group in enumerate(body['groups'], 1):
        metric = TYPE_NAMES.get(group['type'].strip(), group['type'].strip())
        for si, row in enumerate(group['samples'], 1):
            try:
                if (not isinstance(row, dict) or not {'start', 'end', 'value'} <= set(row)
                        or set(row) - {'start', 'end', 'value', 'unit', 'source_id', 'device'}):
                    raise ValueError('start, end, valueと任意のunit/source_id/deviceのみ指定してください')
                value = row['value']
                if metric == 'sleep' and isinstance(value, str):
                    value = SLEEP_NAMES.get(value, value)
                if metric != 'sleep' and isinstance(value, str):
                    # Numeric text only. No unit stripping, locale guessing or NaN/Infinity.
                    if not re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', value.strip()):
                        raise ValueError('数値文字列が不正です。単位はunitに指定してください')
                    value = float(value.strip())
                raw_unit = row.get('unit', group['unit'])
                if not isinstance(raw_unit, str) or (metric == 'sleep' and raw_unit != ''):
                    raise ValueError('unitは文字列です（睡眠は空文字）')
                sample = dict(metric=metric, source=group['source'],
                              unit=None if metric == 'sleep' else UNIT_NAMES.get(raw_unit, raw_unit),
                              start_at=row['start'], end_at=row['end'], value=value)
                sample.update({k: row[k] for k in ('source_id', 'device') if k in row})
                rows.append(normalize(sample))
            except ValueError as error:
                raise ValueError(f'groups[{gi}] samples[{si}]: {error}') from None
    return rows


def ingest(store, body):
    started = time.monotonic()
    if not isinstance(body, dict) or type(body.get('schema')) is not int:
        raise ValueError('schema:1または2を指定してください')
    if body['schema'] == 2:
        normalized = unpack_groups(shortcuts_body(body))
    elif body['schema'] == 1:
        if set(body) != {'schema', 'user_id', 'samples'}:
            raise ValueError('schema:1, user_id, samplesを指定してください')
        samples = body['samples']
        if not isinstance(samples, list) or len(samples) > MAX_SAMPLES:
            raise ValueError('1リクエストは最大50000サンプルです。期間を分けて送信してください')
        normalized = [normalize(s) for s in samples]
    else:
        raise ValueError('schema:1または2を指定してください')
    received_at = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not isinstance(body['user_id'], str) or not db.execute('SELECT 1 FROM users WHERE id=?', (body['user_id'],)).fetchone():
            raise ValueError('登録済みの利用者を指定してください')
        inserted = sum(insert(db, body['user_id'], s, received_at) for s in normalized)
    # connect() commits before this response can be returned; no RAM-only acknowledgement.
    result = {'inserted': inserted, 'duplicates': len(normalized) - inserted, 'received': len(normalized)}
    if body['schema'] == 2:
        counts = {metric: 0 for metric in METRICS}
        for row in normalized:
            counts[row['metric']] += 1
        result.update(counts=counts,
                      start_at=min((s['start_at'] for s in normalized), default=None),
                      end_at=max((s['end_at'] for s in normalized), default=None),
                      processing_ms=round((time.monotonic() - started) * 1000, 2))
    return result


def history(store, user_id, since=None, until=None, limit=200, offset=0):
    if not isinstance(user_id, str) or not user_id:
        raise ValueError('user_idが必要です')
    terms, args = ['user_id=?'], [user_id]
    start, end = instant(since) if since else None, instant(until) if until else None
    if start and end and start >= end:
        raise ValueError('期間が不正です')
    if start:
        terms.append('end_at>=?'); args.append(start)
    if end:
        terms.append('start_at<?'); args.append(end)
    where = ' AND '.join(terms)
    with store.connect() as db:
        if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
            raise ValueError('利用者が見つかりません')
        total = db.execute('SELECT COUNT(*) FROM apple_health_samples WHERE ' + where, args).fetchone()[0]
        rows = db.execute('SELECT payload,received_at FROM apple_health_samples WHERE ' + where +
                          ' ORDER BY start_at,sample_key LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
    return {'total': total, 'samples': [dict(json.loads(r[0]), received_at=r[1]) for r in rows]}


def backup_rows(db):
    return [dict(user_id=r[0], sample=json.loads(r[1]), received_at=r[2]) for r in
            db.execute('SELECT user_id,payload,received_at FROM apple_health_samples ORDER BY user_id,start_at,sample_key')]


def restore_rows(db, rows, users):
    if not isinstance(rows, list):
        raise ValueError('Apple Healthバックアップが不正です')
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'user_id', 'sample', 'received_at'}
                or not isinstance(row['user_id'], str) or row['user_id'] not in users):
            raise ValueError('Apple Healthバックアップの利用者・項目が不正です')
        if not insert(db, row['user_id'], normalize(row['sample']), instant(row['received_at'])):
            raise ValueError('バックアップ内でApple Healthサンプルが重複しています')
