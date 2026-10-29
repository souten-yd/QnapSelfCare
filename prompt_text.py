"""Compact plain-text context for AI prompts.

Small NAS models (QnapAssistant, often a 4096-token context) cannot take the full JSON context:
Japanese JSON with keys, nulls and long reference texts easily passes 4000 tokens before the
answer even starts. This renders only the useful values as short lines, highest priority first,
and stops adding sections once the character budget is used.
"""

BUDGET_CHARS = 1800          # context text; roughly 1500-2000 tokens for Japanese with numbers
STATUS = {'good': '良好', 'ok': '目安内', 'warn': '注意', 'bad': '要注意'}
TREND = {'up': '多め', 'down': '少なめ', 'usual': 'いつもどおり', 'none': '比較なし'}
TREND_BY_ID = {'weight': {'up': '高め', 'down': '低め'}, 'systolic': {'up': '高め', 'down': '低め'},
               'balance': {'up': '摂取が上回る', 'down': '消費が上回る'}}


def _n(value, unit=''):
    if value is None or value == '':
        return None
    if isinstance(value, float):
        value = round(value, 1)
        if value == int(value):
            value = int(value)
    return f'{value}{unit}'


def _join(parts, sep='、'):
    return sep.join(p for p in parts if p)


def _md(d):
    """'2026-10-27' -> '10/27'."""
    try:
        return f'{int(d[5:7])}/{int(d[8:10])}'
    except (TypeError, ValueError, IndexError):
        return str(d)


def current(ctx):
    m = ctx.get('measurements') or {}
    bp = m.get('latest_pressure') or {}
    pressure = f"血圧{_n(bp.get('systolic'))}/{_n(bp.get('diastolic'))}" if bp.get('systolic') else None
    line = _join([
        _n(m.get('latest_weight'), 'kg') and f"体重{_n(m.get('latest_weight'), 'kg')}",
        _n(m.get('weight_change_7d_kg'), 'kg') and f"7日変化{_n(m.get('weight_change_7d_kg'), 'kg')}",
        _n(m.get('bmi')) and f"BMI{_n(m.get('bmi'))}",
        _n(m.get('estimated_bmr_kcal'), 'kcal') and f"推定基礎代謝{_n(m.get('estimated_bmr_kcal'), 'kcal')}",
        pressure])
    return line and '現在: ' + line


def goal(ctx):
    g, m, p = ctx.get('goal') or {}, ctx.get('measurements') or {}, ctx.get('weight_plan') or {}
    a = ctx.get('plan_analysis') or {}
    lines = []
    if g.get('weight_kg') or g.get('date'):
        lines.append('目標: ' + _join([_n(g.get('weight_kg'), 'kg'), g.get('date') and f"{g['date']}まで",
                                      _n(m.get('weekly_change_needed_kg'), 'kg') and f"必要な週変化{_n(m.get('weekly_change_needed_kg'), 'kg')}"]))
    if p:
        lines.append('減量計画: ' + _join([
            p.get('start_date') and f"{p['start_date']} {_n(p.get('start_weight'), 'kg')}→{_n(p.get('goal_weight'), 'kg')}（{p.get('goal_date')}）",
            _n(p.get('deficit_kcal'), 'kcal') and f"1日の不足{_n(p.get('deficit_kcal'), 'kcal')}",
            _n(p.get('exercise_minutes'), '分') and f"運動{_n(p.get('exercise_minutes'), '分')}",
            _n(a.get('planned_intake_kcal'), 'kcal') and f"計画摂取{_n(a.get('planned_intake_kcal'), 'kcal')}",
            _n(a.get('safety_floor_kcal'), 'kcal') and f"安全な下限{_n(a.get('safety_floor_kcal'), 'kcal')}",
            _n(a.get('projected_goal_weight'), 'kg') and f"目標日の試算{_n(a.get('projected_goal_weight'), 'kg')}"]))
    e = ctx.get('energy') or {}
    if e.get('total_kcal'):
        lines.append('エネルギー: ' + _join([f"推定消費{_n(e.get('total_kcal'), 'kcal')}/日",
                                         _n(e.get('target_kcal'), 'kcal') and f"摂取の目安{_n(e.get('target_kcal'), 'kcal')}",
                                         _n(e.get('intake_kcal'), 'kcal') and f"摂取{_n(e.get('intake_kcal'), 'kcal')}"]))
    if ctx.get('saved_plan'):
        lines.append('保存した計画メモ: ' + ctx['saved_plan'][:200])
    return '\n'.join(lines)


