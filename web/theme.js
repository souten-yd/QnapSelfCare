/* Apply the saved appearance before the first paint. No server settings needed. */
(() => {
  const key = 'qnapselfcare-theme';
  const valid = value => ['system', 'light', 'dark'].includes(value);
  const system = window.matchMedia?.('(prefers-color-scheme: dark)');
  let mode = 'system';
  try {
    const saved = localStorage.getItem(key);
    if (valid(saved)) mode = saved;
  } catch {}

  function apply() {
    const dark = mode === 'dark' || (mode === 'system' && system?.matches);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', dark ? '#101a19' : '#f5f8f7');
  }
  apply();
  system?.addEventListener('change', apply);
  document.addEventListener('DOMContentLoaded', () => {
    const select = document.getElementById('theme-mode');
    if (!select) return;
    select.value = mode;
    select.addEventListener('change', () => {
      mode = valid(select.value) ? select.value : 'system';
      try { localStorage.setItem(key, mode); } catch {}
      apply();
    });
  });
})();
