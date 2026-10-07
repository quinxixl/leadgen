// Finishes the website login on its own once the code is confirmed in the Telegram bot.
// The «Завершить вход» button stays as a fallback when scripts or polling fail.
(() => {
  const form = document.querySelector('[data-login-finish]');
  if (!form) return;
  const url = form.dataset.statusUrl || '/login/status';
  const waiting = document.querySelector('[data-login-waiting]');
  const expired = document.querySelector('[data-login-expired]');
  let timer = null;
  let done = false;
  if (waiting) waiting.hidden = false;

  function finish() {
    done = true;
    clearTimeout(timer);
    if (form.requestSubmit) form.requestSubmit(); else form.submit();
  }
  function stop() {
    done = true;
    clearTimeout(timer);
    if (waiting) waiting.hidden = true;
    if (expired) expired.hidden = false;
  }
  async function poll() {
    if (done) return;
    try {
      const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
      if (response.ok) {
        const data = await response.json();
        if (data.status === 'approved') { finish(); return; }
        if (data.status === 'signed_in') { done = true; window.location.assign('/app'); return; }
        if (data.status === 'expired' || data.status === 'none') { stop(); return; }
      }
    } catch (error) { /* network hiccup: try again on the next tick */ }
    timer = setTimeout(poll, 2000);
  }
  // Background tabs throttle timers; check at once when the user comes back from Telegram.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !done) { clearTimeout(timer); poll(); }
  });
  timer = setTimeout(poll, 2000);
})();
