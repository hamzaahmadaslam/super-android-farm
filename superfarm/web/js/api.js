// The one place that talks to the daemon. Same origin only: the session cookie
// authenticates, and every change carries the CSRF value handed out at login.
//
// Three answers are handled here so that no page has to:
//   401                 the session ended            -> back to the login screen
//   409 over_capacity   a start would exceed the capacity estimate -> ask "Run anyway",
//                       and repeat the call with force: true
//   { task }            long work was queued         -> handed to the task drawer

const CSRF_COOKIE = 'sf_csrf';
let csrfValue = '';

// Set by main.js. Kept as hooks so this module needs nothing from the UI layer.
export const hooks = {
  unauthorized() {},
  task() {},
  confirmOverCapacity: async () => false,
};

export class ApiError extends Error {
  constructor(status, error) {
    super(error.message || `The request failed (HTTP ${status}).`);
    this.name = 'ApiError';
    this.status = status;
    this.code = error.code || '';
    this.detail = error.detail;
    this.capacity = error.capacity;
  }
}

// The daemon sets the CSRF value in a cookie the page may read (the session cookie
// itself is HttpOnly). The copy from the login answer covers browsers that hide cookies.
function csrf() {
  const found = document.cookie.split('; ').find((part) => part.startsWith(`${CSRF_COOKIE}=`));
  return found ? decodeURIComponent(found.slice(CSRF_COOKIE.length + 1)) : csrfValue;
}

export function setCsrf(value) {
  csrfValue = value || '';
}

export function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value != null && value !== '') search.set(key, value);
  }
  const out = search.toString();
  return out ? `?${out}` : '';
}

async function parse(response) {
  const type = response.headers.get('content-type') || '';
  if (response.status === 204) return null;
  if (type.includes('application/json')) return response.json();
  return response.text();
}

export async function request(method, path, body, { quiet = false } = {}) {
  const headers = { Accept: 'application/json' };
  const init = { method, headers, credentials: 'same-origin' };
  if (method !== 'GET') headers['X-SF-CSRF'] = csrf();
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError(0, { code: 'unreachable', message: 'The farm daemon did not answer. Is `sf serve` still running?' });
  }
  const data = await parse(response);
  if (response.ok) {
    if (data && typeof data === 'object' && data.task && !quiet) hooks.task(data.task);
    return data;
  }
  const error = (data && typeof data === 'object' && data.error) || { message: typeof data === 'string' ? data.slice(0, 300) : '' };
  // A wrong token at login, or no session yet at start-up, is not a session that ended.
  if (response.status === 401 && path !== '/api/auth/login' && path !== '/api/auth/me') hooks.unauthorized();
  if (response.status === 409 && error.code === 'over_capacity' && !(body && body.force) && !(body instanceof FormData)) {
    if (await hooks.confirmOverCapacity(error)) return request(method, path, { ...(body || {}), force: true }, { quiet });
  }
  throw new ApiError(response.status, error);
}

export const get = (path, params) => request('GET', path + query(params));
export const post = (path, body = {}) => request('POST', path, body);
export const put = (path, body = {}) => request('PUT', path, body);
export const patch = (path, body = {}) => request('PATCH', path, body);
export const del = (path, body) => request('DELETE', path, body);

// One file per request, sent as multipart form data next to any plain fields.
export function upload(path, file, fields = {}) {
  const form = new FormData();
  for (const [key, value] of Object.entries(fields)) {
    if (value != null && value !== '') form.append(key, value);
  }
  form.append('file', file, file.name);
  return request('POST', path, form);
}

export function socketUrl(path) {
  return `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${path}`;
}
