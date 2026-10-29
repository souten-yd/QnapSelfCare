'use strict';
/* Home dashboard: today's checks, diet progress, activity and sleep at a glance. */
(() => {
  const DEFAULT_STEP_GOAL = 8000;
  const DAY = 86400000;
  const SLEEP_STAGES = ['deep', 'core', 'rem', 'asleep_unspecified'];
  const STAGE_NAMES = {deep: '深い', core: 'コア', rem: 'レム', asleep_unspecified: '睡眠'};

  // ---- Pure calculations (exported for Node tests) ----
  function dayKey(t) { const d = new Date(t); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; }
  function dayStart(key) { return +new Date(key + 'T00:00:00'); }
  function lastDays(today, count) { const out = []; const base = new Date(today + 'T12:00:00'); for (let i = count-1; i >= 0; i--) { const d = new Date(base); d.setDate(d.getDate()-i); out.push(dayKey(d)); } return out; }
  function union(intervals) { let end = -Infinity, total = 0; for (const [a, b] of [...intervals].sort((x, y) => x[0]-y[0])) { total += Math.max(0, b-Math.max(a, end)); end = Math.max(end, b); } return total; }
  // Several sources (watch + phone) record the same steps; count only the busiest one.
  function mainSource(rows) { const counts = new Map(); for (const r of rows) counts.set(r.source, (counts.get(r.source) || 0)+1); let best = null, n = 0; for (const [s, c] of counts) if (c > n) { best = s; n = c; } return best; }
  // Each metric keeps its own busiest source: steps may come from the phone while heart rate and
  // blood oxygen come only from the watch, so one shared source would hide the watch-only metrics.
  function perMetricMainSource(rows) {
    const byMetric = new Map(); for (const r of rows) { if (!byMetric.has(r.metric)) byMetric.set(r.metric, []); byMetric.get(r.metric).push(r); }
    const best = new Map(); for (const [m, list] of byMetric) best.set(m, mainSource(list));
    return rows.filter(r => best.get(r.metric) === r.source);
  }
  function dailyTotals(rows, days) {
    const out = Object.fromEntries(days.map(d => [d, {steps: null, distance: null, active_energy: null, heart: [], oxygen: []}]));
    for (const r of rows) {
      const slot = out[dayKey(Date.parse(r.start_at))]; if (!slot) continue;
      if (r.metric === 'steps') slot.steps = (slot.steps || 0) + r.value;
      else if (r.metric === 'walking_running_distance') slot.distance = (slot.distance || 0) + r.value/1000;
      else if (r.metric === 'active_energy') slot.active_energy = (slot.active_energy || 0) + r.value;
      else if (r.metric === 'heart_rate') slot.heart.push(r.value);
      else if (r.metric === 'oxygen_saturation') slot.oxygen.push(r.value);
    }
    return out;
  }
  // Sleep for day D is what overlaps 18:00 of D-1 to 18:00 of D, excluding awake and in-bed time.
  function sleepFor(rows, key) {
    const b = dayStart(key) + 18*3600000, a = b - DAY;
    const list = rows.filter(r => r.metric === 'sleep' && Date.parse(r.end_at) > a && Date.parse(r.start_at) < b);
    const clip = r => [Math.max(a, Date.parse(r.start_at)), Math.min(b, Date.parse(r.end_at))];
    let asleep = list.filter(r => SLEEP_STAGES.includes(r.value)), inBedOnly = false;
    // Some watch apps (e.g. HUAWEI Health) write only "in bed" time to Apple Health: show it, labelled as such.
    if (!asleep.length) { asleep = list.filter(r => r.value === 'in_bed'); inBedOnly = asleep.length > 0; }
    if (!asleep.length) return null;
    if (inBedOnly) { const times = asleep.map(clip); return {total: union(times), stages: {}, inBedOnly, start: Math.min(...times.map(t => t[0])), end: Math.max(...times.map(t => t[1]))}; }
    const stages = Object.fromEntries(SLEEP_STAGES.map(s => [s, union(asleep.filter(r => r.value === s).map(clip))]));
    const times = asleep.map(clip);
    return {total: union(times), stages, start: Math.min(...times.map(t => t[0])), end: Math.max(...times.map(t => t[1]))};
  }
  // Daily lowest / highest / average of a vital (heart rate, blood oxygen); null without samples.
  function vitalRange(values) {
    const v = (values || []).filter(Number.isFinite); if (!v.length) return null;
    return {min: Math.min(...v), max: Math.max(...v), avg: v.reduce((a, b) => a+b, 0)/v.length, n: v.length};
  }
  // Newest day (from the end of `days`) for which pick(day) is truthy; null when none.
  function latestDay(days, pick) { for (let i = days.length-1; i >= 0; i--) if (pick(days[i])) return days[i]; return null; }
  function dietProgress(weights, plan, goalWeight) {
    const known = weights.filter(w => Number.isFinite(w.weight));
    if (!known.length) return null;
    const current = known.at(-1);
    const start = plan ? {date: plan.start_date, weight: plan.start_weight} : {date: known[0].date, weight: known[0].weight};
    const goal = plan ? plan.goal_weight : goalWeight;
    const result = {current: current.weight, current_date: current.date, start: start.weight, start_date: start.date, goal: goal ?? null,
      goal_date: plan?.goal_date || null, remaining: null, percent: null, change7: null};
    if (Number.isFinite(goal)) {
      result.remaining = Math.round((current.weight-goal)*10)/10;
      const span = start.weight-goal;
      if (span > 0) result.percent = Math.max(0, Math.min(100, Math.round((start.weight-current.weight)/span*100)));
    }
    const weekAgo = new Date(Date.parse(current.date+'T00:00:00Z')-7*DAY).toISOString().slice(0, 10);
    const before = known.filter(w => w.date <= weekAgo).at(-1);
    if (before) result.change7 = Math.round((current.weight-before.weight)*10)/10;
    return result;
  }
  // Burn = estimated BMR + recorded active energy (falls back to BMR alone when no watch data).
  function energyBalance(bmr, active, intake) {
    const ok = v => v != null && Number.isFinite(v);
    if (!ok(bmr)) return {burn: null, balance: null, basis: null};
    const burn = Math.round(bmr + (ok(active) ? active : 0));
    return {burn, balance: ok(intake) ? Math.round(intake - burn) : null, basis: ok(active) ? 'bmr+active' : 'bmr'};
  }
  // One or two plain sentences that highlight what needs attention today.
  function summaryText(x) {
    const out = [];
    if (x.remaining != null) out.push(x.remaining <= 0 ? '目標体重に到達しています。' : `目標体重まであと${x.remaining.toFixed(1)}kg${x.change7 != null && x.change7 < 0 ? `（1週間で${Math.abs(x.change7).toFixed(1)}kg減）` : x.change7 != null && x.change7 > 0 ? `（1週間で${x.change7.toFixed(1)}kg増）` : ''}。`);
    for (const e of x.evaluations || []) out.push(e);
    if (x.bpHigh && !(x.evaluations || []).some(e => e.includes('血圧'))) out.push('血圧が参考上限を超えています。');
    if (x.steps != null) out.push(x.steps >= x.stepGoal ? '歩数は目標を達成。' : `歩数は目標まであと${Math.round(x.stepGoal-x.steps).toLocaleString('ja-JP')}歩。`);
    if (x.sleep != null && x.sleepAvg != null) { const d = Math.round((x.sleep-x.sleepAvg)/60000); if (Math.abs(d) >= 20) out.push(`睡眠は平均より${Math.abs(d)}分${d < 0 ? '短め' : '長め'}。`); }
    else if (x.sleep == null) out.push('昨夜の睡眠記録はありません。');
    if (x.balance != null) out.push(`今日の収支は${x.balance > 0 ? '+' : x.balance < 0 ? '−' : '±'}${Math.abs(x.balance).toLocaleString('ja-JP')}kcal${x.balance > 0 ? '（摂取が上回っています）' : ''}。`);
    return out.join('') || '記録がそろうと、ここに今日のまとめを表示します。';
  }
  function stepGoalOf(profile) { const v = Number(profile?.step_goal); return Number.isInteger(v) && v >= 1000 && v <= 50000 ? v : DEFAULT_STEP_GOAL; }
  if (typeof module !== 'undefined' && module.exports) { module.exports = {dayKey, lastDays, vitalRange, union, mainSource, perMetricMainSource, dailyTotals, latestDay, sleepFor, dietProgress, energyBalance, summaryText, adviceHighlights, stepGoalOf, DEFAULT_STEP_GOAL}; return; }
  let STEP_GOAL = DEFAULT_STEP_GOAL;

  // ---- Rendering ----
  const $ = id => document.getElementById(id);
  const NS = 'http://www.w3.org/2000/svg';
  function el(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined && text !== null) n.textContent = text; if (cls) n.className = cls; return n; }
  function svg(tag, attrs, text) { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v)); if (text !== undefined) n.textContent = text; return n; }
  const num = (v, digits = 0) => v == null || !Number.isFinite(v) ? '—' : Number(v).toLocaleString('ja-JP', {minimumFractionDigits: digits, maximumFractionDigits: digits});
  const hm = ms => ms == null ? '—' : `${Math.floor(ms/3600000)}時間${String(Math.round(ms/60000)%60).padStart(2, '0')}分`;
  const clock = t => new Date(t).toLocaleTimeString('ja-JP', {hour: '2-digit', minute: '2-digit'});
  const short = key => { const d = new Date(key+'T12:00:00'); return `${d.getMonth()+1}/${d.getDate()}`; };
  const weekday = key => '日月火水木金土'[new Date(key+'T12:00:00').getDay()];
  function stat(label, value, unit, note, cls) { const box = el('div', undefined, 'home-stat'+(cls ? ' '+cls : '')); box.append(el('span', label)); const v = el('strong', value); if (unit) v.append(el('small', ' '+unit)); box.append(v); if (note) box.append(el('small', note)); return box; }
  function empty(target, text) { target.replaceChildren(el('p', text, 'home-empty')); }

  function ring(percent, color, center, sub) {
    const box = el('div', undefined, 'home-ring');
    const g = svg('svg', {viewBox: '0 0 120 120', role: 'img', 'aria-label': `${center} ${sub || ''}`.trim()});
    const r = 50, c = 2*Math.PI*r, p = Math.max(0, Math.min(100, percent ?? 0));
    g.append(svg('circle', {cx: 60, cy: 60, r, fill: 'none', stroke: 'var(--ring-track)', 'stroke-width': 11}));
    g.append(svg('circle', {cx: 60, cy: 60, r, fill: 'none', stroke: color, 'stroke-width': 11, 'stroke-linecap': 'round',
      'stroke-dasharray': `${c*p/100} ${c}`, transform: 'rotate(-90 60 60)'}));
    box.append(g);
    const label = el('div', undefined, 'home-ring-label'); label.append(el('strong', center)); if (sub) label.append(el('small', sub)); box.append(label);
    return box;
  }

  const STATUS_TEXT = {good: '良好', ok: '注意', warn: '要見直し', unknown: 'データ不足'};
  const CONFIDENCE_TEXT = {high: '信頼度 高', medium: '信頼度 中', low: '信頼度 低'};
  function renderEvaluations(target, items) {
    target.replaceChildren();
    for (const m of items) {
      const li = el('li', undefined, 'home-eval-item '+m.status);
      const box = el('details'); box.open = m.status === 'warn';
      const head = el('summary', undefined, 'eval-head');
      head.append(el('span', STATUS_TEXT[m.status] || '', 'eval-badge'), el('span', m.label, 'eval-label'), el('strong', m.value, 'eval-value'));
      const meta = el('small', undefined, 'eval-meta'); meta.append(el('span', '目安：'+m.reference));
      if (CONFIDENCE_TEXT[m.confidence]) meta.append(el('span', CONFIDENCE_TEXT[m.confidence], 'eval-confidence '+m.confidence));
      box.append(head, el('p', m.detail, 'eval-detail'), meta); li.append(box); target.append(li);
    }
  }
  // Home shows only the 要点 section of the latest daily advice.
  function adviceHighlights(answer) {
    const lines = String(answer || '').split('\n');
    const start = lines.findIndex(l => /^#{1,4}\s*要点/.test(l));
    if (start < 0) return lines.slice(0, 4).join('\n');
    const end = lines.findIndex((l, i) => i > start && /^#{1,4}\s/.test(l));
    return lines.slice(start + 1, end < 0 ? undefined : end).join('\n').trim();
  }
  function renderAdviceCard(item) {
    const card = $('home-advice-card'), box = $('home-advice');
    if (!item || !['done', 'review_only'].includes(item.status)) { card.hidden = true; return; }
    card.hidden = false; box.replaceChildren();
    const d = new Date(item.day + 'T12:00:00');
    box.append(el('p', `${d.getMonth()+1}月${d.getDate()}日の振り返り${item.trigger === 'schedule' ? '（自動作成）' : ''}`, 'home-advice-date'));
    if (item.answer) {
      const body = el('div', undefined, 'ai-answer home-advice-body');
      if (typeof renderMarkdown === 'function') renderMarkdown(body, adviceHighlights(item.answer)); else body.textContent = adviceHighlights(item.answer);
      box.append(body);
    } else if (item.review?.notes?.length) {
      const ul = el('ul', undefined, 'advice-notes'); item.review.notes.forEach(n => ul.append(el('li', n))); box.append(ul);
    } else box.append(el('p', 'この日の記録はいつもどおりでした。', 'home-empty'));
  }
  function renderSummary(tiles, text) {
    const box = $('home-summary'); box.replaceChildren();
    for (const t of tiles) {
      const tile = el('div', undefined, 'home-summary-tile'+(t.state ? ' '+t.state : ''));
      const head = el('span', undefined, 'tile-head'); head.append(el('span', t.label));
      if (t.mark) head.append(el('span', t.mark, 'mark'));
      const value = el('strong', t.value); if (t.unit && t.value !== '—') value.append(el('small', ' '+t.unit));
      tile.append(head, value, el('small', t.sub || '', 'tile-sub')); box.append(tile);
    }
    $('home-summary-text').textContent = text;
  }

  function sparkline(weights, plan, goal) {
    const pts = weights.filter(w => Number.isFinite(w.weight));
    if (pts.length < 2) return el('p', '推移を描くには2日以上の記録が必要です。', 'home-empty');
    const W = 320, H = 96, pad = 6;
    const times = pts.map(p => Date.parse(p.date+'T00:00:00Z'));
    const t0 = times[0], t1 = Math.max(times.at(-1), t0+DAY);
    const planLine = plan ? [[Date.parse(plan.start_date+'T00:00:00Z'), plan.start_weight], [Date.parse(plan.goal_date+'T00:00:00Z'), plan.goal_weight]] : null;
    const vals = pts.map(p => p.weight).concat(Number.isFinite(goal) ? [goal] : []);
    const lo = Math.min(...vals)-0.3, hi = Math.max(...vals)+0.3;
    const x = t => pad+(t-t0)/(t1-t0)*(W-2*pad), y = v => H-pad-(v-lo)/(hi-lo)*(H-2*pad);
    const g = svg('svg', {viewBox: `0 0 ${W} ${H}`, class: 'home-spark', role: 'img', 'aria-label': `直近${pts.length}日の体重推移`, preserveAspectRatio: 'none'});
    const defs = svg('defs', {}); const grad = svg('linearGradient', {id: 'home-spark-fill', x1: 0, x2: 0, y1: 0, y2: 1});
    grad.append(svg('stop', {offset: '0%', 'stop-color': 'var(--chart-primary)', 'stop-opacity': .28}), svg('stop', {offset: '100%', 'stop-color': 'var(--chart-primary)', 'stop-opacity': 0}));
    defs.append(grad); g.append(defs);
    if (Number.isFinite(goal)) g.append(svg('line', {x1: pad, x2: W-pad, y1: y(goal), y2: y(goal), stroke: 'var(--energy-target)', 'stroke-dasharray': '4 4', 'stroke-width': 1}));
    if (planLine) {
      // Interpolate the plan line within the visible window only.
      const at = t => planLine[0][1]+(planLine[1][1]-planLine[0][1])*(t-planLine[0][0])/Math.max(DAY, planLine[1][0]-planLine[0][0]);
      const a = Math.max(t0, planLine[0][0]), b = Math.min(t1, planLine[1][0]);
      if (b > a) g.append(svg('line', {x1: x(a), x2: x(b), y1: y(at(a)), y2: y(at(b)), stroke: 'var(--chart-secondary)', 'stroke-width': 1.5, 'stroke-dasharray': '2 3'}));
    }
    const line = pts.map((p, i) => `${x(times[i]).toFixed(1)},${y(p.weight).toFixed(1)}`);
    g.append(svg('path', {d: `M${line[0]} L${line.join(' L')} L${x(times.at(-1)).toFixed(1)},${H} L${x(times[0]).toFixed(1)},${H} Z`, fill: 'url(#home-spark-fill)'}));
    g.append(svg('polyline', {points: line.join(' '), fill: 'none', stroke: 'var(--chart-primary)', 'stroke-width': 2.2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke'}));
    const wrap = el('div', undefined, 'home-spark-wrap'); wrap.append(g);
    const legend = el('div', undefined, 'home-legend');
    legend.append(el('span', `${short(pts[0].date)}〜${short(pts.at(-1).date)}`, 'muted'));
    if (planLine) legend.append(el('span', '┄ 計画線', 'plan'));
    if (Number.isFinite(goal)) legend.append(el('span', '┄ 目標', 'goal'));
    wrap.append(legend);
    return wrap;
  }

  function renderDiet(energy, summary) {
    const target = $('home-diet');
    if (!energy || !summary) { empty(target, '体重を記録すると、目標までの進み具合を表示します。'); return null; }
    const weights = energy.history.filter(h => h.weight != null).map(h => ({date: h.date, weight: h.weight}));
    const recent = weights.filter(w => w.date >= new Date(Date.parse(energy.today+'T00:00:00Z')-90*DAY).toISOString().slice(0, 10));
    const plan = energy.active_plan || null;
    const p = dietProgress(recent.length ? recent : weights, plan, summary.goal_weight_kg);
    if (!p) { empty(target, '体重を記録すると、目標までの進み具合を表示します。'); return null; }
    const head = el('div', undefined, 'home-diet-head');
    head.append(ring(p.percent, 'var(--chart-primary)', p.percent == null ? '—' : p.percent+'%', p.percent == null ? '目標未設定' : '達成'));
    const main = el('div', undefined, 'home-diet-main');
    const now = el('div', undefined, 'home-big'); now.append(el('strong', num(p.current, 1)), el('span', 'kg'));
    main.append(el('span', '現在の体重', 'home-label'), now, el('small', `${p.current_date.replaceAll('-', '/')} 時点`, 'muted'));
    head.append(main); target.replaceChildren(head);
    const daysLeft = p.goal_date ? Math.round((Date.parse(p.goal_date+'T00:00:00Z')-Date.parse(energy.today+'T00:00:00Z'))/DAY) : null;
    const change = p.change7 == null ? '—' : (p.change7 > 0 ? '+' : '')+num(p.change7, 1);
    const grid = el('div', undefined, 'home-stats');
    grid.append(stat('目標', num(p.goal, 1), p.goal == null ? '' : 'kg', p.goal == null ? '健康サポートで設定' : `開始 ${num(p.start, 1)} kg`),
      stat('残り', p.remaining == null ? '—' : num(Math.max(0, p.remaining), 1), p.remaining == null ? '' : 'kg', p.remaining != null && p.remaining <= 0 ? '目標に到達' : ''),
      stat('7日間の変化', change, p.change7 == null ? '' : 'kg', '', p.change7 == null ? '' : p.change7 <= 0 ? 'good' : 'warn'),
      stat('目標日まで', daysLeft == null ? '—' : num(Math.max(0, daysLeft)), daysLeft == null ? '' : '日', p.goal_date ? p.goal_date.replaceAll('-', '/') : '計画未設定'));
    target.append(grid);
    const week = (energy.weeks || []).find(w => w.partial);
    if (week && week.actual != null) {
      const dev = week.deviation;
      target.append(el('p', `今週の平均 ${num(week.actual, 1)} kg（計画 ${num(week.planned, 1)} kg、${dev > 0 ? '+' : ''}${num(dev, 1)} kg）`, 'home-note '+(dev < 0.05 ? 'good' : 'warn')));
    }
    target.append(sparkline(recent.slice(-30), plan, p.goal));
    return p;
  }

  function renderActivity(today, todayEntry) {
    const target = $('home-activity'); target.replaceChildren();
    const steps = today?.steps;
    const row = el('div', undefined, 'home-activity-row');
    row.append(ring(steps == null ? 0 : steps/STEP_GOAL*100, 'var(--activity)', num(steps), `/ ${num(STEP_GOAL)} 歩`));
    const stats = el('div', undefined, 'home-stats home-stats-tight');
    stats.append(stat('活動エネルギー', num(today?.active_energy), today?.active_energy == null ? '' : 'kcal'),
      stat('距離', num(today?.distance, 2), today?.distance == null ? '' : 'km'),
      stat('運動時間', num(todayEntry?.minutes), todayEntry?.minutes == null ? '' : '分', todayEntry?.source === 'routine' ? 'いつもの設定' : ''));
    row.append(stats); target.append(row);
    if (steps == null) target.append(el('p', 'ウォッチ・ヘルスケアの記録はまだありません。記録なしはゼロではありません。', 'home-empty'));
    else if (steps >= STEP_GOAL) target.append(el('p', '歩数の目標を達成しました。', 'home-note good'));
    else target.append(el('p', `目標まであと ${num(STEP_GOAL-steps)} 歩`, 'home-note'));
  }

  function renderSleep(night, average, dayLabel) {
    const target = $('home-sleep');
    if (!night) { empty(target, '直近7日の睡眠記録はありません。'); return; }
    target.replaceChildren();
    if (dayLabel) target.append(el('p', `昨夜の記録はありません。最新の記録（${dayLabel}）を表示しています。`, 'home-note'));
    const big = el('div', undefined, 'home-big'); big.append(el('strong', `${Math.floor(night.total/3600000)}`), el('span', '時間'), el('strong', String(Math.round(night.total/60000)%60).padStart(2, '0')), el('span', '分'));
    target.append(big, el('small', `${clock(night.start)} 〜 ${clock(night.end)}`, 'muted'));
    const bar = el('div', undefined, 'home-stage-bar'), legend = el('div', undefined, 'home-legend');
    for (const s of SLEEP_STAGES) {
      const ms = night.stages[s]; if (!ms) continue;
      const seg = el('span', undefined, 'stage-'+s); seg.style.flexGrow = String(ms); seg.title = `${STAGE_NAMES[s]} ${hm(ms)}`; bar.append(seg);
      const item = el('span', `${STAGE_NAMES[s]} ${hm(ms)}`, 'stage-label stage-'+s); legend.append(item);
    }
    if (night.inBedOnly) target.append(el('p', 'ベッドにいた時間です（ウォッチのアプリが睡眠段階をヘルスケアへ書き込んでいません）。', 'home-note'));
    else target.append(bar, legend);
    if (average != null) { const diff = night.total-average; target.append(el('p', `7日平均 ${hm(average)}（${diff >= 0 ? '+' : '−'}${Math.round(Math.abs(diff)/60000)}分）`, 'home-note')); }
  }

  function renderHeart(today, dayLabel) {
    const target = $('home-heart');
    const hr = today?.heart || [], ox = today?.oxygen || [];
    if (!hr.length && !ox.length) { empty(target, '直近7日の心拍・血中酸素の記録はありません。'); return; }
    target.replaceChildren();
    if (dayLabel) target.append(el('p', `今日の記録はまだありません。最新の記録（${dayLabel}）を表示しています。`, 'home-note'));
    const avg = list => list.reduce((a, b) => a+b, 0)/list.length;
    const grid = el('div', undefined, 'home-stats home-stats-tight');
    grid.append(stat('平均心拍', hr.length ? num(avg(hr)) : '—', hr.length ? 'bpm' : '', hr.length ? `${num(Math.min(...hr))}〜${num(Math.max(...hr))} bpm` : ''),
      stat('血中酸素', ox.length ? num(avg(ox)) : '—', ox.length ? '%' : '', ox.length ? `${ox.length}件の平均` : ''));
    target.append(grid, el('p', '記録値の単純平均です。', 'home-foot'));
  }

  function renderEnergy(energy, active, isLive) {
    const target = $('home-energy');
    const c = energy?.current;
    if (!c) { empty(target, '身長・年齢などを設定すると推定値を表示します。'); return; }
    target.replaceChildren();
    const r = energyBalance(c.bmr, active, c.intake_kcal);
    const sign = v => (v > 0 ? '+' : v < 0 ? '−' : '±')+num(Math.abs(v));
    const big = el('div', undefined, 'home-big'); big.append(el('strong', r.balance == null ? '—' : sign(r.balance)), el('span', r.balance == null ? '' : 'kcal'));
    if (r.balance != null) big.classList.add(r.balance <= 0 ? 'good' : 'warn');
    target.append(el('span', '収支 ＝ 摂取 − 基礎代謝 − 活動エネルギー', 'home-label'), big);
    const formula = el('div', undefined, 'home-formula');
    formula.append(stat('摂取', num(c.intake_kcal), c.intake_kcal == null ? '' : 'kcal', c.target_kcal ? `目標 ${num(c.target_kcal)}` : ''), el('span', '−', 'op'),
      stat('基礎代謝', num(c.bmr), c.bmr == null ? '' : 'kcal', '推定'), el('span', '−', 'op'),
      stat('活動', num(active), active == null ? '' : 'kcal', active == null ? '記録なし' : 'ウォッチ'));
    target.append(formula);
    if (r.burn != null) target.append(el('p', `消費合計 ${num(r.burn)} kcal${r.basis === 'bmr+active' ? '' : '（基礎代謝のみ）'}`, 'home-note'));
    const notes = [];
    if (c.intake_kcal == null) notes.push('今日の摂取は未入力です。');
    else if (c.source === 'routine') notes.push('摂取はいつもの設定の値です。');
    if (r.basis === 'bmr') notes.push('活動エネルギーの記録がないため、消費は基礎代謝のみです。');
    if (c.bmr == null) notes.push('基礎代謝は身長・年齢・計算条件・体重から推定します。');
    if (isLive && active != null) notes.push('活動エネルギーは現在までの記録分です。');
    target.append(el('p', notes.join(' ') || '摂取は今日の入力値です。', 'home-foot'));
  }

  function balanceBars(target, days, rows) {
    target.replaceChildren();
    const values = rows.map(r => r.balance);
    if (values.every(v => v == null)) { target.append(el('p', '摂取の入力とウォッチの活動エネルギーがある日に表示します。', 'home-empty')); return; }
    const W = 340, H = 150, top = 16, bottom = 26, gap = 10, bw = (W-gap*(days.length+1))/days.length;
    const max = Math.max(200, ...values.filter(v => v != null).map(Math.abs))*1.15;
    const mid = top+(H-top-bottom)/2, scale = ((H-top-bottom)/2-12)/max;
    const g = svg('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': '直近7日のエネルギー収支'});
    g.append(svg('line', {x1: 0, x2: W, y1: mid, y2: mid, stroke: 'var(--chart-grid)', 'stroke-width': 1.5}));
    days.forEach((d, i) => {
      const x = gap+i*(bw+gap), v = values[i], isToday = i === days.length-1;
      if (v != null) {
        const h = Math.max(2, Math.abs(v)*scale), y = v > 0 ? mid-h : mid;
        const rect = svg('rect', {x, y, width: bw, height: h, rx: 3, fill: v > 0 ? 'var(--warn)' : 'var(--good)', opacity: isToday ? .55 : .9});
        rect.append(svg('title', {}, `${short(d)} 収支 ${v > 0 ? '+' : ''}${num(v)} kcal（摂取 ${num(rows[i].intake)} − 消費 ${num(rows[i].burn)}）`)); g.append(rect);
        g.append(svg('text', {x: x+bw/2, y: v > 0 ? y-4 : y+h+11, 'text-anchor': 'middle', 'font-size': 9.5, fill: 'var(--chart-label)'}, (v > 0 ? '+' : v < 0 ? '−' : '')+Math.abs(Math.round(v))));
      } else g.append(svg('rect', {x, y: mid-1.5, width: bw, height: 3, rx: 1.5, fill: 'var(--chart-grid)'}));
      g.append(svg('text', {x: x+bw/2, y: H-8, 'text-anchor': 'middle', 'font-size': 11, fill: isToday ? 'var(--text)' : 'var(--chart-label)', 'font-weight': isToday ? 700 : 400}, isToday ? '今日' : weekday(d)));
    });
    target.append(g);
    const legend = el('div', undefined, 'home-legend');
    legend.append(el('span', '■ 消費が上回る', 'bal-good'), el('span', '■ 摂取が上回る', 'bal-warn'), el('span', '今日は途中経過', 'muted'));
    target.append(legend);
  }

  // 7-day range chart: a rounded bar from the day's lowest to highest value with the average as a dot
  // (the Apple Health / Fitbit style). opts.line also joins the daily averages with a line.
  function rangeChart(target, days, ranges, opts) {
    target.replaceChildren();
    const known = ranges.filter(Boolean);
    if (!known.length) { target.append(el('p', opts.empty, 'home-empty')); return; }
    const W = 340, H = 160, left = 30, top = 18, bottom = 26, gap = 10, bw = (W-left-gap*(days.length+1))/days.length;
    let lo = Math.min(...known.map(r => r.min), opts.ref ?? Infinity), hi = Math.max(...known.map(r => r.max), opts.ref ?? -Infinity);
    const pad = Math.max(opts.minPad || 2, (hi-lo)*.12); lo = Math.floor(lo-pad); hi = Math.ceil(hi+pad);
    if (opts.ceil != null) hi = Math.min(hi, opts.ceil);
    const y = v => top+(hi-v)/(hi-lo || 1)*(H-top-bottom);
    const g = svg('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': opts.label});
    for (const v of [hi, Math.round((hi+lo)/2), lo]) {
      g.append(svg('line', {x1: left, x2: W, y1: y(v), y2: y(v), stroke: 'var(--chart-grid)', 'stroke-width': 1}));
      g.append(svg('text', {x: left-4, y: y(v)+3.5, 'text-anchor': 'end', 'font-size': 9.5, fill: 'var(--chart-label)'}, String(v)));
    }
    if (opts.ref != null) g.append(svg('line', {x1: left, x2: W, y1: y(opts.ref), y2: y(opts.ref), stroke: 'var(--chart-limit)', 'stroke-dasharray': '4 4'}));
    const cx = i => left+gap+i*(bw+gap)+bw/2, points = [];
    days.forEach((d, i) => {
      const r = ranges[i], isToday = i === days.length-1, x = cx(i);
      if (r) {
        const w = Math.min(14, bw*.45), top0 = y(r.max), h = Math.max(w, y(r.min)-top0);
        const bar = svg('rect', {x: x-w/2, y: h === w ? y(r.avg)-w/2 : top0, width: w, height: h, rx: w/2, fill: opts.color, opacity: .28});
        bar.append(svg('title', {}, `${short(d)} 平均 ${num(r.avg, opts.digits)}・最低 ${num(r.min, opts.digits)}・最高 ${num(r.max, opts.digits)} ${opts.unit}（${r.n}件）`));
        g.append(bar); points.push([x, y(r.avg)]);
        g.append(svg('text', {x, y: Math.max(10, top0-5), 'text-anchor': 'middle', 'font-size': 9.5, fill: 'var(--chart-label)'}, num(r.avg, opts.digits)));
      }
      g.append(svg('text', {x, y: H-8, 'text-anchor': 'middle', 'font-size': 11, fill: isToday ? 'var(--text)' : 'var(--chart-label)', 'font-weight': isToday ? 700 : 400}, isToday ? '今日' : weekday(d)));
    });
    if (opts.line && points.length > 1) g.append(svg('polyline', {points: points.map(p => p.join(',')).join(' '), fill: 'none', stroke: opts.color, 'stroke-width': 2, 'stroke-linejoin': 'round'}));
    for (const [x, py] of points) g.append(svg('circle', {cx: x, cy: py, r: 3.6, fill: opts.color, stroke: 'var(--surface, #fff)', 'stroke-width': 1.5}));
    target.append(g);
    const legend = el('div', undefined, 'home-legend');
    legend.append(el('span', `● 平均${opts.line ? '（線）' : ''}`, 'vital-avg'), el('span', '▮ 最低〜最高', 'vital-range'));
    if (opts.refLabel) legend.append(el('span', '┄ '+opts.refLabel, 'goal-line'));
    legend.style.setProperty('--c', opts.color); target.append(legend);
  }

  function bars(target, days, values, opts) {
    target.replaceChildren();
    if (values.every(v => v == null)) { target.append(el('p', opts.empty, 'home-empty')); return; }
    const W = 340, H = 150, top = 14, bottom = 26, gap = 10, bw = (W-gap*(days.length+1))/days.length;
    const totals = values.map(v => v == null ? 0 : opts.total(v));
    const max = Math.max(opts.goal || 0, ...totals)*1.1 || 1;
    const y = v => H-bottom-(v/max)*(H-top-bottom);
    const g = svg('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': opts.label});
    if (opts.goal) { g.append(svg('line', {x1: 0, x2: W, y1: y(opts.goal), y2: y(opts.goal), stroke: 'var(--chart-limit)', 'stroke-dasharray': '4 4'})); }
    days.forEach((d, i) => {
      const x = gap+i*(bw+gap), v = values[i], isToday = i === days.length-1;
      if (v == null) g.append(svg('rect', {x, y: H-bottom-3, width: bw, height: 3, rx: 1.5, fill: 'var(--chart-grid)'}));
      else {
        let base = H-bottom;
        for (const part of opts.parts(v)) {
          const h = (part.value/max)*(H-top-bottom); if (h <= 0) continue;
          const r = svg('rect', {x, y: base-h, width: bw, height: h, rx: 3, fill: part.color, opacity: isToday || opts.stacked ? 1 : .78});
          r.append(svg('title', {}, `${short(d)} ${part.title}`)); g.append(r); base -= h;
        }
        g.append(svg('text', {x: x+bw/2, y: base-4, 'text-anchor': 'middle', 'font-size': 10, fill: 'var(--chart-label)'}, opts.short(v)));
      }
      g.append(svg('text', {x: x+bw/2, y: H-8, 'text-anchor': 'middle', 'font-size': 11, fill: isToday ? 'var(--text)' : 'var(--chart-label)', 'font-weight': isToday ? 700 : 400}, isToday ? '今日' : weekday(d)));
    });
    target.append(g);
    const legend = opts.legend || el('div', undefined, 'home-legend');
    if (opts.goal) legend.append(el('span', '┄ '+opts.goalLabel, 'goal-line'));
    target.append(legend);
  }

  let serial = 0;
  async function fetchSamples(user, since, until, metric) {
    const all = []; let total = 0;
    do {
      const p = new URLSearchParams({user_id: user, since: new Date(since).toISOString(), until: new Date(until).toISOString(), limit: '1000', offset: String(all.length)});
      if (metric) p.set('metric', metric);
      const response = await fetch('/api/apple-health/samples?'+p, {cache: 'no-store'}); const data = await response.json();
      if (!response.ok) throw Error(data.error || 'ヘルスケアの記録を取得できません');
      total = data.total; if (!data.samples.length) break; all.push(...data.samples);
    } while (all.length < total && all.length < 20000);
    return all;
  }

  function greeting(now) { const h = now.getHours(); return h < 4 ? 'こんばんは' : h < 11 ? 'おはようございます' : h < 18 ? 'こんにちは' : 'こんばんは'; }

  async function refreshHome() {
    const ticket = ++serial, now = new Date(), userId = $('current-user')?.value;
    $('home-date').textContent = now.toLocaleDateString('ja-JP', {month: 'long', day: 'numeric', weekday: 'short'}).toUpperCase();
    const name = userId ? (typeof currentUserName === 'function' ? currentUserName(userId) : '') : '';
    $('home-greeting').replaceChildren(document.createTextNode(greeting(now)+(name ? '、' : '')));
    if (name) { const em = el('em', name); $('home-greeting').append(em, document.createTextNode('さん')); }
    for (const id of ['home-diet', 'home-activity', 'home-sleep', 'home-heart', 'home-energy', 'home-steps-week', 'home-sleep-week', 'home-balance-week', 'home-heart-week', 'home-oxygen-week', 'home-eval-diet', 'home-eval-health']) if (!userId) empty($(id), '利用者を選択してください。');
    if (!userId) { renderAdviceCard(null); $('home-proposal-notice').hidden = true; renderSummary([], '利用者を選択すると、今日のまとめを表示します。'); $('home-source').textContent = ''; $('home-steps-avg').textContent = ''; $('home-sleep-avg').textContent = ''; $('home-balance-avg').textContent = ''; $('home-heart-avg').textContent = ''; $('home-oxygen-avg').textContent = ''; return; }
    const todayKey = dayKey(now), days = lastDays(todayKey, 7);
    let rows = [], error = null, metrics = null;
    const metricsRequest = fetch('/api/wellness/metrics?user_id='+encodeURIComponent(userId), {cache: 'no-store'}).then(r => r.ok ? r.json() : null).catch(() => null);
    // Fetch per metric group so frequent watch samples (heart rate) can never crowd out last night's sleep.
    try {
      const end = dayStart(todayKey)+DAY;
      const groups = await Promise.all([
        fetchSamples(userId, dayStart(days[0]), end, 'steps,walking_running_distance,active_energy'),
        fetchSamples(userId, dayStart(days[0])-6*3600000, end, 'sleep'),
        fetchSamples(userId, dayStart(days[0]), end, 'heart_rate,oxygen_saturation')]);
      rows = groups.flat();
    } catch (e) { error = e; }
    metrics = await metricsRequest;
    fetch('/api/wellness/advice?user_id='+encodeURIComponent(userId), {cache: 'no-store'}).then(r => r.ok ? r.json() : null)
      .then(data => { if (ticket === serial) renderAdviceCard(data?.latest || null); }).catch(() => renderAdviceCard(null));
    fetch('/api/wellness/plan-proposals?user_id='+encodeURIComponent(userId), {cache: 'no-store'}).then(r => r.ok ? r.json() : null)
      .then(data => { if (ticket === serial) $('home-proposal-notice').hidden = !data?.pending; }).catch(() => { $('home-proposal-notice').hidden = true; });
    if (ticket !== serial) return;
    const source = mainSource(rows.filter(r => r.metric !== 'sleep'));
    const sleepSource = mainSource(rows.filter(r => r.metric === 'sleep'));
    const totals = dailyTotals(perMetricMainSource(rows.filter(r => r.metric !== 'sleep')), days);
    const nights = Object.fromEntries(days.map(d => [d, sleepFor(rows.filter(r => r.source === sleepSource), d)]));
    const energy = typeof energyData !== 'undefined' && wellnessProfileUser === userId ? energyData : null;
    const summary = typeof wellnessSummaryData !== 'undefined' && wellnessProfileUser === userId ? wellnessSummaryData : null;
    STEP_GOAL = stepGoalOf(summary?.profile);
    const goalInput = $('home-goal-form').elements.step_goal; if (document.activeElement !== goalInput) goalInput.value = STEP_GOAL;
    $('home-goal-summary').textContent = `歩数目標 ${num(STEP_GOAL)} 歩を変更`;
    const todayEntry = energy ? (energy.entries || energy.diaries || []).find(x => x.date === energy.today) : null;
    $('home-source').textContent = error ? error.message : source ? `記録元：${source === 'unknown' ? '不明' : source}` : '';
    const diet = renderDiet(energy, summary);
    if (metrics) { renderEvaluations($('home-eval-diet'), metrics.diet); renderEvaluations($('home-eval-health'), metrics.health); }
    else for (const id of ['home-eval-diet', 'home-eval-health']) empty($(id), '評価を取得できませんでした。');
    renderActivity(totals[todayKey], todayEntry);
    const sleptDays = days.filter(d => nights[d]);
    const sleepAvg = sleptDays.length ? sleptDays.reduce((a, d) => a+nights[d].total, 0)/sleptDays.length : null;
    const md = key => `${Number(key.slice(5, 7))}/${Number(key.slice(8, 10))}`;
    const sleepDay = nights[todayKey] ? todayKey : latestDay(days, d => nights[d]);
    renderSleep(sleepDay ? nights[sleepDay] : null, sleptDays.length > 1 ? sleepAvg : null, sleepDay && sleepDay !== todayKey ? md(sleepDay) : '');
    const hasVitals = d => totals[d] && (totals[d].heart.length || totals[d].oxygen.length);
    const vitalDay = hasVitals(todayKey) ? todayKey : latestDay(days, hasVitals);
    renderHeart(vitalDay ? totals[vitalDay] : null, vitalDay && vitalDay !== todayKey ? md(vitalDay) : '');
    renderEnergy(energy, totals[todayKey].active_energy, true);
    const history = new Map((energy?.history || []).map(x => [x.date, x]));
    const balanceRows = days.map(d => { const h = history.get(d); const intake = h?.intake_kcal ?? (d === energy?.today ? energy?.current?.intake_kcal : null);
      const r = energyBalance(h?.bmr ?? (d === energy?.today ? energy?.current?.bmr : null), totals[d].active_energy, intake);
      return {intake, burn: r.burn, balance: r.basis === 'bmr+active' ? r.balance : null}; });
    balanceBars($('home-balance-week'), days, balanceRows);
    const done = balanceRows.slice(0, -1).filter(r => r.balance != null);
    $('home-balance-avg').textContent = done.length ? (avg => `1日平均 ${avg > 0 ? '+' : avg < 0 ? '−' : '±'}${num(Math.abs(avg))} kcal`)(done.reduce((a, r) => a+r.balance, 0)/done.length) : '';
    const stepDays = days.filter(d => totals[d].steps != null && d !== todayKey);
    $('home-steps-avg').textContent = stepDays.length ? `1日平均 ${num(stepDays.reduce((a, d) => a+totals[d].steps, 0)/stepDays.length)} 歩` : '';
    $('home-sleep-avg').textContent = sleepAvg == null ? '' : `平均 ${hm(sleepAvg)}`;
    bars($('home-steps-week'), days, days.map(d => totals[d].steps), {label: '直近7日の歩数', empty: '歩数の記録はありません。', goal: STEP_GOAL, goalLabel: `目標 ${num(STEP_GOAL)}`,
      total: v => v, parts: v => [{value: v, color: v >= STEP_GOAL ? 'var(--activity)' : 'var(--activity-soft)', title: `${num(v)} 歩`}], short: v => v >= 1000 ? (v/1000).toFixed(1)+'k' : String(Math.round(v))});
    const legend = el('div', undefined, 'home-legend'); for (const s of ['deep', 'core', 'rem']) legend.append(el('span', STAGE_NAMES[s], 'stage-label stage-'+s));
    bars($('home-sleep-week'), days, days.map(d => nights[d]), {label: '直近7日の睡眠時間', empty: '睡眠の記録はありません。', goal: 7*3600000, goalLabel: '7時間', stacked: true, legend,
      total: v => v.total, parts: v => SLEEP_STAGES.map(s => ({value: v.stages[s], color: `var(--sleep-${s})`, title: `${STAGE_NAMES[s]} ${hm(v.stages[s])}`})), short: v => (v.total/3600000).toFixed(1)+'h'});
    const heartDays = days.map(d => vitalRange(totals[d].heart)), oxygenDays = days.map(d => vitalRange(totals[d].oxygen));
    const avgOf = list => { const k = list.filter(Boolean); return k.length ? k.reduce((a, r) => a+r.avg, 0)/k.length : null; };
    $('home-heart-avg').textContent = avgOf(heartDays) == null ? '' : `平均 ${num(avgOf(heartDays))} bpm`;
    $('home-oxygen-avg').textContent = avgOf(oxygenDays) == null ? '' : `平均 ${num(avgOf(oxygenDays))} %`;
    rangeChart($('home-heart-week'), days, heartDays, {label: '直近7日の心拍数', empty: '心拍数の記録はありません。', unit: 'bpm', color: 'var(--heart)', line: true, minPad: 4});
    rangeChart($('home-oxygen-week'), days, oxygenDays, {label: '直近7日の血中酸素', empty: '血中酸素の記録はありません。', unit: '%', color: 'var(--oxygen)', line: true, ref: 95, refLabel: '目安 95%', ceil: 100, minPad: 1});
    const latest = kind => (typeof chartRecords !== 'undefined' ? chartRecords : []).find(r => r.kind === kind && r.user_id === userId);
    const measuredToday = kind => { const r = latest(kind); return !!r && dayKey(Date.parse(r.measured_at)) === todayKey; };
    const steps = totals[todayKey].steps, night = nights[todayKey], profile = summary?.profile;
    const bp = latest('blood_pressure'), weight = latest('body_composition');
    const bpHigh = !!(bp && profile && (bp.values.systolic > profile.bp_sys_high || bp.values.diastolic > profile.bp_dia_high));
    const today = energyBalance(energy?.current?.bmr, totals[todayKey].active_energy, energy?.current?.intake_kcal);
    const tick = v => v ? '✓' : '';
    renderSummary([
      {label: '体重', value: weight ? num(weight.values.weight, 1) : '—', unit: 'kg', mark: tick(measuredToday('body_composition')),
        sub: diet?.remaining != null ? (diet.remaining <= 0 ? '目標に到達' : `目標まで ${num(diet.remaining, 1)} kg`) : measuredToday('body_composition') ? '今日測定済み' : '今日は未測定',
        state: measuredToday('body_composition') ? 'done' : ''},
      {label: '血圧', value: bp ? `${bp.values.systolic}/${bp.values.diastolic}` : '—', unit: 'mmHg', mark: tick(measuredToday('blood_pressure')),
        sub: bpHigh ? '参考上限を超えています' : measuredToday('blood_pressure') ? '今日測定済み' : '今日は未測定', state: bpHigh ? 'warn' : measuredToday('blood_pressure') ? 'done' : ''},
      {label: '歩数', value: num(steps), unit: '歩', mark: tick(steps != null && steps >= STEP_GOAL),
        sub: `目標 ${num(STEP_GOAL)}${steps != null ? `（${Math.min(999, Math.round(steps/STEP_GOAL*100))}%）` : ''}`, state: steps != null && steps >= STEP_GOAL ? 'done' : ''},
      {label: '睡眠', value: night ? hm(night.total).replace('時間', 'h').replace('分', 'm') : '—', unit: '', mark: tick(!!night),
        sub: night ? (sleptDays.length > 1 ? `平均 ${hm(sleepAvg)}` : '昨夜の記録') : '記録なし', state: night ? 'done' : ''},
      {label: '収支', value: today.balance == null ? '—' : `${today.balance > 0 ? '+' : today.balance < 0 ? '−' : '±'}${num(Math.abs(today.balance))}`, unit: 'kcal',
        sub: today.basis === 'bmr+active' ? '摂取−基礎代謝−活動' : today.basis === 'bmr' ? '摂取−基礎代謝' : '設定が必要', state: today.balance == null ? '' : today.balance <= 0 ? 'good' : 'warn'}
    ], summaryText({remaining: diet?.remaining ?? null, change7: diet?.change7 ?? null, bpHigh, steps, stepGoal: STEP_GOAL,
      sleep: night?.total ?? null, sleepAvg: sleptDays.length > 1 ? sleepAvg : null, balance: today.balance, evaluations: metrics?.summary || []}));
  }
  $('home-goal-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget, button = form.querySelector('button'), userId = $('current-user').value;
    if (!userId) { message('利用者を選択してください', true); return; }
    button.disabled = true;
    try {
      await api('/api/wellness/profile', {user_id: userId, profile: {step_goal: Number(form.elements.step_goal.value)}});
      await refreshWellness(); $('home-goal-edit').open = false; await refreshHome();
      message('歩数目標を保存しました');
    } catch (error) { message(error.message, true); } finally { button.disabled = false; }
  });
  window.refreshHome = refreshHome;
})();
