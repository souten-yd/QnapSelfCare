const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
(async () => {
  const dom = new JSDOM(fs.readFileSync('web/index.html', 'utf8'), {url: 'http://nas:17863', runScripts: 'outside-only'});
  const w = dom.window;
  const calls = [];
  const attack = '<img src=x onerror=alert(1)>';
  w.fetch = async (url, options) => {
    calls.push({url, options});
    return {ok: true, json: async () => ({enabled: true, remaining_seconds: 600, entries: [
      {received_at: 'now', status: 400, result: {error: 'bad'}, body_preview: attack, body_bytes: 12, truncated: true}
    ]})};
  };
  w.eval(fs.readFileSync('web/health-debug.js', 'utf8'));
  assert.equal(calls.length, 0, 'no background polling at load');
  assert.equal(w.document.getElementById('health-debug-panel').open, false);
  assert.equal(w.document.getElementById('health-import-url').textContent, 'http://nas:17863/api/apple-health/import');
  w.document.getElementById('health-debug-start').click();
  await new Promise(r => setTimeout(r, 10));
  assert.equal(calls[0].options.headers['X-SelfCare-Request'], '1');
  assert.deepEqual(JSON.parse(calls[0].options.body), {action: 'start'});
  const entries = w.document.getElementById('health-debug-entries');
  assert.equal(entries.querySelector('img'), null, 'payload rendered as text, never HTML');
  assert(entries.textContent.includes(attack));
  assert(entries.textContent.includes('8 KiB'));
  w.fetch = async () => {throw new Error('offline');};
  w.document.getElementById('health-debug-refresh').click();
  await new Promise(r => setTimeout(r, 10));
  assert(w.document.getElementById('health-debug-status').textContent.includes('offline'));
  assert.equal(w.document.getElementById('health-debug-refresh').disabled, false);
  dom.window.close();
  console.log('health debug UI: passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
