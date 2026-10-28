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
