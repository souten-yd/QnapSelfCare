/* Explicit QPKG updates; persisted server state remains authoritative. */
(() => {
  const el = id => document.getElementById(id);
  let state = null, target = null, checking = false, submitting = false, uncertain = false;
  async function request(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(path, {method: body ? 'POST' : 'GET', signal: controller.signal,
        headers: body ? {'Content-Type':'application/json','X-SelfCare-Request':'1'} : {},
        body: body ? JSON.stringify(body) : undefined});
      const value = await response.json();
      if (!response.ok) {const error = new Error(value.error || '更新処理に失敗しました');error.http = true;throw error;}
      return value;
    } finally {clearTimeout(timer);}
  }
  function render() {
    const busy = Boolean(state?.busy || submitting || uncertain);
    el('check-update').disabled = busy || checking;
    el('apply-update').textContent = target ? `${target} に更新する` : '更新する';
    el('apply-update').disabled = busy || checking || !state?.supported || !target;
    const phases = {queued:'待機中',checking:'更新対象確認',downloading:'取得・検証中',backup:'バックアップ中',installing:'インストール中',verifying:'再起動確認中',success:'更新完了',failed:'更新失敗'};
    el('update-progress').textContent = state && state.phase !== 'idle'
      ? `${phases[state.phase] || state.phase}：${state.message || ''}${state.busy && state.phase === 'failed' ? '（インストーラー終了待ち）' : ''}` : '';
    el('update-support').textContent = state && !state.supported ? state.unsupported_reason : '';
    el('reload-update').hidden = state?.phase !== 'success' || state.busy;
    el('update-backup').textContent = state?.backup ? `更新前バックアップ：${state.backup}` : '';
  }
  async function refresh() {
    try {
      state = await request('/api/update/status');
      uncertain = false;
      if (state.phase === 'success' && target === state.target) target = null;
      render();
      if (el('update-log-panel').open) await refreshLog();
      return true;
    } catch {
      if (state?.busy || submitting || uncertain) {
        uncertain = true;
        el('update-progress').textContent = '再接続を待っています。更新は自動で再実行しません。';
        el('apply-update').disabled = true;
      }
      return false;
    }
  }
  async function refreshLog() {
    try {el('update-log').textContent = (await request('/api/update/log')).log || '更新ログはまだありません';}
    catch {el('update-log').textContent = '更新ログに再接続できません。App Centerの起動状態を確認してください。';}
  }
  el('check-update').addEventListener('click', async () => {
    if (checking || submitting || state?.busy || uncertain) return;
    checking = true; target = null;render();el('update-message').textContent = '更新を確認中…';
    try {
      const result = await request('/api/update');
      target = result.available ? result.version : null;
      el('update-message').textContent = target ? `${target} が利用できます。ボタンを押すとNASが取得・検証・適用します。` : '現在のバージョンが最新です';
      await refresh();
    } catch (error) {el('update-message').textContent = `更新確認に失敗しました：${error.message}`;}
    finally {checking = false;render();}
  });
  el('apply-update').addEventListener('click', async () => {
    if (!target || !state?.supported || state.busy || submitting || uncertain) return;
    if (!confirm(`${target} に更新しますか？\n更新前にDBをバックアップし、Webサービスを再起動します。`)) return;
    submitting = true;render();el('update-message').textContent = '更新を受け付けています…';
    try {
      state = {...state, ...await request('/api/update/apply', {version:target})};
      el('update-message').textContent = '更新を開始しました。この画面で進捗を確認できます。';
    } catch (error) {
      uncertain = !error.http;
      el('update-message').textContent = error.http ? `更新開始失敗：${error.message}` : '接続が切れました。受付済みの可能性があるため状態を再確認します。';
    } finally {submitting = false;await refresh();render();}
  });
  el('reload-update').addEventListener('click', () => location.reload());
  el('refresh-update-log').addEventListener('click', refreshLog);
  el('update-log-panel').addEventListener('toggle', () => {if (el('update-log-panel').open) void refreshLog();});
  async function poll() {
    if (!document.hidden || !state || state.busy || uncertain) await refresh();
    setTimeout(poll, state?.busy || uncertain ? 3000 : 15000);
  }
  void poll();
})();
