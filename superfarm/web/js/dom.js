// DOM building, formatting and selector helpers shared by every page.
//
// All markup is built with h(): values from the API only ever become text nodes or
// attributes, never HTML, so nothing a device or an app reports can inject script.

const ATTRIBUTE_ONLY = new Set(['role', 'list', 'form', 'for']);   // read-only or unevenly supported as properties

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  const { value: initial, ...rest } = attrs || {};
  for (const [key, value] of Object.entries(rest)) {
    if (value == null || value === false) continue;
    if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else if (key === 'class') el.className = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key in el && !key.startsWith('aria') && !ATTRIBUTE_ONLY.has(key)) el[key] = value;
    else el.setAttribute(key, value === true ? '' : value);
  }
  add(el, children);
  // After the children: a <select> can only take a value once its options exist.
  if (initial != null) el.value = initial;
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
