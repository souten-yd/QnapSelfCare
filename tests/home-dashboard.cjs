// Pure calculations behind the Home dashboard (no DOM, no network).
process.env.TZ = 'Asia/Tokyo';
const assert = require('node:assert/strict');
const h = require('../web/home.js');

assert.deepEqual(h.lastDays('2026-03-02', 3), ['2026-02-28', '2026-03-01', '2026-03-02']);
assert.equal(h.union([[0, 10], [5, 20], [30, 40]]), 30);
assert.equal(h.mainSource([{source: 'Watch'}, {source: 'Phone'}, {source: 'Watch'}]), 'Watch');
assert.equal(h.mainSource([]), null);

// Totals use the start day in local time; missing days stay null (not zero).
const days = ['2026-10-27', '2026-10-28'];
const t = h.dailyTotals([
  {metric: 'steps', start_at: '2026-10-27T23:50:00+09:00', value: 100},
  {metric: 'steps', start_at: '2026-10-28T08:00:00+09:00', value: 200},
  {metric: 'steps', start_at: '2026-10-28T09:00:00+09:00', value: 300},
  {metric: 'walking_running_distance', start_at: '2026-10-28T09:00:00+09:00', value: 1500},
  {metric: 'heart_rate', start_at: '2026-10-28T09:00:00+09:00', value: 70},
], days);
assert.equal(t['2026-10-27'].steps, 100);
assert.equal(t['2026-10-28'].steps, 500);
assert.equal(t['2026-10-28'].distance, 1.5);
assert.equal(t['2026-10-28'].active_energy, null);
assert.deepEqual(t['2026-10-28'].heart, [70]);

// Sleep: 18:00 previous day to 18:00, excluding awake/in-bed, overlaps counted once.
const sleep = [
  {metric: 'sleep', start_at: '2026-10-27T23:00:00+09:00', end_at: '2026-10-28T01:00:00+09:00', value: 'core'},
  {metric: 'sleep', start_at: '2026-10-28T00:30:00+09:00', end_at: '2026-10-28T02:00:00+09:00', value: 'deep'},
  {metric: 'sleep', start_at: '2026-10-28T02:00:00+09:00', end_at: '2026-10-28T02:30:00+09:00', value: 'awake'},
  {metric: 'sleep', start_at: '2026-10-28T02:30:00+09:00', end_at: '2026-10-28T03:00:00+09:00', value: 'rem'},
  {metric: 'sleep', start_at: '2026-10-27T13:00:00+09:00', end_at: '2026-10-27T14:00:00+09:00', value: 'core'},
];
const night = h.sleepFor(sleep, '2026-10-28');
assert.equal(night.total, 3.5 * 3600000);
assert.equal(night.stages.deep, 1.5 * 3600000);
assert.equal(night.stages.rem, 0.5 * 3600000);
assert.equal(h.sleepFor(sleep.filter(r => r.value === 'awake'), '2026-10-28'), null);

// Diet progress against the active plan, clamped to 0-100%.
const weights = [{date: '2026-10-01', weight: 80}, {date: '2026-10-20', weight: 78.4}, {date: '2026-10-28', weight: 77.5}];
const plan = {start_date: '2026-10-01', start_weight: 80, goal_date: '2026-12-31', goal_weight: 75};
const p = h.dietProgress(weights, plan, null);
assert.equal(p.percent, 50);
assert.equal(p.remaining, 2.5);
assert.equal(p.change7, -0.9);
assert.equal(h.dietProgress([{date: '2026-10-28', weight: 81}], plan, null).percent, 0);
assert.equal(h.dietProgress(weights, null, null).percent, null);
assert.equal(h.dietProgress(weights, null, 76).percent, 63);
assert.equal(h.dietProgress([], plan, null), null);
console.log('Home dashboard calculations: passed');
assert.equal(h.stepGoalOf(null), 8000);
assert.equal(h.stepGoalOf({step_goal: 10000}), 10000);
assert.equal(h.stepGoalOf({step_goal: 'x'}), 8000);
assert.equal(h.stepGoalOf({step_goal: 500}), 8000);
console.log('Home step goal: passed');
assert.deepEqual(h.energyBalance(1600, 450, 1900), {burn: 2050, balance: -150, basis: 'bmr+active'});
assert.deepEqual(h.energyBalance(1600, null, 1900), {burn: 1600, balance: 300, basis: 'bmr'});
assert.deepEqual(h.energyBalance(1600, 450.6, null), {burn: 2051, balance: null, basis: 'bmr+active'});
assert.deepEqual(h.energyBalance(null, 450, 1900), {burn: null, balance: null, basis: null});
console.log('Home energy balance: passed');
assert.equal(h.summaryText({remaining: 3.4, change7: -0.5, bpHigh: false, steps: 3111, stepGoal: 10000, sleep: 6.9*3600000, sleepAvg: 7.5*3600000, balance: 125}),
  '目標体重まであと3.4kg（1週間で0.5kg減）。歩数は目標まであと6,889歩。睡眠は平均より36分短め。今日の収支は+125kcal（摂取が上回っています）。');
