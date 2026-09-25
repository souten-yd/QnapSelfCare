"""Read OMRON connect history CSVs without changing device or pairing settings."""
import csv
from datetime import datetime, timedelta, timezone
import io
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HEADERS = {
    'blood_pressure': {'測定日', 'タイムゾーン', '最高血圧(mmHg)', '最低血圧(mmHg)', '脈拍(bpm)', '機種'},
    'body_composition': {'測定日', 'タイムゾーン', '体重(kg)', '機種'},
}
MODELS = {'blood_pressure': 'HEM-6232T', 'body_composition': 'HBF-228T'}
FIELDS = {
    'blood_pressure': {'最高血圧(mmHg)': 'systolic', '最低血圧(mmHg)': 'diastolic', '脈拍(bpm)': 'pulse'},
    'body_composition': {'体重(kg)': 'weight', '体脂肪(%)': 'body_fat',
                         '内臓脂肪レベル': 'visceral_fat', '基礎代謝(kcal)': 'bmr',
                         '骨格筋(%)': 'muscle', 'BMI': 'bmi', '体年齢(才)': 'body_age'},
}


def _time(value, region):
    try:
        local = datetime.strptime(value.strip(), '%Y/%m/%d %H:%M')
    except (AttributeError, ValueError):
        try:
            local = datetime.strptime(value.strip(), '%Y/%m/%d %H:%M:%S')
        except (AttributeError, ValueError):
            raise ValueError('測定日の形式が不正です（YYYY/MM/DD HH:MM）') from None
    if region == 'Asia/Tokyo':
        zone = timezone(timedelta(hours=9))
    elif isinstance(region, str) and re.fullmatch(r'[+-](?:0\d|1[0-4]):[0-5]\d', region):
        sign = 1 if region[0] == '+' else -1
        zone = timezone(sign * timedelta(hours=int(region[1:3]), minutes=int(region[4:6])))
    else:
        try:
            zone = ZoneInfo(region)
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            raise ValueError('タイムゾーンが不明です') from None
    return local.replace(tzinfo=zone).isoformat()


def parse(content, user_id):
    if not isinstance(content, str) or len(content.encode('utf-8')) > 8 * 1024 * 1024 or '\0' in content:
        raise ValueError('CSVは8MB以内です')
    reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff'), newline=''), strict=True)
    header = reader.fieldnames or []
    kind = next((name for name, required in HEADERS.items() if required <= set(header)), None)
    if not kind or len(header) != len(set(header)):
        raise ValueError('画像のOMRON形式の血圧・体組成CSVを選択してください')
    records = []
    try:
        for row in reader:
            if not any(value for value in row.values()):
                continue
            if len(records) >= 10000:
                raise ValueError('一度の取込は10000件までです')
            if None in row or not isinstance(row['機種'], str) or row['機種'].strip() != MODELS[kind]:
                raise ValueError(f'{reader.line_num}行目: 機種またはCSVの列数が想定と異なります')
            try:
                values = {metric: float(row[column]) for column, metric in FIELDS[kind].items()
                          if column in row and row[column] and row[column].strip()}
                measured_at = _time(row['測定日'], row['タイムゾーン'])
            except ValueError as error:
                raise ValueError(f'{reader.line_num}行目: {error}') from None
            extra = [f'{column}: {value}' for column, value in row.items()
                     if column not in FIELDS[kind] and column not in ('測定日', 'タイムゾーン', '機種') and value and value.strip()]
            note = ' / '.join(extra)
            if len(note) > 1000:
                raise ValueError(f'{reader.line_num}行目: 付加情報が長すぎます')
            records.append({'user_id': user_id, 'kind': kind, 'measured_at': measured_at,
                            'values': values, 'note': note})
    except csv.Error as error:
        raise ValueError(f'{reader.line_num}行目: CSVの引用符または区切りが不正です') from error
    if not records:
        raise ValueError('測定データがありません')
    return kind, records
