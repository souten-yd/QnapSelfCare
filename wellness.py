"""Deterministic wellness summaries; no diagnosis or autonomous treatment advice."""
from datetime import datetime, timedelta, timezone
import math

DEFAULT = {'height_cm': None, 'age': None, 'sex': '', 'goal_weight_kg': None,
           'goal_date': '', 'bp_sys_low': 90, 'bp_sys_high': 135,
           'bp_dia_low': 60, 'bp_dia_high': 85, 'bmi_low': 18.5, 'bmi_high': 24.9,
           'plan_text': '', 'plan_updated_at': ''}


def number(value, minimum, maximum, label, nullable=False):
    if nullable and value in (None, ''):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'{label}は{minimum}〜{maximum}の数値を指定してください')
    return round(float(value), 2)


def validate(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('健康設定の項目が不正です')
    result = dict(DEFAULT, **value)
    result['height_cm'] = number(result['height_cm'], 100, 250, '身長', True)
    result['age'] = number(result['age'], 18, 120, '年齢', True)
    if result['age'] is not None and not result['age'].is_integer():
        raise ValueError('年齢は整数で指定してください')
    result['sex'] = str(result['sex'])
    if result['sex'] not in ('', 'male', 'female'):
        raise ValueError('推定基礎代謝の計算条件が不正です')
    result['goal_weight_kg'] = number(result['goal_weight_kg'], 25, 400, '目標体重', True)
    for key, label, low, high in [('bp_sys_low', '最高血圧下限', 40, 250),
                                   ('bp_sys_high', '最高血圧上限', 40, 350),
                                   ('bp_dia_low', '最低血圧下限', 30, 180),
                                   ('bp_dia_high', '最低血圧上限', 30, 250),
                                   ('bmi_low', 'BMI下限', 10, 60), ('bmi_high', 'BMI上限', 10, 60)]:
        result[key] = number(result[key], low, high, label)
    for left, right in [('bp_sys_low', 'bp_sys_high'), ('bp_dia_low', 'bp_dia_high'), ('bmi_low', 'bmi_high')]:
        if result[left] >= result[right]:
            raise ValueError('下限は上限より小さくしてください')
    if not isinstance(result['goal_date'], str) or len(result['goal_date']) > 10:
        raise ValueError('目標日が不正です')
    if result['goal_date']:
        try:
            datetime.strptime(result['goal_date'], '%Y-%m-%d')
        except ValueError:
            raise ValueError('目標日はYYYY-MM-DD形式です') from None
    if not isinstance(result['plan_text'], str) or len(result['plan_text']) > 8000:
        raise ValueError('計画は8000文字以内です')
    if not isinstance(result['plan_updated_at'], str) or len(result['plan_updated_at']) > 40:
        raise ValueError('計画日時が不正です')
    return result


def summarize(profile, records, meals=(), current=None):
    current = current or datetime.now(timezone.utc)
    weights = sorted((r for r in records if r['kind'] == 'body_composition' and 'weight' in r['values']),
                     key=lambda r: r['measured_at'])
    pressure = sorted((r for r in records if r['kind'] == 'blood_pressure'), key=lambda r: r['measured_at'])
    latest_weight = weights[-1] if weights else None
    latest_pressure = pressure[-1] if pressure else None
    weight = latest_weight['values']['weight'] if latest_weight else None
    height = profile['height_cm']
    bmi = round(weight / (height / 100) ** 2, 1) if weight is not None and height else None
    bmr = None
    if weight is not None and height and profile['age'] is not None and profile['sex']:
        bmr = round(10 * weight + 6.25 * height - 5 * profile['age'] + (5 if profile['sex'] == 'male' else -161))
    # Multiple same-day readings are all retained. Use each day's mean for a trend.
    daily = {}
    for r in weights:
        day = (datetime.fromisoformat(r['measured_at']) + timedelta(hours=9)).date().isoformat()
        daily.setdefault(day, []).append(r['values']['weight'])
    daily = [{'date': day, 'weight': round(sum(values) / len(values), 2), 'readings': len(values)}
             for day, values in sorted(daily.items())]
    trend = None
    if len(daily) >= 2:
        recent = daily[-1]
        earlier = next((x for x in reversed(daily[:-1])
                        if (datetime.fromisoformat(recent['date']) - datetime.fromisoformat(x['date'])).days >= 7), None)
        if earlier:
            trend = round(recent['weight'] - earlier['weight'], 2)
    notices = []
    goal = profile['goal_weight_kg']
    weekly_needed = None
    if goal is not None and weight is not None and profile['goal_date']:
        days = (datetime.fromisoformat(profile['goal_date']).date() - (current + timedelta(hours=9)).date()).days
        if days > 0:
            weekly_needed = round((weight - goal) * 7 / days, 2)
            if weekly_needed > 0.9:
                notices.append('目標日までの減量ペースが速めです。目標時期を見直し、医療者と相談してください')
        else:
            notices.append('目標日を過ぎました。測定結果に合わせて計画を見直してください')
    if goal is not None and height and goal / (height / 100) ** 2 < profile['bmi_low']:
        notices.append('目標体重は設定したBMI下限より低くなります。目標を確認してください')
    if latest_pressure:
        v = latest_pressure['values']
        if v['systolic'] > profile['bp_sys_high'] or v['diastolic'] > profile['bp_dia_high']:
            notices.append('最新の血圧は設定した参考上限を超えています。測定条件と経過を確認してください')
        elif v['systolic'] < profile['bp_sys_low'] or v['diastolic'] < profile['bp_dia_low']:
            notices.append('最新の血圧は設定した参考下限を下回っています。症状があれば医療者に相談してください')
    if latest_weight and (current - datetime.fromisoformat(latest_weight['measured_at'])).days >= 14:
        notices.append('体重の新しい記録が14日以上ありません')
    if profile['plan_text'] and profile['plan_updated_at']:
        try:
            if (current - datetime.fromisoformat(profile['plan_updated_at'])).days >= 30:
                notices.append('計画を最後に保存してから30日以上経過しました。進捗に合わせて見直してください')
        except ValueError:
            pass
    return {'latest_weight': weight, 'latest_pressure': latest_pressure['values'] if latest_pressure else None,
            'bmi': bmi, 'estimated_bmr_kcal': bmr, 'weight_change_7d_kg': trend,
            'goal_weight_kg': goal, 'weekly_change_needed_kg': weekly_needed,
            'daily_weights': daily[-90:], 'notices': notices, 'meal_count': len(meals),
            'profile': profile}
