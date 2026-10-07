(() => {
  const panel = document.querySelector('[data-qr-wait]');
  if (!panel) return;
  const status = panel.querySelector('[data-qr-status]');
  const image = panel.querySelector('img');
  const link = panel.querySelector('a[href^="tg:"]');
  // The server answers within a few seconds (short poll); keep the pause between
  // polls so a single tab never occupies the small pool of web workers.
  const POLL_DELAY = 1500;
  const MAX_FAILURES = 8;
  let stopped = false;
  let failures = 0;

  function again(delay) {
    if (!stopped) setTimeout(waitForLogin, delay);
  }

  async function waitForLogin() {
    if (stopped) return;
    const body = new URLSearchParams({csrf: panel.dataset.csrf});
    try {
      const response = await fetch(panel.dataset.qrWait, {
        method: 'POST', body, credentials: 'same-origin',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'}
      });
      const result = await response.json();
      failures = 0;
      if (result.status === 'pending' || result.status === 'refreshed') {
        if (result.status === 'refreshed' && image) {
          image.src = `/app/telegram/qr.svg?t=${Date.now()}`;
        }
        if (result.status === 'refreshed' && link && result.url) link.href = result.url;
        status.textContent = 'QR-код активен. Ожидаем подтверждение в Telegram…';
        again(POLL_DELAY);
        return;
      }
      if (result.status === 'retry') {
        again(POLL_DELAY * 4);
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
      failures += 1;
      if (failures >= MAX_FAILURES) {
        stopped = true;
        status.textContent = 'Нет связи с сервером. Обновите страницу.';
        return;
      }
      again(POLL_DELAY * 2);
    }
  }

  window.addEventListener('pagehide', () => { stopped = true; });
  waitForLogin();
})();
