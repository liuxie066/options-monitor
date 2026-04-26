export async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({ detail: r.statusText }));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}

function jsonHeaders(token) {
  const headers = { 'content-type': 'application/json' };
  if (token) headers['x-om-token'] = String(token).trim();
  return headers;
}

export const fetchMeta = () => api('/api/meta');
export const fetchVersionCheck = () => api('/api/version/check');
export const fetchConfigSummaries = () => api('/api/configs/summary');
export const fetchEditor = (configKey) => api(`/api/configs/editor?configKey=${encodeURIComponent(configKey)}`);
export const fetchWatchlist = () => api('/api/watchlist');
export const fetchHistory = (configKey) => api(`/api/history?configKey=${encodeURIComponent(configKey)}&limit=20`);

export const postGlobalUpdate = (payload, token) => api('/api/configs/global/update', {
  method: 'POST',
  headers: jsonHeaders(token),
  body: JSON.stringify(payload),
});

export const postAccountUpsert = (payload, token) => api('/api/accounts/upsert', {
  method: 'POST',
  headers: jsonHeaders(token),
  body: JSON.stringify(payload),
});

export const postAccountDelete = (payload, token) => api('/api/accounts/delete', {
  method: 'POST',
  headers: jsonHeaders(token),
  body: JSON.stringify(payload),
});

export const postWatchlistUpsert = (payload, token) => api('/api/watchlist/upsert', {
  method: 'POST',
  headers: jsonHeaders(token),
  body: JSON.stringify(payload),
});

export const postWatchlistDelete = (payload, token) => api('/api/watchlist/delete', {
  method: 'POST',
  headers: jsonHeaders(token),
  body: JSON.stringify(payload),
});

export const postToolRun = (payload) => api('/api/tools/run', {
  method: 'POST',
  headers: jsonHeaders(''),
  body: JSON.stringify(payload),
});

export const postNotificationsCheck = (payload) => api('/api/notifications/check', {
  method: 'POST',
  headers: jsonHeaders(''),
  body: JSON.stringify(payload),
});

export const postNotificationsPreview = (payload) => api('/api/notifications/preview', {
  method: 'POST',
  headers: jsonHeaders(''),
  body: JSON.stringify(payload),
});

export const postNotificationsTestSend = (payload, token) => api('/api/notifications/test-send', {
  method: 'POST',
  headers: jsonHeaders(token),
  body: JSON.stringify(payload),
});
