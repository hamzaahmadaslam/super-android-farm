// The one place that talks to the daemon. Same origin only: the session cookie
// authenticates, and every change carries the CSRF value handed out at login.
// The network itself is reached through transport.js, which this module re-exports.
//
// Three answers are handled here so that no page has to:
//   401                 the session ended            -> back to the login screen
//   409 over_capacity   a start would exceed the capacity estimate -> ask "Run anyway",
//                       and repeat the call with force: true
//   { task }            long work was queued         -> handed to the task drawer

import { request as transportRequest } from './transport.js';

export { mediaUrl, openSocket, streamInto } from './transport.js';

const CSRF_COOKIE = 'sf_csrf';
let csrfValue = '';

// Set by main.js. Kept as hooks so this module needs nothing from the UI layer.
export const hooks = {
  unauthorized() {},
  task() {},
  confirmOverCapacity: async () => false,
  link() {},                  // link(true | false): did the daemon answer the last request
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

// A change asked for twice at the same moment is sent once: while a POST, PUT, PATCH or
// DELETE is on its way, the identical request gets the same answer instead of a second
// trip to the daemon. This backs up the busy state of the controls (dom.js): whatever
// control fires it, a double press cannot create two Androids or start two backups.
// Live input is the exception, because there a repeat is the point (a double tap, the
// same key twice).
const REPEATABLE = /^\/api\/(input\/|automation\/record\/[^/]+\/(?!stop$|cancel$))/;
const pending = new Map();

function signature(method, path, body) {
  if (method === 'GET' || REPEATABLE.test(path)) return '';
  if (body instanceof FormData) {
    const fields = [...body.entries()].map(([name, value]) => (value instanceof File
      ? `${name}=file:${value.name}:${value.size}:${value.lastModified}` : `${name}=${value}`));
    return `${method} ${path} ${fields.join('&')}`;
  }
  return `${method} ${path} ${body === undefined ? '' : JSON.stringify(body)}`;
}

export function request(method, path, body, options = {}) {
  const key = signature(method, path, body);
  if (!key) return send(method, path, body, options);
  if (pending.has(key)) return pending.get(key);
  const promise = send(method, path, body, options).finally(() => pending.delete(key));
  pending.set(key, promise);
  return promise;
}

async function send(method, path, body, { quiet = false } = {}) {
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
    response = await transportRequest(path, init);
  } catch {
    hooks.link(false);
    throw new ApiError(0, { code: 'unreachable', message: 'The farm daemon did not answer. Is `sf serve` still running?' });
  }
  hooks.link(true);
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
// For a task the page follows by itself (a chat turn): no "Started" toast, no entry to wait on.
export const postQuiet = (path, body = {}) => request('POST', path, body, { quiet: true });
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
