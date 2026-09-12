'use strict';
(() => {
  const form = document.getElementById('api-access-form');
  const widgetForm = document.getElementById('widget-access-form');
  const forms = [form, widgetForm];
  const widgetStatus = document.getElementById('widget-access-status');
  const status = document.getElementById('api-access-status');
  const output = document.getElementById('api-access-token');
  const enabled = document.getElementById('api-access-widget');
  function render(data) {
    status.textContent = data.configured ? 'Private API token configured. Existing tokens are never displayed.' : 'No private API token configured. Private API calls are blocked.';
    enabled.checked = data.widget_enabled;
    widgetStatus.textContent = data.widget_enabled ? 'Public widget access is enabled.' : 'Public widget access is disabled.';
  }
  forms.forEach(currentForm => currentForm.addEventListener('submit', async event => {
    event.preventDefault();
    const action = event.submitter.value;
    output.value = ''; output.hidden = true;
    forms.forEach(item => item.querySelector('fieldset').disabled = true);
    try {
      const data = await api('api-access', {method: 'POST', body: JSON.stringify({action, widget_enabled: enabled.checked})});
      render(data);
      if (data.token) { output.hidden = false; output.value = data.token; output.focus(); output.select(); }
    } catch (problem) { (currentForm === widgetForm ? widgetStatus : status).textContent = problem.message; }
    finally { forms.forEach(item => item.querySelector('fieldset').disabled = false); }
  }));
  (async () => {
    try {
      const identity = await api('session');
      if (identity.role !== 'administrator') { status.textContent = widgetStatus.textContent = 'Administrator role required to manage API access.'; return; }
      csrf = identity.csrf_token;
      render(await api('api-access'));
      forms.forEach(item => item.querySelector('fieldset').disabled = false);
    } catch (problem) { status.textContent = widgetStatus.textContent = problem.message; }
  })();
})();
