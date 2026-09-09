'use strict';
(() => {
  const form = document.getElementById('api-access-form');
  const status = document.getElementById('api-access-status');
  const output = document.getElementById('api-access-token');
  const enabled = document.getElementById('api-access-widget');
  function render(data) {
    status.textContent = data.configured ? 'Private API token configured. Existing tokens are never displayed.' : 'No private API token configured. Private API calls are blocked.';
    enabled.checked = data.widget_enabled;
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const action = event.submitter.value;
    output.value = ''; output.hidden = true;
    form.querySelector('fieldset').disabled = true;
    try {
      const data = await api('api-access', {method: 'POST', body: JSON.stringify({action, widget_enabled: enabled.checked})});
      render(data);
      if (data.token) { output.hidden = false; output.value = data.token; output.focus(); output.select(); }
    } catch (problem) { status.textContent = problem.message; }
    finally { form.querySelector('fieldset').disabled = false; }
  });
  (async () => {
    try {
      const identity = await api('session');
      if (identity.role !== 'administrator') { status.textContent = 'Administrator role required to manage API access.'; return; }
      csrf = identity.csrf_token;
      render(await api('api-access'));
      form.querySelector('fieldset').disabled = false;
    } catch (problem) { status.textContent = problem.message; }
  })();
})();
