// The demo's whole "farm": one plain object, kept in sessionStorage.
//
// sessionStorage belongs to one browser tab. It survives a reload and moving between
// pages, and it is gone when the tab closes; a new private window starts empty. Nothing
// here is sent anywhere: there is no request in this file or in any file that imports it.

import { seed } from './seed.js';

const KEY = 'superfarm-demo';
const VERSION = 1;

// Node (the contract tests) and browsers that block storage get a Map instead.
function openStorage() {
  try {
    const found = globalThis.sessionStorage;
    found.getItem(KEY);
    return found;
  } catch {
    const memory = new Map();
    return { getItem: (k) => (memory.has(k) ? memory.get(k) : null), setItem: (k, v) => memory.set(k, v), removeItem: (k) => memory.delete(k) };
  }
}

const storage = openStorage();
let clock = () => Date.now() / 1000;

// Seconds, like every timestamp of the real API. Tests replace the clock to move time.
export const now = () => clock();
export function setClock(fn) { clock = fn; }

function load() {
  try {
    const raw = storage.getItem(KEY);
    const data = raw ? JSON.parse(raw) : null;
    if (data && data.v === VERSION) return data;
  } catch { /* unreadable: start again */ }
  return { v: VERSION, ...seed(now()) };
}

// The same object for the whole life of the page; reset() refills it in place.
export const db = load();

let full = false;
export function save() {
  try {
    storage.setItem(KEY, JSON.stringify(db));
    full = false;
  } catch {
    full = true;            // over the browser's quota: the page goes on from memory
  }
}
export const storageFull = () => full;

export function reset() {
  for (const key of Object.keys(db)) delete db[key];
  Object.assign(db, { v: VERSION, ...seed(now()) });
  save();
  emit({ type: 'resync' });
}

// ---------------------------------------------------------------- events
// What the real daemon pushes on /api/events. Here: every open fake socket.
const listeners = new Set();
export function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function emit(message) {
  for (const fn of [...listeners]) {
    try { fn(message); } catch { /* one closed socket must not stop the others */ }
  }
}

// ------------------------------------------------------------------- ids
export function nextId(kind) {
  db.counters[kind] = (db.counters[kind] || 0) + 1;
  return db.counters[kind];
}

export * from './util.js';
