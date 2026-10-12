// DOM building, formatting and selector helpers shared by every page.
//
// All markup is built with h(): values from the API only ever become text nodes or
// attributes, never HTML, so nothing a device or an app reports can inject script.

const ATTRIBUTE_ONLY = new Set(['role', 'list', 'form', 'for']);   // read-only or unevenly supported as properties

// ------------------------------------------------------- one press, one action
// Every button and form in the panel is built with h(), so this is the one place that
// makes a double click, a held Enter or an impatient second press harmless: while the
// promise a click or submit handler returned is still pending, the control is marked
// busy (aria-busy, aria-disabled, a spinner) and further presses do nothing. It is
// released when the work settles, whether it succeeded or failed.
//
// aria-disabled rather than the disabled property: the control keeps the focus, so a
// keyboard user is not thrown to the top of the page, and a dialog that the press
// opened can hand the focus back to it.
//
// A control that is redrawn while its action runs (a row of a polled list) names the
// action with data-action="start:3"; the new copy is then born busy and released with
// the old one.
const busyNow = new WeakSet();
const actions = new Map();          // data-action -> Set of elements showing it

// Set by main.js: where an error from a handler is shown. Nothing is lost silently.
export const actionHooks = { error(error) { console.error(error); } };

function parts(el) {
  return el.tagName === 'FORM' ? [el, ...el.querySelectorAll('button[type="submit"], button:not([type])')] : [el];
}

function mark(el, on) {
  for (const part of parts(el)) {
    if (on) {
      busyNow.add(part);
      part.setAttribute('aria-busy', 'true');
      if (part !== el || el.tagName !== 'FORM') part.setAttribute('aria-disabled', 'true');
      part.classList.add('is-busy');
    } else {
      busyNow.delete(part);
      part.removeAttribute('aria-busy');
      part.removeAttribute('aria-disabled');
      part.classList.remove('is-busy');
    }
  }
}

export const isBusy = (el) => busyNow.has(el) || Boolean(el.dataset && el.dataset.action && actions.has(el.dataset.action));

// Run work() for `el` unless its previous run is still going. Returns what work()
// returned (a promise is passed on, already guarded), or undefined when it was ignored.
export function busy(el, work) {
  if (isBusy(el)) return undefined;
  const result = work();
  if (!result || typeof result.then !== 'function') return result;
  const name = el.dataset ? el.dataset.action : '';
  if (name) actions.set(name, new Set([el]));
  mark(el, true);
  const release = () => {
    for (const shown of name ? actions.get(name) || [el] : [el]) mark(shown, false);
    if (name) actions.delete(name);
  };
  return result.then((value) => { release(); return value; }, (error) => { release(); actionHooks.error(error); });
}

function guarded(el, handler) {
  return (event) => {
    if (isBusy(el)) {
      // A form must still not be submitted by the browser itself.
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    busy(el, () => handler.call(el, event));
  };
}

// The same promise for the same thing asked twice at once ("delete Android 3" from a
// card and from the bulk bar): `work` runs once, both callers get its result.
const once = new Map();
export function single(key, work) {
  if (once.has(key)) return once.get(key);
  const promise = Promise.resolve().then(work).finally(() => once.delete(key));
  once.set(key, promise);
  return promise;
}

// When a dialog or the command palette closes, the page underneath is suddenly under the
// pointer and the opener has the focus again. For a moment, clicks and Enter / Space are
// swallowed, so the second half of a double click (or a key still held down) does not
// press whatever happens to be there.
let shieldUntil = 0;
let shieldOn = false;
function swallow(event) {
  if (performance.now() > shieldUntil) return;
  if (event.type !== 'click' && event.key !== 'Enter' && event.key !== ' ') return;
  event.preventDefault();
  event.stopImmediatePropagation();
}
export function shield(ms = 320) {
  shieldUntil = performance.now() + ms;
  if (shieldOn) return;
  shieldOn = true;
  for (const type of ['click', 'keydown', 'keypress', 'keyup']) window.addEventListener(type, swallow, true);
}

const GUARDED = { button: 'onclick', form: 'onsubmit' };

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  const { value: initial, ...rest } = attrs || {};
  for (const [key, value] of Object.entries(rest)) {
    if (value == null || value === false) continue;
    if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), GUARDED[tag] === key ? guarded(el, value) : value);
    else if (key === 'class') el.className = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key in el && !key.startsWith('aria') && !ATTRIBUTE_ONLY.has(key)) el[key] = value;
    else el.setAttribute(key, value === true ? '' : value);
  }
  add(el, children);
  // After the children: a <select> can only take a value once its options exist.
  if (initial != null) el.value = initial;
  // Redrawn while its action is still running: born busy, released with the original.
  if (el.dataset.action && actions.has(el.dataset.action)) {
    actions.get(el.dataset.action).add(el);
    mark(el, true);
  }
  return el;
}

