// The operator's Yes/No for AI assistants: what is waiting, kept current for the whole
// panel. The top bar shows the count, a toast announces a request that arrives while
// another page is open, and the Approvals page draws the cards from the same list.
//
// The daemon pushes a changed request on /api/events; polling every few seconds gives
// the same answer when the socket is down. Time left is counted against the server's
// clock (`now` in every answer), never this computer's, which may be minutes off.

import { get, post } from './api.js';

const POLL_MS = 5000;
const listeners = new Set();
const state = {
  pending: [],            // oldest first
  count: 0,
  offset: 0,              // server time minus this computer's, in seconds
  loaded: false,
  error: '',
};
const seen = new Set();   // ids already announced
let timer = 0;
let running = false;
let onArrival = () => {};

export const approvals = state;

export function serverNow() {
  return Date.now() / 1000 + state.offset;
}

export const secondsLeft = (item) => Math.max(0, item.expires - serverNow());

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function publish() {
  for (const fn of listeners) {
    try { fn(state); } catch { /* one broken listener must not stop the others */ }
  }
}

function take(list, announce) {
  const fresh = [];
  state.pending = list.filter((item) => item.state === 'pending').sort((a, b) => a.created - b.created);
  state.count = state.pending.length;
  for (const item of state.pending) {
    if (!seen.has(item.id)) { seen.add(item.id); fresh.push(item); }
  }
  if (announce && fresh.length) onArrival(fresh);
}

export async function refreshApprovals() {
  if (!running) return;
  try {
    const data = await get('/api/approvals', { state: 'pending', limit: 200 });
    state.offset = data.now - Date.now() / 1000;
    take(data.approvals, state.loaded);
    state.loaded = true;
    state.error = '';
  } catch (error) {
    state.error = error.message;
  }
  publish();
}

// A push from the daemon: requests that were created or decided just now.
export function ingestApprovals(changed) {
  if (!running || !Array.isArray(changed) || !changed.length) return;
  const byId = new Map(state.pending.map((item) => [item.id, item]));
  for (const item of changed) byId.set(item.id, item);
  take([...byId.values()], state.loaded);
  publish();
}

// arrival(fresh): called with requests that were not there a moment ago.
export function startApprovals(arrival) {
  onArrival = arrival;
  running = true;
  state.loaded = false;
  seen.clear();
  clearInterval(timer);
  timer = setInterval(() => { if (!document.hidden) refreshApprovals(); }, POLL_MS);
  refreshApprovals();
}

export function stopApprovals() {
  running = false;
  clearInterval(timer);
  state.pending = [];
  state.count = 0;
  state.loaded = false;
  publish();
}

// The two answers. A request decided meanwhile (the other way, or expired) comes back
// as 409: the caller refreshes and shows what it is now.
export const approve = (id, remember = false) => post(`/api/approvals/${encodeURIComponent(id)}/approve`, { remember, via: 'panel' });
export const deny = (id, reason = '') => post(`/api/approvals/${encodeURIComponent(id)}/deny`, { reason, via: 'panel' });
