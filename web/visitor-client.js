'use strict';
// Visitor credentials are scoped to public widget routes, never private API keys.
window.visitorFetch = (() => {
  let session = null, pending = null;
  try { session = JSON.parse(sessionStorage.getItem('website-visitor-token')); } catch {}
  async function credential() {
    if (session && session.expires_at * 1000 > Date.now() + 60000) return session.token;
    if (!pending) pending = (async () => {
      const headers = {};
      if (session && session.expires_at * 1000 > Date.now()) headers.Authorization = 'Bearer ' + session.token;
      const response = await fetch('/api/widget/session', {method: 'POST', headers, credentials: 'omit'});
      if (!response.ok) throw Error('Visitor session unavailable');
      session = await response.json();
      try { sessionStorage.setItem('website-visitor-token', JSON.stringify(session)); } catch {}
      return session.token;
    })().finally(() => { pending = null; });
    return pending;
  }
  return async (path, options = {}) => {
    const token = await credential();
    const response = await fetch(path.replace(/^\/api\//, '/api/widget/'), {
      ...options, credentials: 'omit', headers: {...options.headers, Authorization: 'Bearer ' + token}
    });
    if (response.status === 401) {
      session = null;
      try { sessionStorage.removeItem('website-visitor-token'); } catch {}
      // No automatic replay: an appointment mutation must never be duplicated.
    }
    return response;
  };
})();
