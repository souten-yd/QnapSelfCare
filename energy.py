"""Dated energy diaries and versioned weekly plans. Estimates, not prescriptions."""
from datetime import date, datetime, timedelta, timezone
from statistics import mean
import wellness

JST = timezone(timedelta(hours=9))


def today():
    return datetime.now(JST).date()


def day(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError('日付はYYYY-MM-DDで指定してください')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError('日付が不正です') from None


def diary(value):
    allowed = {'date', 'pal', 'steps', 'minutes', 'total_kcal', 'intake_kcal', 'target_kcal', 'note'}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError('活動記録の項目が不正です')
    d = day(value.get('date'))
    if d > today():
        raise ValueError('活動実績は今日以前の日付にしてください')
    result = {'date': d.isoformat(), 'pal': wellness.number(value.get('pal', 1.5), 1.2, 2.4, '活動係数')}
    for key, maximum in [('steps', 100000), ('minutes', 1440), ('total_kcal', 15000), ('intake_kcal', 15000), ('target_kcal', 6000)]:
        result[key] = wellness.number(value.get(key), 0, maximum, key, True)
    if result['target_kcal'] is not None and result['target_kcal'] < 1000:
        raise ValueError('目標摂取量は1000 kcal以上を指定してください')
    result['note'] = value.get('note', '')
    if not isinstance(result['note'], str) or len(result['note']) > 1000:
        raise ValueError('活動メモは1000文字以内です')
    return result


def plan(value):
    allowed = {'start_date', 'start_weight', 'goal_date', 'goal_weight', 'pal', 'deficit_kcal', 'target_kcal',
               'adaptation_pct', 'exercise_minutes', 'exercise_met', 'reason'}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError('減量計画の項目が不正です')
    start, end = day(value.get('start_date')), day(value.get('goal_date'))
    if not 7 <= (end - start).days <= 1095 or start > today():
        raise ValueError('開始日は今日以前、目標日は開始から7日〜3年以内にしてください')
    result = {'start_date': start.isoformat(), 'goal_date': end.isoformat()}
    defaults = {'pal': 1.2, 'deficit_kcal': 300, 'adaptation_pct': 0, 'exercise_minutes': 0, 'exercise_met': 1}
    for key, low, high in [('start_weight', 25, 400), ('goal_weight', 25, 400), ('pal', 1.2, 2.4),
                           ('deficit_kcal', 0, 750), ('adaptation_pct', 0, 15),
                           ('exercise_minutes', 0, 1440), ('exercise_met', 1, 20)]:
        result[key] = wellness.number(value.get(key, defaults.get(key)), low, high, key)
    if result['goal_weight'] > result['start_weight']:
        raise ValueError('減量計画の目標体重は開始体重以下にしてください')
    result['target_kcal'] = wellness.number(value.get('target_kcal'), 1000, 6000, '標準摂取量', True)
    result['reason'] = value.get('reason', '')
    if not isinstance(result['reason'], str) or len(result['reason']) > 1000:
        raise ValueError('見直し理由は1000文字以内です')
    return result


def exercise_kcal(weight, minutes, met):
    """Estimated net exercise energy so resting energy is not counted twice."""
    if weight is None or not minutes or met is None:
        return 0
    return round(max(met - 1, 0) * 3.5 * weight / 200 * minutes)


def bmr(profile, weight):
    if weight is None or not profile.get('height_cm') or profile.get('age') is None or not profile.get('sex'):
        return None
    return round(10 * weight + 6.25 * profile['height_cm'] - 5 * profile['age'] + (5 if profile['sex'] == 'male' else -161))


def report(profile, records, diaries, plans, current=None, routines=()):
    current = current or today()
    grouped = {}
    for r in records:
        if r['kind'] == 'body_composition' and 'weight' in r['values']:
            d = datetime.fromisoformat(r['measured_at']).astimezone(JST).date().isoformat()
            if d <= current.isoformat():
                grouped.setdefault(d, []).append(r['values']['weight'])
    weights = {d: round(mean(v), 2) for d, v in sorted(grouped.items())}
    active = plans[-1] if plans else None
    days = {x['date']: dict(x, source='entered') for x in diaries}
    # Defaults are dated assumptions, never inserted as measured daily records.
    routine_days = {}
    ordered = sorted(routines, key=lambda r: (r['effective_date'], r.get('id', 0)))
    routine = None
    if ordered:
        cursor, index = day(ordered[0]['effective_date']), 0
        while cursor <= current:
            key = cursor.isoformat()
            while index < len(ordered) and ordered[index]['effective_date'] <= key:
                routine = ordered[index]['entry']
                index += 1
            if routine is not None:
                routine_days[key] = dict(routine, date=key, source='routine')
            cursor += timedelta(days=1)
    effective = dict(routine_days, **days)
    history = []
    last_weight, last_date = None, None
    # Never use future measurements to fill an earlier activity entry.
    for d in sorted(set(weights) | set(effective)):
        if d in weights:
            last_weight, last_date = weights[d], d
        entry = effective.get(d, {})
        applicable = next((p for p in reversed(plans) if p['start_date'] <= d), None)
        pal = entry.get('pal', applicable['pal'] if applicable else 1.5)
        basal = bmr(profile, last_weight)
        total = entry.get('total_kcal')
        estimated = round(basal * pal) if basal is not None else None
        total = total if total is not None else estimated
        target = entry.get('target_kcal')
        if target is None and applicable:
            target = applicable['target_kcal']
            if target is None and total is not None:
                candidate = round(total - applicable['deficit_kcal'])
                target = candidate if candidate >= max(1200, basal or 1200) else None
        history.append({'date': d, 'weight': weights.get(d), 'weight_used': last_weight, 'weight_date': last_date,
                        'bmr': basal, 'total_kcal': total, 'source': entry.get('source', 'estimate'),
                        'total_source': ('いつもの設定' if entry.get('source') == 'routine' else '入力') if entry.get('total_kcal') is not None else '推定',
                        'intake_kcal': entry.get('intake_kcal'), 'target_kcal': target,
                        'balance_kcal': round(entry['intake_kcal'] - total) if entry.get('intake_kcal') is not None and total is not None else None})
    weeks, projection, notices = [], [], []
    if active:
        start, end = day(active['start_date']), day(active['goal_date'])
        duration = (end-start).days
        for offset in range(0, duration+1, 7):
            a = start + timedelta(days=offset)
            z = min(a+timedelta(days=6), end)
            values = [v for d, v in weights.items() if a.isoformat() <= d <= z.isoformat()]
            actual = round(mean(values), 2) if values else None
            reference_date = min(z, current) if a <= current else z
            expected = round(active['start_weight'] + (active['goal_weight']-active['start_weight']) * (reference_date-start).days / duration, 2)
            weeks.append({'date': a.isoformat(), 'end': z.isoformat(), 'planned': expected, 'actual': actual,
                          'days': len(values), 'partial': a <= current <= z, 'future': a > current,
                          'remaining': round(actual-active['goal_weight'], 2) if actual is not None else None,
                          'deviation': round(actual-expected, 2) if actual is not None else None})
        w = active['start_weight']
        for offset in range(duration+1):
            basal = bmr(profile, w)
            if basal is None:
                break
            exercise = exercise_kcal(w, active.get('exercise_minutes', 0), active.get('exercise_met', 1))
            # User-selected sensitivity scenario; no claim to measure metabolic adaptation.
            reduction = active['adaptation_pct']/100 * min(offset/56, 1)
            expenditure = (basal * active['pal'] + exercise) * (1-reduction)
            target = active['target_kcal']
            if target is None:
                target = max(1200, basal, expenditure-active['deficit_kcal'])
            if offset % 7 == 0 or offset == duration:
                projection.append({'date': (start+timedelta(days=offset)).isoformat(), 'weight': round(w, 2),
                                   'bmr': basal, 'exercise_kcal': exercise, 'total_kcal': round(expenditure),
                                   'target_kcal': round(target)})
            w = max(25, min(400, w + (target-expenditure)/7700))
        complete = [x for x in weeks if x['end'] < current.isoformat()]
        if len(complete) >= 3 and all(x['days'] >= 3 for x in complete[-3:]):
            if complete[-3]['actual'] - complete[-1]['actual'] < 0.2:
                notices.append('直近3週の平均体重はほぼ横ばい、または増加しています。食事記録・活動量・測定条件を確認し、目標日を見直せます。代謝低下とは断定できません。')
        pace = (active['start_weight']-active['goal_weight'])*7/duration
        if pace > 0.9:
            notices.append('計画の減量ペースが速めです。目標日の延長を検討してください。')
        if profile.get('height_cm') and active['goal_weight']/(profile['height_cm']/100)**2 < profile['bmi_low']:
            notices.append('目標体重が設定したBMI参考下限を下回ります。目標を確認してください。')
        if projection and projection[-1]['weight'] > active['goal_weight']+1:
            notices.append('消費量の変化を含む試算では目標日に届かない見込みです。週次実績と比較し、期間や計画を見直してください。')
        if active['goal_date'] < current.isoformat():
            notices.append('計画の目標日を過ぎています。新しい計画を保存して見直せます。')
    latest_weight = next(reversed(weights.values()), None) if weights else None
    basal = bmr(profile, latest_weight)
    entry = effective.get(current.isoformat(), {})
    pal = entry.get('pal', active['pal'] if active else 1.5)
    total = entry.get('total_kcal')
    if total is None and basal is not None:
        total = round(basal*pal)
    target = entry.get('target_kcal')
    if target is None and active:
        target = active['target_kcal']
        if target is None and total is not None:
            candidate = total-active['deficit_kcal']
            if candidate >= max(1200, basal or 1200):
                target = round(candidate)
            else:
                notices.append('自動計算の摂取目標が低すぎるため提示を止めています。消費差や目標日を見直してください。')
    if target is not None and basal is not None and target < basal:
        notices.append('入力した摂取目標は推定基礎代謝を下回っています。専門家と相談して目標を確認してください。')
    analysis = None
    if active:
        duration = (day(active['goal_date']) - day(active['start_date'])).days
        start_basal = bmr(profile, active['start_weight'])
        start_exercise = exercise_kcal(active['start_weight'], active.get('exercise_minutes', 0), active.get('exercise_met', 1))
        start_total = round(start_basal * active['pal'] + start_exercise) if start_basal is not None else None
        required_deficit = round((active['start_weight'] - active['goal_weight']) * 7700 / duration) if duration else None
        safety_floor = max(1200, start_basal or 1200)
        planned_intake = active['target_kcal']
        if planned_intake is None and start_total is not None:
            planned_intake = round(max(safety_floor, start_total - active['deficit_kcal']))
        projected_weight = projection[-1]['weight'] if projection else None
        required_intake = round(start_total - required_deficit) if start_total is not None and required_deficit is not None else None
        analysis = {'duration_days': duration, 'required_daily_deficit_kcal': required_deficit,
                    'start_bmr_kcal': start_basal, 'start_exercise_kcal': start_exercise,
                    'start_total_kcal': start_total, 'planned_intake_kcal': planned_intake,
                    'required_start_intake_kcal': required_intake, 'safety_floor_kcal': safety_floor,
                    'projected_goal_weight': projected_weight,
                    'projected_gap_kg': round(projected_weight-active['goal_weight'], 2) if projected_weight is not None else None,
                    'floor_blocks_required_intake': required_intake is not None and required_intake < safety_floor}
    return {'today': current.isoformat(), 'current': {'bmr': basal, 'total_kcal': total, 'target_kcal': target,
            'weight_date': next(reversed(weights), None) if weights else None, 'pal': pal,
            'intake_kcal': entry.get('intake_kcal'), 'source': entry.get('source', 'estimate')},
            'history': history, 'weeks': weeks, 'projection': projection, 'notices': notices,
            'diaries': diaries, 'entries': [effective[d] for d in sorted(effective)], 'routine': routine,
            'routines': routines, 'plans': plans, 'active_plan': active, 'plan_analysis': analysis}
