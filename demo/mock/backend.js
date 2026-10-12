// The demo's stand-in for the daemon: a route table keyed by method and path pattern.
//
// handle(method, url, body, file) answers one request with { status, body } and never
// touches the network. Route files under routes/ add entries with route(); a pattern is a
// path of the real API with {name} for one segment and {name*} for the rest of the path:
//
//     route('GET', '/api/instances/{id}/net', ({ params, query }) => ({ ... }));
//
// A handler returns the JSON body (status 200), or reply(status, body), or throws an
// ApiError from farm.js. `tests/unit/test_demo_contract.py` checks that every path the
// panel calls has an entry here, and compares the answers with the real API's.

import { advance, ApiError, publicTask } from './farm.js';
import { db, save } from './store.js';

const table = [];
const PUBLIC = new Set(['GET /api/health', 'POST /api/auth/login', 'POST /api/auth/logout']);

function compile(pattern) {
  const names = [];
  const parts = pattern.split('/');
  const source = parts.map((part) => {
    const m = /^\{(\w+)(\*?)\}$/.exec(part);
    if (!m) return part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    names.push(m[1]);
    return m[2] ? '(.+)' : '([^/]+)';
  }).join('/');
  // The more of a pattern is literal text, the more specific it is: /api/instances/bulk
  // wins over /api/instances/{id}, whatever order the two were added in.
  return { regex: new RegExp(`^${source}$`), names, literal: parts.length - names.length - (pattern.includes('*}') ? 100 : 0) };
}

export function route(method, pattern, handler) {
  table.push({ method, pattern, handler, ...compile(pattern) });
}

// What the browser loads by itself (a picture, a download) and what is a socket in the
// real API. They are listed here so that the route table names everything the panel asks
// for; transport.js serves them (blob URLs, a fake socket) instead of handle().
const DRAWN = { demo: 'in the demo this address is served inside the page, not by a request' };
export function media(pattern, kind) {
  table.push({ method: 'GET', pattern, kind, handler: () => DRAWN, ...compile(pattern) });
}
export function socket(pattern, kind) {
  table.push({ method: 'WS', pattern, kind, handler: () => DRAWN, ...compile(pattern) });
}
// { kind, params, query } for a media or socket address, or null.
export function served(method, url) {
  const at = url.indexOf('?');
  const found = find(method, at < 0 ? url : url.slice(0, at));
  if (!found.entry || !found.entry.kind) return null;
  return { kind: found.entry.kind, params: found.params, query: Object.fromEntries(new URLSearchParams(at < 0 ? '' : url.slice(at + 1))) };
}

export const reply = (status, body) => ({ __reply: true, status, body });
export const created = (body) => reply(201, body);
export const accepted = (task) => reply(202, { task: publicTask(task) });

// What the mock answers, for the contract test and for anyone extending it.
export const listRoutes = () => table.map(({ method, pattern }) => ({ method, pattern }));

function find(method, path) {
  let wrongMethod = false;
  let best = null;
  for (const entry of table) {
    const m = entry.regex.exec(path);
    if (!m) continue;
    if (entry.method !== method) { wrongMethod = true; continue; }
    if (best && best.entry.literal >= entry.literal) continue;
    const params = {};
    try {
      entry.names.forEach((name, index) => { params[name] = decodeURIComponent(m[index + 1]); });
    } catch {
      continue;
    }
    best = { entry, params };
  }
  return best || { wrongMethod };
}

export function handle(method, url, body, file = null) {
  const at = url.indexOf('?');
  const path = at < 0 ? url : url.slice(0, at);
  const query = Object.fromEntries(new URLSearchParams(at < 0 ? '' : url.slice(at + 1)));
  try {
    const { entry, params, wrongMethod } = find(method, path);
    if (!entry) {
      if (wrongMethod) throw new ApiError(405, 'method_not_allowed', `this route does not take ${method}`);
      throw new ApiError(404, 'not_in_demo', `The demo does not simulate ${method} ${path} yet. See docs/DEMO.md for how to add a route.`);
    }
    if (!db.session.signedIn && !PUBLIC.has(`${method} ${entry.pattern}`)) {
      throw new ApiError(401, 'unauthorized', 'authentication required: sign in first');
    }
    advance();
    const answer = entry.handler({ method, path, params, query, body: body && typeof body === 'object' ? body : {}, file });
    if (method !== 'GET') save();
    if (answer && answer.__reply) return { status: answer.status, body: answer.body };
    return { status: 200, body: answer === undefined ? null : answer };
  } catch (error) {
    if (error instanceof ApiError) return { status: error.status, body: error.body };
    // A bug in the mock: answered the way the daemon answers its own, and visible in the console.
    console.error(error);                                    // eslint-disable-line no-console
    return { status: 500, body: { error: { code: 'internal', message: `the demo backend failed: ${error.message}` } } };
  }
}

// ---------------------------------------------------- helpers for handlers
export const truthy = (value) => value === true || value === 'true' || value === '1';

export function need(body, field, { max = 500 } = {}) {
  const value = body[field];
  if (typeof value !== 'string' || !value.trim()) throw new ApiError(422, 'validation_error', `${field}: Field required`, { detail: [{ field, message: 'Field required' }] });
  if (value.length > max) throw new ApiError(422, 'validation_error', `${field}: String should have at most ${max} characters`, { detail: [{ field, message: `String should have at most ${max} characters` }] });
  return value.trim();
}

export function whole(value, low, high, name) {
  const n = Number(value);
  if (!Number.isFinite(n)) throw new ApiError(422, 'validation_error', `${name}: Input should be a valid number`);
  return Math.min(high, Math.max(low, Math.round(n)));
}
