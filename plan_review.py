"""Weight-plan check with revision options the user must approve before anything changes.

1. Detect whether the active plan needs a review (behind plan, late forecast, unsafe pace, plateau…).
2. Build up to three options with the same energy model the app uses (extend the goal date / adjust
   intake / add exercise). Options that would cut intake below the safety floor or lose more than
   1 % of body weight per week are never offered.
3. Ask the AI to explain the trade-offs and recommend one option; without AI a simple rule recommends.
4. Nothing changes until the user applies an option. Applying saves a new plan revision with a reason,
   so the history of plans is kept.
"""
from datetime import datetime, timedelta
import json
import re

import energy
import health_metrics

LETTERS = ('A', 'B', 'C')
MAX_WEEKLY_PCT = 1.0      # never propose faster than 1 % of body weight per week
SAFE_WEEKLY_PCT = 0.5     # default pace when the observed one is not usable
REVIEW_INTERVAL_DAYS = 7  # at most one automatic proposal per week
NEW_PLAN_GRACE_DAYS = 14  # a new or revised plan gets two weeks before it is judged automatically


def now_iso():
    return datetime.now(energy.JST).isoformat(timespec='seconds')


def review_reasons(metrics, report):
    """Plain reasons why the active plan should be reviewed; [] when it is on track."""
    plan = report.get('active_plan')
    if not plan:
        return []
    by = {m['id']: m for m in metrics['diet']}
    reasons = []
    gap, forecast, pace = by.get('plan_gap'), by.get('goal_forecast'), by.get('weight_pace')
    if gap and gap['status'] in ('ok', 'warn'):
        reasons.append(f"計画線より{gap['value'].lstrip('+')}重い状態です。")
    if forecast and forecast['status'] == 'warn' and forecast['value'] == '予測できません':
        reasons.append('体重の減少傾向がはっきりせず、目標日に届くか予測できません。')
    elif forecast and forecast['status'] == 'warn' and forecast['value'] != '到達済み':
        reasons.append(f"今のペースでは目標日に間に合わない見込みです（{forecast['value']}）。")
    if pace and pace['status'] == 'warn':
        reasons.append(f"減量ペースが目安から外れています（{pace['value']}）。")
    if any('横ばい' in n for n in report.get('notices', [])):
        reasons.append('直近3週の平均体重がほぼ横ばい、または増加しています。')
    analysis = report.get('plan_analysis') or {}
    if analysis.get('floor_blocks_required_intake'):
        reasons.append('目標日から逆算した摂取量が安全な下限を下回っています。')
    if plan['goal_date'] < report['today']:
        reasons.append('計画の目標日を過ぎています。')
    return reasons


def _current_weight(metrics, report):
    pace = next((m for m in metrics['diet'] if m['id'] == 'weight_pace'), None)
    match = re.search(r'現在の体重は([\d.]+) kg', pace['detail']) if pace else None
    if match:
        return float(match.group(1))
    weights = [h['weight'] for h in report.get('history', []) if h.get('weight') is not None]
    return weights[-1] if weights else None


def _observed_kg_per_day(metrics):
    pace = next((m for m in metrics['diet'] if m['id'] == 'weight_pace'), None)
    if not pace or pace['status'] == 'unknown':
        return None
    match = re.match(r'([+-][\d.]+) kg/週（([+-][\d.]+)%）', pace['value'])
    if not match:
        return None
    kg_week, pct = float(match.group(1)), float(match.group(2))
    return -kg_week / 7 if -MAX_WEEKLY_PCT <= pct <= -0.25 else None


