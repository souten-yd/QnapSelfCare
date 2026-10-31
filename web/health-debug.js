(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const panel = byId('health-debug-panel');
  const status = byId('health-debug-status');
  const entries = byId('health-debug-entries');
  const buttons = [...panel.querySelectorAll('button')];
  let busy = false;
  let expiryTimer;
  byId('health-import-url').textContent = location.origin + '/api/apple-health/import';
  async function request(action) {
    const response = await fetch('/api/apple-health/debug', action ? {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-SelfCare-Request': '1'},
      body: JSON.stringify({action})
    } : {});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }
  function render(data) {
    clearTimeout(expiryTimer);
    if (data.enabled) expiryTimer = setTimeout(() => {
      entries.replaceChildren();
      status.textContent = '記録期限に達しました。記録済みの全文をダウンロードできます。';
    }, (data.remaining_seconds + 1) * 1000);
    status.textContent = data.enabled
      ? `記録中：残り約${data.remaining_seconds}秒・${data.total_entries ?? data.entries.length}件（この表示は更新時点）`
      : `停止中：記録${data.total_entries ?? data.entries.length}件。全文をダウンロードできます`;
    if (data.omitted) status.textContent += `・容量／件数上限により${data.omitted}件の記録または本文が欠落しています`;
    entries.replaceChildren();
    if (!data.entries.length) {
      const empty = document.createElement('p');
      empty.textContent = '受信ログはありません。記録開始後にiPhoneから送信し、「受信状況を更新」を押してください。';
      entries.append(empty);
    }
    [...data.entries].reverse().forEach(entry => {
      const detail = document.createElement('details');
      const summary = document.createElement('summary');
      const secs = entry.elapsed_ms == null ? '' : ` · ${(entry.elapsed_ms / 1000).toFixed(1)}秒`;
      summary.textContent = entry.status === 'receiving'
        ? `${entry.received_at} · 受信・保存中${entry.declared_bytes ? ` · ${entry.declared_bytes} bytes` : ''}${secs}`
        : `${entry.received_at} · HTTP ${entry.status} · ${entry.body_bytes ?? '本文未取得'} bytes${secs}`;
      const result = document.createElement('p');
      result.textContent = JSON.stringify(entry.result);
      const preview = document.createElement('pre');
      preview.style.whiteSpace = 'pre-wrap';
      preview.style.overflowWrap = 'anywhere';
      preview.textContent = entry.body_preview || '本文は取得されていません（ヘッダー不備など）';
      detail.append(summary, result, preview);
      if (entry.truncated) {
        const notice = document.createElement('p');
        notice.textContent = '本文プレビューは先頭8 KiBまでです。全文ダウンロードには受信本文全体を含みます（容量上限時は欠落を表示）。';
        detail.append(notice);
      }
      entries.append(detail);
    });
  }
  async function run(action) {
    if (busy) return;
    busy = true;
    buttons.forEach(button => { button.disabled = true; });
    try { await action(); }
    catch (error) { status.textContent = '取得・操作に失敗しました: ' + error.message; }
    finally { busy = false; buttons.forEach(button => { button.disabled = false; }); }
  }
  ['start', 'stop', 'clear'].forEach(action => {
    byId('health-debug-' + action).addEventListener('click', () => run(async () => render(await request(action))));
  });
  byId('health-debug-refresh').addEventListener('click', () => run(async () => render(await request())));
  byId('health-debug-download').addEventListener('click', () => run(async () => {
    const response = await fetch('/api/apple-health/debug/download');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = 'selfcare-shortcuts-debug.json';
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }));
  panel.addEventListener('toggle', () => {
    if (panel.open) run(async () => render(await request()));
    else entries.replaceChildren();
  });
})();
