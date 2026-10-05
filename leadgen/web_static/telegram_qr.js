(() => {
  const panel = document.querySelector('[data-qr-wait]');
  if (!panel) return;
  const status = panel.querySelector('[data-qr-status]');
  let stopped = false;

  async function waitForLogin() {
    if (stopped) return;
    const body = new URLSearchParams({csrf: panel.dataset.csrf});
    try {
      const response = await fetch(panel.dataset.qrWait, {
        method: 'POST', body, credentials: 'same-origin',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'}
      });
      const result = await response.json();
      if (result.status === 'pending') {
        status.textContent = 'QR-код активен. Ожидаем подтверждение в Telegram…';
        setTimeout(waitForLogin, 250);
        return;
      }
      if (result.status === 'active') {
        stopped = true;
        status.textContent = `Аккаунт подключён. Найдено групп: ${result.count}.`;
        window.location.reload();
        return;
      }
      if (result.status === 'password') {
        stopped = true;
        window.location.reload();
        return;
      }
      stopped = true;
      status.textContent = result.message || 'Не удалось подтвердить QR-код.';
    } catch (_error) {
      if (!stopped) setTimeout(waitForLogin, 1500);
    }
  }

  window.addEventListener('pagehide', () => { stopped = true; });
  waitForLogin();
})();