assert.equal(h.summaryText({remaining: 0, change7: null, bpHigh: true, steps: 12000, stepGoal: 8000, sleep: null, sleepAvg: null, balance: -300}),
  '目標体重に到達しています。血圧が参考上限を超えています。歩数は目標を達成。昨夜の睡眠記録はありません。今日の収支は−300kcal。');
assert.equal(h.summaryText({remaining: null, change7: null, bpHigh: false, steps: null, stepGoal: 8000, sleep: 7*3600000, sleepAvg: null, balance: null}), '記録がそろうと、ここに今日のまとめを表示します。');
console.log('Home summary text: passed');
assert.equal(h.summaryText({remaining: 3.4, change7: null, bpHigh: true, steps: null, stepGoal: 8000, sleep: 7*3600000, sleepAvg: null, balance: null,
  evaluations: ['減量ペースは週0.41kg減で適正。', '家庭血圧の7日平均は127/81で高値血圧。']}),
  '目標体重まであと3.4kg。減量ペースは週0.41kg減で適正。家庭血圧の7日平均は127/81で高値血圧。');
console.log('Home summary evaluations: passed');
assert.equal(h.adviceHighlights('## 要点\n- よく歩けました\n## 提案\n- 早く寝る'), '- よく歩けました');
assert.equal(h.adviceHighlights('前置きのみ'), '前置きのみ');
console.log('Home advice highlights: passed');
const mixed = [{metric: 'steps', source: 'iPhone'}, {metric: 'steps', source: 'iPhone'}, {metric: 'steps', source: 'Watch'},
  {metric: 'heart_rate', source: 'Watch'}, {metric: 'oxygen_saturation', source: 'Watch'}];
assert.deepEqual(h.perMetricMainSource(mixed).map(r => r.metric + '@' + r.source),
  ['steps@iPhone', 'steps@iPhone', 'heart_rate@Watch', 'oxygen_saturation@Watch']);   // watch-only vitals are kept
console.log('Home per-metric source: passed');
const inBed = [{metric: 'sleep', value: 'in_bed', start_at: '2026-10-28T23:00:00', end_at: '2026-10-29T06:30:00', source: 'HUAWEI'}];
const bedNight = h.sleepFor(inBed, '2026-10-29');
assert.equal(bedNight.inBedOnly, true);
assert.equal(bedNight.total, 7.5 * 3600000);
assert.equal(h.sleepFor([{...inBed[0], value: 'core'}, inBed[0]], '2026-10-29').inBedOnly, undefined);   // real stages win
assert.equal(h.latestDay(['a', 'b', 'c'], d => d !== 'c'), 'b');
assert.equal(h.latestDay(['a'], () => false), null);
console.log('Home in-bed sleep and latest-day fallback: passed');

// Daily vital ranges for the 7-day heart rate / blood oxygen charts.
assert.equal(h.vitalRange([]), null);
assert.equal(h.vitalRange(undefined), null);
assert.deepEqual(h.vitalRange([60, 80, 70]), {min: 60, max: 80, avg: 70, n: 3});