export function add(el, ...children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function replace(el, ...children) {
  el.replaceChildren();
  return add(el, children);
}

export const $ = (selector, root = document) => root.querySelector(selector);

let ids = 0;
export const uid = (prefix = 'f') => `${prefix}${++ids}`;

// ------------------------------------------------------------------ lifetime
// A page gets one scope. Everything it starts (timers, listeners, streams) is
// registered there and stops when the operator navigates away.
export function scope() {
  const cleanups = [];
  return {
    add(fn) { cleanups.push(fn); return fn; },
    on(target, type, fn, options) {
      target.addEventListener(type, fn, options);
      cleanups.push(() => target.removeEventListener(type, fn, options));
    },
    // Run now and then every `ms`, skipping turns while the tab is hidden. The next turn
    // is scheduled after the previous one finished, so a slow API never piles up.
    // Returns the first run's promise: awaiting it lets a page report a failed first
    // load (and then nothing is scheduled); later failures are retried silently.
    every(ms, fn) {
      let timer = 0;
      let stopped = false;
      const turn = async (first) => {
        try {
          if (first || !document.hidden) await fn();
        } catch (error) {
          if (first) { stopped = true; throw error; }
        }
        if (!stopped) timer = setTimeout(turn, ms);
      };
      cleanups.push(() => { stopped = true; clearTimeout(timer); });
      return turn(true);
    },
    dispose() {
      for (const fn of cleanups.splice(0)) {
        try { fn(); } catch { /* one failing cleanup must not keep the others from running */ }
      }
    },
  };
}

// ---------------------------------------------------------------- formatting
export function bytes(n) {
  if (n == null || Number.isNaN(Number(n))) return '';
  let value = Number(n);
  for (const unit of ['B', 'KB', 'MB', 'GB', 'TB']) {
    if (value < 1024 || unit === 'TB') return `${unit === 'B' ? value : value.toFixed(1)} ${unit}`;
    value /= 1024;
  }
  return '';
}

export const mb = (n) => (n == null ? '' : n >= 1024 ? `${(n / 1024).toFixed(1)} GB` : `${Math.round(n)} MB`);

export function duration(seconds) {
  if (seconds == null) return '';
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

// Accepts epoch seconds or an ISO string; the API uses both.
export function when(value) {
  if (!value) return '';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

export function ago(value) {
  if (!value) return '';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return `${duration((Date.now() - date.getTime()) / 1000)} ago`;
}

export function text(value) {
  if (value == null || value === '') return '';
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (Array.isArray(value)) return value.map(text).join(', ');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

// ---------------------------------------------------------------- selectors
// The same grammar as the server's parse_selector, so the selector box can preview a
// selection without a round trip: all | running | stopped | tag:x | 5 | 1-10, comma =
// union, a leading ! removes.
const RUNNING = new Set(['running', 'booted', 'starting']);

export function parseSelector(selector, instances) {
  const picked = new Set();
  for (const raw of String(selector).split(',').map((p) => p.trim()).filter(Boolean)) {
    const negate = raw.startsWith('!');
    const part = negate ? raw.slice(1) : raw;
    let match;
    if (part === 'all') match = () => true;
    else if (part === 'running') match = (i) => RUNNING.has(i.state);
    else if (part === 'stopped') match = (i) => !RUNNING.has(i.state);
    else if (part.startsWith('tag:')) match = (i) => (i.tags || []).includes(part.slice(4).toLowerCase());
    else if (/^\d+-\d+$/.test(part)) {
      const [a, b] = part.split('-').map(Number);
      match = (i) => i.id >= Math.min(a, b) && i.id <= Math.max(a, b);
    } else if (/^\d+$/.test(part)) match = (i) => i.id === Number(part);
    else throw new Error(`"${raw}" is not a selector. Use all, running, stopped, tag:name, 5 or 1-10.`);
    for (const instance of instances) {
      if (match(instance)) picked[negate ? 'delete' : 'add'](instance.id);
    }
  }
  return picked;
}

// Ids as the shortest selector: 1,2,3,7 -> "1-3,7".
export function selectorOf(ids) {
  const sorted = [...ids].map(Number).sort((a, b) => a - b);
  const parts = [];
  for (let i = 0; i < sorted.length; i++) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j++;
    parts.push(j > i ? `${sorted[i]}-${sorted[j]}` : `${sorted[i]}`);
    i = j;
  }
  return parts.join(',');
}

export function copyText(value) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(value);
  // Clipboard API needs a secure context; a panel reached over plain HTTP on the LAN falls back.
  const area = h('textarea', { class: 'visually-hidden', value });
  document.body.append(area);
  area.select();
  const ok = document.execCommand('copy');
  area.remove();
  return ok ? Promise.resolve() : Promise.reject(new Error('copy is not available in this browser'));
}
