const {JSDOM} = require('jsdom');
const fs = require('node:fs');
const assert = require('node:assert/strict');
async function until(check) {
  for (let i = 0; i < 100; i++) {
    if (check()) return;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  throw Error('Protection UI timed out');
}
(async () => {
  const dom = new JSDOM(fs.readFileSync('web/index.html', 'utf8'), {url: 'http://nas:17863', runScripts: 'outside-only', pretendToBeVisual: true});
  const w = dom.window, d = w.document, calls = [];
  let poll;
  let state = {settings: {enabled: true, interval_hours: 24, keep: 14}, busy: false, phase: 'idle',
    last_success: 1700000000, next_at: 1700086400, directory: '/data/backups', recovery_required: false,
    backups: [{name: 'selfcare-20260925T000000Z-aaaaaaaa.zip', created_at: 1700000000, size: 50000,
      counts: {records: 123}, readable: true, reason: 'automatic'}]};
  w.setInterval = fn => { poll = fn; return 0; };
  w.fetch = async (path, options) => {
    calls.push({path, options});
    if (path.endsWith('/backup')) state = {...state, busy: true, phase: 'running'};
    if (path.endsWith('/settings')) state.settings = JSON.parse(options.body);
    if (path.endsWith('/verify')) state = {...state, busy: true, phase: 'verifying'};
    return {ok: true, json: async () => structuredClone(state)};
  };
  try {
    w.eval(fs.readFileSync('web/protection.js', 'utf8'));
    await until(() => d.getElementById('backup-list').textContent.includes('123件'));
    assert.equal(calls.filter(c => c.options.method === 'POST').length, 0);
    const link = d.querySelector('#backup-list a');
    assert.match(link.href, /api\/protection\/download\?name=selfcare-/);
    const form = d.getElementById('protection-form');
    form.elements.keep.value = '30';
    await poll();
    assert.equal(form.elements.keep.value, '30', 'polling must not overwrite unsaved edits');
    form.elements.interval_hours.value = '6';
    form.dispatchEvent(new w.Event('submit', {cancelable: true}));
    await until(() => d.getElementById('protection-message').textContent.includes('設定を保存'));
    assert.deepEqual(state.settings, {enabled: true, interval_hours: 6, keep: 30});
    d.getElementById('backup-now').click();
    d.getElementById('backup-now').click();
    await until(() => d.getElementById('protection-message').textContent.includes('作成・検証'));
    assert.equal(calls.filter(c => c.path.endsWith('/backup')).length, 1);
    assert.equal(calls.find(c => c.path.endsWith('/backup')).options.headers['X-SelfCare-Request'], '1');
    state = {...state, busy: false, phase: 'idle', recovery_required: true, database_error: 'corrupt'};
    await poll();
    await until(() => !d.getElementById('data-alert').hidden);
    assert.equal(d.getElementById('backup-now').disabled, true);
    assert.equal(d.getElementById('verify-backup').disabled, false, 'verification remains available in recovery mode');
    assert.ok(d.querySelector('#backup-list a'), 'download remains available in recovery mode');
    d.getElementById('verify-backup').click();
    await until(() => d.getElementById('protection-message').textContent.includes('保存済みバックアップを検査'));
    state = {...state, busy: false, phase: 'idle', verification: {name: state.backups[0].name,
      checked_at: 1700000200, ok: false, error: '<script>broken</script>'}};
    await poll();
    await until(() => d.getElementById('backup-verification').textContent.includes('検査に失敗'));
    assert.equal(d.querySelector('#backup-verification script'), null);
    console.log('Protection UI: settings, manual backup, duplicate guard, protected-mode downloads and re-verification: passed');
  } finally { w.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
