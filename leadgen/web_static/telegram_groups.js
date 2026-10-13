(() => {
  const panel = document.querySelector('.telegram-groups');
  if (!panel) return;
  const rows = [...panel.querySelectorAll('[data-group-row]')];
  const search = panel.querySelector('[data-group-search]');
  const sphere = panel.querySelector('[data-group-sphere]');
  const summary = panel.querySelector('[data-group-summary]');

  function updateSummary() {
    const visible = rows.filter((row) => !row.hidden);
    const selected = rows.filter((row) => row.querySelector('input').checked);
    summary.textContent = `Показано: ${visible.length} из ${rows.length}. Выбрано: ${selected.length}.`;
  }

  function applyFilters() {
    const query = search.value.trim().toLocaleLowerCase('ru-RU');
    const selectedSphere = sphere.value;
    rows.forEach((row) => {
      const nameMatches = !query || row.dataset.name.toLocaleLowerCase('ru-RU').includes(query);
      const spheres = row.dataset.spheres.split('|');
      row.hidden = !(nameMatches && (!selectedSphere || spheres.includes(selectedSphere)));
    });
    updateSummary();
  }

  panel.addEventListener('click', (event) => {
    const button = event.target.closest('[data-group-action]');
    if (!button) return;
    const action = button.dataset.groupAction;
    rows.forEach((row) => {
      const checkbox = row.querySelector('input');
      if (action === 'all') checkbox.checked = true;
      if (action === 'visible' && !row.hidden) checkbox.checked = true;
      if (action === 'clear-visible' && !row.hidden) checkbox.checked = false;
      if (action === 'clear') checkbox.checked = false;
    });
    updateSummary();
  });
  panel.addEventListener('change', updateSummary);
  search.addEventListener('input', applyFilters);
  sphere.addEventListener('change', applyFilters);
  applyFilters();
})();