def build_options(store, user_id, metrics, report, profile, today):
    plan = report['active_plan']
    start_weight = _current_weight(metrics, report)
    if start_weight is None:
        return []
    start_weight = round(start_weight, 1)
    goal = plan['goal_weight']
    if goal >= start_weight:
        return []
    keep = {k: plan.get(k) for k in ('pal', 'deficit_kcal', 'target_kcal', 'adaptation_pct', 'exercise_minutes', 'exercise_met')}
    base = dict(keep, start_date=today.isoformat(), start_weight=start_weight, goal_weight=goal, goal_date=plan['goal_date'])
    to_lose = start_weight - goal
    days_left = (energy.day(plan['goal_date']) - today).days
    candidates = []
    # A: keep daily life, move the goal date to a safe pace (the observed pace when it is already safe).
    per_day = _observed_kg_per_day(metrics) or start_weight * SAFE_WEEKLY_PCT / 100 / 7
    extend_days = min(1095, max(14, round(to_lose / per_day)))
    candidates.append(('extend', '目標日を見直す', dict(base, goal_date=(today + timedelta(days=extend_days)).isoformat())))
    if days_left >= 14:
        # B: keep the goal date, set the daily intake the date requires (only if safe).
        trial = store.energy_report(user_id, extra_plan=dict(base, target_kcal=None, reason='試算'))['plan_analysis']
        needed = trial and trial.get('required_start_intake_kcal')
        if needed is not None and needed >= trial['safety_floor_kcal'] and to_lose / days_left * 7 / start_weight * 100 <= MAX_WEEKLY_PCT:
            candidates.append(('intake', '摂取量を見直す', dict(base, target_kcal=int(round(needed, -1)))))
        # C: keep the goal date and intake, add about 30 minutes of brisk walking a day.
        minutes = min(120, int(keep.get('exercise_minutes') or 0) + 30)
        candidates.append(('exercise', '運動を増やす', dict(base, exercise_minutes=minutes, exercise_met=max(keep.get('exercise_met') or 1, 4.3))))
    options = []
    for kind, title, entry in candidates:
        letter = LETTERS[len(options)]
        entry = dict(entry, reason='')
        try:
            analysis = store.energy_report(user_id, extra_plan=entry)['plan_analysis']
        except ValueError:
            continue
        weeks = max(1, (energy.day(entry['goal_date']) - today).days) / 7
        weekly_kg = (start_weight - goal) / weeks
        weekly_pct = weekly_kg / start_weight * 100
        warnings = []
        if weekly_pct > MAX_WEEKLY_PCT:
            continue
        if analysis.get('floor_blocks_required_intake'):
            warnings.append('目標日から逆算した摂取量が安全な下限を下回るため、期日には届かない見込みです。')
        gap = analysis.get('projected_gap_kg')
        if gap is not None and gap > 1.0:
            warnings.append(f'この案だけでは、目標日に約{gap:.1f} kg届かない見込みです（消費の変化を含む試算）。')
        height = profile.get('height_cm')
        if height and goal / (height / 100) ** 2 < profile['bmi_low']:
            warnings.append('目標体重がBMIの参考下限を下回ります。目標体重も見直してください。')
        changes = []
        for key, label, unit in (('goal_date', '目標日', ''), ('target_kcal', '標準摂取', ' kcal/日'),
                                 ('exercise_minutes', '標準運動', ' 分/日'), ('exercise_met', '運動の強さ', ' MET')):
            if entry.get(key) != plan.get(key) and not (key == 'target_kcal' and entry.get(key) is None):
                fmt = lambda v: f'{v:g}' if isinstance(v, float) else str(v)
                before = fmt(plan[key]) + unit if plan.get(key) is not None else '自動'
                changes.append(f'{label}: {before} → {fmt(entry[key])}{unit}')
        if not changes:
            continue  # same as the current plan: nothing to decide
        options.append({'id': letter, 'kind': kind, 'title': title, 'entry': entry, 'changes': changes, 'warnings': warnings,
                        'weekly_loss_kg': round(weekly_kg, 2), 'weekly_pct': round(weekly_pct, 2),
                        'planned_intake_kcal': analysis.get('planned_intake_kcal'),
                        'projected_goal_weight': analysis.get('projected_goal_weight'),
                        'projected_gap_kg': analysis.get('projected_gap_kg')})
    return options


def rule_recommendation(options, metrics):
    """Without AI: a too-fast pace → extend; otherwise the first option that reaches the goal on time."""
    pace = next((m for m in metrics['diet'] if m['id'] == 'weight_pace'), None)
    if pace and '速すぎ' in pace['detail']:
        return next((o['id'] for o in options if o['kind'] == 'extend'), None)
    for kind in ('exercise', 'intake', 'extend'):
        option = next((o for o in options if o['kind'] == kind and not o['warnings']
                       and (o['projected_gap_kg'] is None or o['projected_gap_kg'] <= 0.5)), None)
        if option:
            return option['id']
    return options[0]['id'] if options else None


