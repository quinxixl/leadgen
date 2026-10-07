(() => {
  'use strict';
  // The SDK object also exists in a plain browser; only initData proves we run inside Telegram.
  const sdk = window.Telegram && window.Telegram.WebApp;
  const initData = (sdk && sdk.initData) || '';
  const tg = initData ? sdk : null;
  const body = document.body;
  const page = body.dataset.page;
  const GUARD = 'signalid_tg_boot';
  if (tg) { tg.ready(); tg.expand(); body.classList.add('in-tg'); }

  function post(url, data) {
    return fetch(url, { method: 'POST', credentials: 'include', headers: { Accept: 'application/json' },
      body: new URLSearchParams(data) })
      .then((r) => r.json().catch(() => ({ ok: false })).then((json) => Object.assign({ status: r.status }, json)));
  }
  function openExternal(url) {
    if (tg && tg.openLink) tg.openLink(url); else window.open(url, '_blank', 'noopener');
  }
  function alertText(text) {
    if (tg && tg.showAlert) tg.showAlert(text); else window.alert(text);
  }
  // One sign-in attempt per 30 s: if the cookie did not stick, the boot page must not reload forever.
  function attemptAllowed() {
    try {
      if (Date.now() - Number(sessionStorage.getItem(GUARD) || 0) < 30000) return false;
      sessionStorage.setItem(GUARD, String(Date.now()));
      return true;
    } catch (e) { return false; }
  }
  function signIn(next) {
    return post('/tg/auth', { init_data: initData, next: next || '' });
  }

  function boot() {
    const show = (name) => document.querySelectorAll('[data-boot]').forEach((el) => { el.hidden = el.dataset.boot !== name; });
    if (!initData) { show('outside'); return; }
    if (!attemptAllowed()) { show('fallback'); return; }
    signIn(body.dataset.target).then((res) => {
      if (res.ok) { location.replace(res.next); return; }
      document.querySelector('[data-boot-error]').textContent = res.error || 'Проверьте соединение и попробуйте ещё раз.';
      show('error');
    }).catch(() => show('error'));
  }
  if (page === 'boot') {
    boot();
    const retry = document.querySelector('[data-boot-retry]');
    if (retry) retry.addEventListener('click', () => { try { sessionStorage.removeItem(GUARD); } catch (e) { /* blocked */ } location.reload(); });
    const handoff = document.querySelector('[data-boot-handoff]');
    if (handoff) handoff.addEventListener('click', () => {
      post('/tg/auth', { init_data: initData, handoff: '1' }).then((res) => {
        if (res.handoff) openExternal(res.handoff);
        else document.querySelector('[data-boot-message]').textContent = res.error || 'Войдите на сайте через Telegram-бота.';
      });
    });
  } else {
    // Another Telegram account on the same device must not see this session: hide it and sign in again.
    const viewer = tg && tg.initDataUnsafe && tg.initDataUnsafe.user;
    if (viewer && body.dataset.tgUser && String(viewer.id) !== body.dataset.tgUser) {
      document.querySelectorAll('main, .tg-nav').forEach((el) => { el.hidden = true; });
      const reopen = 'Закройте мини-приложение и откройте его снова.';
      if (attemptAllowed()) {
        signIn(location.pathname + location.search)
          .then((res) => { if (res.ok) location.replace(res.next); else alertText(res.error || reopen); })
          .catch(() => alertText(reopen));
      } else alertText(reopen);
    } else {
      try { sessionStorage.removeItem(GUARD); } catch (e) { /* blocked */ }
    }
  }

  if (tg) {
    const back = tg.BackButton;
    if (page === 'boot' || body.dataset.root === '1') back.hide();
    else {
      back.show();
      back.onClick(() => { if (history.length > 1) history.back(); else location.href = '/tg/feed'; });
    }
    const form = document.querySelector('form[data-main-button]');
    const main = tg.MainButton;
    if (form) {
      main.setText(form.dataset.mainButton);
      main.enable(); main.hideProgress(); main.show();
      main.onClick(() => {
        if (!form.reportValidity()) return;
        if (form.requestSubmit) form.requestSubmit(); else form.submit();
      });
      form.addEventListener('submit', () => { main.showProgress(false); main.disable(); });
      window.addEventListener('pageshow', () => { main.hideProgress(); main.enable(); });
    } else main.hide();
    const haptic = document.querySelector('[data-haptic]');
    if (haptic && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(haptic.dataset.haptic);
  }

  document.addEventListener('click', (event) => {
    const handoff = event.target.closest('[data-handoff]');
    if (handoff) {
      event.preventDefault();
      post('/tg/handoff', { csrf: body.dataset.csrf, next: handoff.dataset.handoff }).then((res) => {
        if (res.ok && res.url) openExternal(res.url); else alertText(res.error || 'Не удалось открыть сайт. Попробуйте ещё раз.');
      }).catch(() => alertText('Нет соединения. Попробуйте ещё раз.'));
      return;
    }
    const fill = event.target.closest('a[data-fill]');
    const area = document.querySelector('textarea[name="body"]');
    if (fill && area) {
      event.preventDefault();
      area.value = fill.dataset.fill;
      document.querySelectorAll('a[data-fill]').forEach((el) => el.removeAttribute('aria-current'));
      fill.setAttribute('aria-current', 'page');
      const chosen = document.querySelector('input[name="v"]');
      if (chosen) chosen.value = new URLSearchParams(fill.search).get('v') || '';
      return;
    }
    const preset = event.target.closest('[data-budget]');
    const budget = document.querySelector('input[name="min_budget"]');
    if (preset && budget) { budget.value = preset.dataset.budget; return; }
    // Webviews ignore target=_blank: Telegram opens such links itself (t.me inside Telegram).
    const external = event.target.closest('a[target="_blank"]');
    if (external && tg && /^https?:/.test(external.href)) {
      event.preventDefault();
      if (/^https:\/\/t\.me\//.test(external.href) && tg.openTelegramLink) tg.openTelegramLink(external.href);
      else tg.openLink(external.href);
    }
  });
})();