def day_review(ctx):
    r = ctx.get('day_review')
    if not r:
        return ''
    rows = []
    for i in r.get('items', []):
        if i.get('value') is None:
            continue
        avg = f"7日平均{_n(i.get('avg7'), i['unit'])}・" if i.get('avg7') is not None else ''
        rows.append(f"- {i['label']}: {_n(i['value'], i['unit'])}（{avg}{TREND_BY_ID.get(i.get('id'), {}).get(i.get('trend')) or TREND.get(i.get('trend'), '')}）")
    missing = [i['label'] for i in r.get('items', []) if i.get('value') is None]
    text = f"{_md(r.get('date'))}の記録（直前7日平均との比較）\n" + '\n'.join(rows)
    if missing:
        text += '\n記録なし: ' + '、'.join(missing)
    return text


def plan_review(ctx):
    r = ctx.get('plan_review')
    if not r:
        return ''
    lines = ['計画を見直す理由: ' + '、'.join(x.get('text', str(x)) if isinstance(x, dict) else str(x) for x in r.get('reasons', []))]
    for o in r.get('options', []):
        lines.append(f"案{o.get('id')} {o.get('title', '')}: " + _join([
            '、'.join(map(str, o.get('changes') or [])) if isinstance(o.get('changes'), list) else str(o.get('changes') or ''),
            _n(o.get('weekly_loss_kg'), 'kg') and f"週{_n(o.get('weekly_loss_kg'), 'kg')}減",
            _n(o.get('planned_intake_kcal'), 'kcal') and f"摂取{_n(o.get('planned_intake_kcal'), 'kcal')}",
            _n(o.get('projected_goal_weight'), 'kg') and f"目標日の試算{_n(o.get('projected_goal_weight'), 'kg')}",
            o.get('warnings') and '注意: ' + '、'.join(map(str, o['warnings']))]))
    return '\n'.join(lines)


def evaluations(ctx, only_flags=False):
    rows = []
    for e in ctx.get('evaluations') or []:
        if e.get('status') not in STATUS or e.get('value') in (None, '—'):
            continue
        if only_flags and e['status'] in ('ok',):
            continue
        rows.append(f"- {e['label']}: {e['value']}（{STATUS[e['status']]}）")
    return rows and '評価指標（公的な目安との比較）\n' + '\n'.join(rows)


def weeks(ctx, count=4):
    rows = []
    for w in (ctx.get('weekly_review') or [])[-count:]:
        if w.get('actual') is None:
            continue
        rows.append(f"- {_md(w.get('date'))}〜: 計画{_n(w.get('planned'), 'kg')} 実績{_n(w.get('actual'), 'kg')}"
                    + (f"（差{w['deviation']:+.1f}kg）" if w.get('deviation') is not None else ''))
    return rows and '週ごとの体重（計画との比較）\n' + '\n'.join(rows)


def activity(ctx, count=7):
    rows = []
    for a in (ctx.get('recent_activity') or [])[-count:]:
        parts = _join([_n(a.get('steps'), '歩'), _n(a.get('minutes'), '分運動'), _n(a.get('intake_kcal'), 'kcal摂取'),
                       '（いつもの設定）' if a.get('source') == 'routine' else None], ' ')
        if parts:
            rows.append(f"- {_md(a.get('date'))} {parts}")
    return rows and '最近の活動と摂取\n' + '\n'.join(rows)


def meals(ctx, count=3):
    rows = [f"- {_md(m.get('created_at', '')[:10])} {m.get('note', '')[:80]}" + (f"（{_n(m.get('calories'), 'kcal')}）" if m.get('calories') else '')
            for m in (ctx.get('recent_meals') or [])[:count]]
    return rows and '最近の食事メモ\n' + '\n'.join(rows)


def notices(ctx):
    items = list((ctx.get('measurements') or {}).get('notices') or []) + list(ctx.get('energy_notices') or [])
    return items and 'お知らせ: ' + ' / '.join(map(str, items[:4]))


def render(ctx, mode, budget=BUDGET_CHARS):
    """Sections in priority order for the mode; lower-priority sections are dropped when over budget."""
    nightly = mode == 'daily' or 'plan_review' in ctx
    order = [day_review(ctx), plan_review(ctx), current(ctx), goal(ctx), notices(ctx),
             evaluations(ctx, only_flags=nightly), weeks(ctx, 2 if nightly else 4)]
    if mode in ('meal', 'diet', 'health') or not nightly:
        order.append(meals(ctx))
    if not nightly:
        order.append(activity(ctx))
    out, used = [], 0
    for section in order:
        if not section:
            continue
        if used + len(section) > budget and out:
            continue  # skip a section that does not fit, but try smaller later ones
        out.append(section)
        used += len(section) + 2
    return '\n\n'.join(out) or '記録: まだほとんどありません（数値に基づく評価はできません）'
