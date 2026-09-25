const statusMessage = document.getElementById('update-message');
async function getJSON(path) {
  const response = await fetch(path, {cache: 'no-store'});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '取得に失敗しました');
  return data;
}
getJSON('/api/status').then(data => {
  document.getElementById('version').textContent = data.version;
  document.getElementById('arch').textContent = data.architecture || '未対応';
  document.getElementById('bp-state').textContent = data.devices['HEM-6232T'];
  document.getElementById('scale-state').textContent = data.devices['HBF-228T'];
}).catch(() => { statusMessage.textContent = 'システム状態を取得できませんでした。'; });
document.getElementById('check-update').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  statusMessage.textContent = 'GitHub Releases を確認中…';
  try {
    const data = await getJSON('/api/update');
    statusMessage.replaceChildren();
    if (data.available) {
      statusMessage.append(document.createTextNode(`新しいバージョン ${data.version} が利用できます。 `));
      const link = document.createElement('a');
      link.href = data.url;
      link.rel = 'noopener noreferrer';
      link.textContent = 'QPKGをダウンロード';
      statusMessage.append(link);
    } else {
      statusMessage.textContent = '現在のバージョンが最新です。';
    }
  } catch (error) {
    statusMessage.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
