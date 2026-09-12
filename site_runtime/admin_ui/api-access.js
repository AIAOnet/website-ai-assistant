'use strict';
(() => {
  const endpoint = document.getElementById('private-api-endpoint');
  const copyStatus = document.getElementById('private-api-copy-status');
  endpoint.value = new URL('/api/chat', window.location.origin).href;
  document.getElementById('private-api-copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(endpoint.value);
      copyStatus.textContent = 'Private API endpoint copied.';
    } catch {
      endpoint.focus(); endpoint.select();
      copyStatus.textContent = 'Select and copy the endpoint manually.';
    }
  });
  const example = document.getElementById('private-api-example');
  example.value = `// Server-side JavaScript (Node.js 18+). Never embed this token in a webpage.
const endpoint = ${JSON.stringify(endpoint.value)};
const apiToken = process.env.ASSISTANT_API_TOKEN || 'YOUR_PRIVATE_API_TOKEN';

async function askAssistant() {
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: 'Bearer ' + apiToken
    },
    body: JSON.stringify({
      conversation_id: 'YOUR_CONVERSATION_ID',
      message: 'YOUR_QUESTION_HERE',
      language: 'en'
    }),
    signal: AbortSignal.timeout(90000)
  });

  if (!response.ok) {
    throw new Error('Assistant request failed: HTTP ' + response.status);
  }

  const data = await response.json();
  console.log(data.answer);
  console.log('Sources:', data.sources);
  return data;
}

askAssistant().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});`;
  document.getElementById('private-api-example-copy').addEventListener('click', async () => {
    const status = document.getElementById('private-api-example-status');
    try {
      await navigator.clipboard.writeText(example.value);
      status.textContent = 'JavaScript example copied.';
    } catch {
      example.focus(); example.select();
      status.textContent = 'Select and copy the example manually.';
    }
  });
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
