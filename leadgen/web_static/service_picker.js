(() => {
  document.querySelectorAll('[data-service-picker]').forEach((picker) => {
    const rows = [...picker.querySelectorAll('[data-service-row]')];
    const groups = [...picker.querySelectorAll('[data-service-group]')];
    const search = picker.querySelector('[data-service-search]');
    const summary = picker.querySelector('[data-service-summary]');

    function updateSummary() {
      const visible = rows.filter((row) => !row.hidden);
      const selected = rows.filter((row) => row.querySelector('input').checked);
      summary.textContent = `Показано: ${visible.length} из ${rows.length}. Выбрано услуг: ${selected.length}.`;
    }
    function filter() {
      const query = search.value.trim().toLocaleLowerCase('ru-RU');
      rows.forEach((row) => { row.hidden = Boolean(query && !row.dataset.name.includes(query)); });
      groups.forEach((group) => {
        group.hidden = ![...group.querySelectorAll('[data-service-row]')].some((row) => !row.hidden);
      });
      updateSummary();
    }
    picker.addEventListener('click', (event) => {
      const button = event.target.closest('[data-service-action]');
      if (!button) return;
      rows.forEach((row) => {
        const checkbox = row.querySelector('input');
        if (button.dataset.serviceAction === 'visible' && !row.hidden) checkbox.checked = true;
        if (button.dataset.serviceAction === 'clear') checkbox.checked = false;
      });
      updateSummary();
    });
    picker.addEventListener('change', updateSummary);
    search.addEventListener('input', filter);
    filter();
  });
})();
