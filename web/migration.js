(() => {
  'use strict';
  const button = document.getElementById('migrate-homehub');
  const restore = document.getElementById('restore-homehub');
  const message = document.getElementById('migration-message');
  let requesting = false;
  async function request(body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const options = {signal: controller.signal, cache: 'no-store'};
      if (body) Object.assign(options, {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-SelfCare-Request': '1'}, body: JSON.stringify(body)});
      const response = await fetch('/api/homehub-migration', options);
      const data = await response.json();
      if (!response.ok) throw Error(data.error || '移行処理に失敗しました');
      return data;
    } finally { clearTimeout(timeout); }
  }
  function render(data) {
    button.disabled = requesting || data.busy || data.can_restore;
    restore.hidden = !data.can_restore;
    restore.disabled = requesting || data.busy;
    message.textContent = data.message || '共通Bluetoothサービスが未接続の場合は、上のボタンで構成を更新できます。';
    document.getElementById('migration-backup').textContent = data.backup ? '設定の退避先: ' + data.backup : '';
  }
  async function load() {
    if (requesting) return;
    try { render(await request()); }
    catch (error) { message.textContent = '状態を確認できません: ' + error.message; }
  }
  async function apply(action) {
    if (requesting) return;
    requesting = true;
    button.disabled = restore.disabled = true;
    message.textContent = '処理を受け付けています…';
    try {
      const data = await request({action});
      requesting = false;
      render(data);
    } catch (error) {
      requesting = false;
      message.textContent = '受付状態を再確認します: ' + error.message;
      // Poll status before enabling another submission after an uncertain response.
    }
  }
  button.addEventListener('click', () => apply('migrate'));
  restore.addEventListener('click', () => apply('restore'));
  load();
  setInterval(() => { if (!document.hidden) load(); }, 5000);
})();
