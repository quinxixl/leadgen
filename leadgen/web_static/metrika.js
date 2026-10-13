// Yandex Metrica loader. The counter number comes from data-id, so the script stays static and CSP-friendly.
(function () {
  var id = Number(document.currentScript && document.currentScript.dataset.id);
  if (!id) return;
  window.dataLayer = window.dataLayer || [];
  (function (m, e, t, r, i, k, a) {
    m[i] = m[i] || function () { (m[i].a = m[i].a || []).push(arguments); };
    m[i].l = 1 * new Date();
    for (var j = 0; j < document.scripts.length; j++) { if (document.scripts[j].src === r) { return; } }
    k = e.createElement(t); a = e.getElementsByTagName(t)[0]; k.async = 1; k.src = r; a.parentNode.insertBefore(k, a);
  })(window, document, 'script', 'https://mc.yandex.ru/metrika/tag.js?id=' + id, 'ym');
  window.ym(id, 'init', {ssr: true, webvisor: true, clickmap: true, ecommerce: 'dataLayer',
    referrer: document.referrer, url: location.href, accurateTrackBounce: true, trackLinks: true});
  // Goals: data-goal="name" fires on click, data-goal-view="name" fires when the page shows the element.
  function goal(name) { if (name) window.ym(id, 'reachGoal', name); }
  document.addEventListener('click', function (event) {
    var el = event.target.closest && event.target.closest('[data-goal]');
    if (el) goal(el.getAttribute('data-goal'));
  }, true);
  function views() {
    var items = document.querySelectorAll('[data-goal-view]');
    for (var n = 0; n < items.length; n++) goal(items[n].getAttribute('data-goal-view'));
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', views); else views();
})();