class PlanReviewer:
    def __init__(self, store, coach):
        self.store, self.coach = store, coach

    def _row(self, row):
        item = dict(row)
        for key in ('reasons', 'options'):
            item[key] = json.loads(item[key])
        return item

    def proposals(self, user_id, limit=10):
        self.store.wellness_profile(user_id)
        with self.store.connect() as db:
            rows = [self._row(r) for r in db.execute(
                'SELECT * FROM ai_plan_proposals WHERE user_id=? ORDER BY id DESC LIMIT ?', (user_id, limit))]
        report = self.store.energy_report(user_id)
        current = (report.get('active_plan') or {}).get('id')
        for row in rows:  # a pending proposal made for an older plan can no longer be applied
            if row['status'] == 'pending' and row['base_plan_id'] != current:
                row['status'] = 'outdated'
        pending = next((r for r in rows if r['status'] == 'pending'), None)
        return {'pending': pending, 'history': rows, 'has_plan': current is not None}

    def review(self, user_id, trigger='manual', allow_ai=True, cancelled=None, poll=3):
        """Create a proposal if the plan needs one. Returns the new proposal, or None when on track."""
        today = energy.today()
        report = self.store.energy_report(user_id)
        plan = report.get('active_plan')
        if not plan:
            return None
        if trigger == 'schedule':
            if energy.day(plan['start_date']) > today - timedelta(days=NEW_PLAN_GRACE_DAYS):
                return None
            with self.store.connect() as db:
                recent = db.execute('SELECT created_at, decided_at, base_plan_id, status FROM ai_plan_proposals WHERE user_id=? ORDER BY id DESC LIMIT 1',
                                    (user_id,)).fetchone()
            if recent:
                last = max(datetime.fromisoformat(recent['created_at']).date(),
                           datetime.fromisoformat(recent['decided_at']).date() if recent['decided_at'] else datetime.min.date())
                if (recent['status'] == 'pending' and recent['base_plan_id'] == plan['id']) or last > today - timedelta(days=REVIEW_INTERVAL_DAYS):
                    return None
        metrics = health_metrics.compute(self.store, user_id, today)
        reasons = review_reasons(metrics, report)
        if not reasons:
            return None
        profile = self.store.wellness_profile(user_id)
        options = build_options(self.store, user_id, metrics, report, profile, today)
        if not options:
            return None
        recommended, by, answer, error = rule_recommendation(options, metrics), 'rule', None, None
        if allow_ai and self.coach.settings()['base_url']:
            summary = [{k: o[k] for k in ('id', 'title', 'changes', 'warnings', 'weekly_loss_kg', 'weekly_pct',
                                          'planned_intake_kcal', 'projected_goal_weight', 'projected_gap_kg')} for o in options]
            question = ('減量計画の見直しが必要かを判断し、context.plan_reviewの修正案を比較してください。'
                        '最初の行に「推奨: A」のように推奨する案を1つだけ書き、続けて理由とトレードオフ、'
                        '案を選ぶ前に確認したいことを簡潔に説明してください。案にない数値の計画は作らないでください。')
            try:
                answer = self.coach.ask(user_id, 'review', question, extra={'plan_review': {'reasons': reasons, 'options': summary}},
                                        cancelled=cancelled, poll=poll)['answer']
                match = re.search(r'推奨\s*[:：]\s*案?\s*([ABC])', answer)
                if match and any(o['id'] == match.group(1) for o in options):
                    recommended, by = match.group(1), 'ai'
            except ValueError as failure:
                error = str(failure)
        with self.store.connect() as db:
            db.execute("UPDATE ai_plan_proposals SET status='superseded' WHERE user_id=? AND status='pending'", (user_id,))
            cursor = db.execute(
                'INSERT INTO ai_plan_proposals(user_id,created_at,status,trigger,base_plan_id,reasons,options,recommended,recommended_by,answer,error)'
                ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (user_id, now_iso(), 'pending', trigger, plan['id'], json.dumps(reasons, ensure_ascii=False),
                 json.dumps(options, ensure_ascii=False), recommended, by, answer, error))
            proposal_id = cursor.lastrowid
        return self.get(user_id, proposal_id)

    def get(self, user_id, proposal_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM ai_plan_proposals WHERE user_id=? AND id=?', (user_id, proposal_id)).fetchone()
        if not row:
            raise ValueError('計画の修正案が見つかりません')
        return self._row(row)

    def apply(self, user_id, proposal_id, option_id):
        proposal = self.get(user_id, proposal_id)
        if proposal['status'] != 'pending':
            raise ValueError('この修正案はすでに処理済みです')
        current = (self.store.energy_report(user_id).get('active_plan') or {}).get('id')
        if current != proposal['base_plan_id']:
            raise ValueError('案を作った後に計画が変更されました。最新の計画で点検し直してください')
        option = next((o for o in proposal['options'] if o['id'] == option_id), None)
        if not option:
            raise ValueError('選択した案が見つかりません')
        reason = f"修正案{option['id']}「{option['title']}」を適用：" + ' '.join(proposal['reasons'])
        saved = self.store.save_weight_plan(user_id, dict(option['entry'], reason=reason[:1000]))
        with self.store.connect() as db:
            db.execute("UPDATE ai_plan_proposals SET status='applied', applied_option=?, decided_at=? WHERE id=?",
                       (option_id, now_iso(), proposal_id))
        return {'applied': option_id, 'plan': saved}

    def dismiss(self, user_id, proposal_id):
        proposal = self.get(user_id, proposal_id)
        if proposal['status'] != 'pending':
            raise ValueError('この修正案はすでに処理済みです')
        with self.store.connect() as db:
            db.execute("UPDATE ai_plan_proposals SET status='dismissed', decided_at=? WHERE id=?", (now_iso(), proposal_id))
        return {'dismissed': proposal_id}
