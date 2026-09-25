/* Independent of record loading: remains available when the DB is protected. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const form = byId('protection-form');
  const nowButton = byId('backup-now');
  const verifyButton = byId('verify-backup');
  let initialized = false, loading = false;
  function node(tag, text) {
    const result = document.createElement(tag);
    result.textContent = text;
    return result;
  }
  const date = seconds => seconds ? new Date(seconds * 1000).toLocaleString('ja-JP') : '未実施';
  async function request(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const options = {cache: 'no-store', signal: controller.signal};
      if (body !== undefined) Object.assign(options, {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-SelfCare-Request': '1'}, body: JSON.stringify(body)});
      const response = await fetch(path, options);
      const data = await response.json();
      if (!response.ok) throw Error(data.error || 'バックアップ処理に失敗しました');
      return data;
    } finally { clearTimeout(timer); }
  }
  function render(data) {
    if (!initialized) {
      form.elements.enabled.checked = data.settings.enabled;
      form.elements.interval_hours.value = String(data.settings.interval_hours);
      form.elements.keep.value = data.settings.keep;
      initialized = true;
    }
    nowButton.disabled = data.busy || data.recovery_required;
    verifyButton.disabled = data.busy || !data.backups.some(b => b.readable);
    byId('backup-verification').textContent = data.verification ?
      `${date(data.verification.checked_at)} · ${data.verification.name} · ${data.verification.ok ? '再検査は正常です' : '検査に失敗しました: ' + data.verification.error}` : '';
    byId('protection-message').textContent = data.config_error || data.error || data.warning ||
      (data.busy ? (data.phase === 'verifying' ? '保存済みバックアップを検査しています…' : 'バックアップを作成・検証しています…') : data.last_success ? '検証済みバックアップを保存しました。' : '初回バックアップを待っています。');
    const rows = {'定期保存': data.settings.enabled ? '有効' : '無効', '前回の成功': date(data.last_success),
      '次回の予定': data.next_at === null ? '停止中' : data.recovery_required ? '保護モードのため保留' :
        data.next_at <= Date.now() / 1000 ? 'まもなく実行' : date(data.next_at), '保存先': data.directory};
    byId('protection-status').replaceChildren();
    for (const [label, value] of Object.entries(rows)) byId('protection-status').append(node('dt', label), node('dd', value));
    const alert = byId('data-alert');
    alert.textContent = data.recovery_required ? 'データを保護しています。保存・新規Bluetooth収集は停止しています。設定・バックアップの診断と復旧手順を確認してください。' :
      data.config_error || data.error || (data.verification && !data.verification.ok ? '保存済みバックアップの再検査に失敗しました。設定・バックアップを確認してください。' : '') || (!data.settings.enabled ? '定期バックアップは無効です。設定・バックアップから有効にできます。' : '');
    alert.hidden = !alert.textContent;
    byId('backup-list').replaceChildren();
    if (!data.backups.length) byId('backup-list').append(node('p', 'まだ保存世代がありません。'));
    for (const backup of data.backups) {
      const row = node('div', ''); row.className = 'backup-row';
      row.append(node('strong', backup.readable ? date(backup.created_at) : 'この保存世代は読めません'));
      row.append(node('small', backup.name));
      if (backup.readable) {
        row.append(node('p', `${backup.counts?.records ?? '—'}件 · ${(backup.size / 1024 / 1024).toFixed(2)} MB · ${backup.reason === 'automatic' ? '定期保存' : '手動保存'}`));
        const link = node('a', '検証してダウンロード');
        link.href = '/api/protection/download?name=' + encodeURIComponent(backup.name);
        link.className = 'button secondary';
        row.append(link);
      }
      byId('backup-list').append(row);
    }
  }
  async function load() {
    if (loading) return;
    loading = true;
    try { render(await request('/api/protection')); }
    catch (error) { byId('protection-message').textContent = '状態を取得できません: ' + error.message; }
    finally { loading = false; }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('[type=submit]');
    button.disabled = true;
    try {
      const data = await request('/api/protection/settings', {enabled: form.elements.enabled.checked,
        interval_hours: Number(form.elements.interval_hours.value), keep: Number(form.elements.keep.value)});
      initialized = false;
      render(data);
      byId('protection-message').textContent = 'バックアップ設定を保存しました。';
    } catch (error) { byId('protection-message').textContent = error.message; }
    finally { button.disabled = false; }
  });
  nowButton.addEventListener('click', async () => {
    nowButton.disabled = true;
    try { render(await request('/api/protection/backup', {})); }
    catch (error) {
      byId('protection-message').textContent = '状態を再取得してください。同じ処理は重複実行しません: ' + error.message;
      await load();
    }
  });
  verifyButton.addEventListener('click', async () => {
    verifyButton.disabled = true;
    try { render(await request('/api/protection/verify', {})); }
    catch (error) { byId('protection-message').textContent = error.message; await load(); }
  });
  load();
  setInterval(() => { if (!document.hidden) load(); }, 5000);
})();
