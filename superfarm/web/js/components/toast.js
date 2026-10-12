// Short results in the corner. The container is an aria-live region (see index.html),
// so screen readers announce them without moving focus. Each says what happened in
// plain words; nothing here can be undone from a toast, so none pretends it can.

import { h } from '../dom.js';
import { icon } from '../icons.js';

const ICON = { ok: 'check', warn: 'alert', fail: 'alert', info: 'info' };
const MOST = 5;

export function toast(message, kind = 'info', { action, timeout } = {}) {
  const host = document.getElementById('toasts');
  const close = () => el.remove();
  const el = h('div', { class: `toast glass ${kind}` },
    icon(ICON[kind] || 'info'),
    h('div', { class: 'grow' }, message),
    action && h('button', { class: 'btn small', type: 'button', onclick: () => { close(); action.run(); } }, action.label),
    h('button', { class: 'btn small ghost icon-only', type: 'button', 'aria-label': 'Dismiss', title: 'Dismiss', onclick: close }, icon('x')));
  host.append(el);
  // A burst of results must not bury the page: the oldest give way.
  while (host.children.length > MOST) host.firstElementChild.remove();
  // Errors stay until dismissed: they usually carry the one line the operator needs to read.
  const ms = timeout ?? (kind === 'fail' ? 0 : 5000);
  if (ms) setTimeout(close, ms);
  return close;
}

export const ok = (message, options) => toast(message, 'ok', options);
export const warn = (message, options) => toast(message, 'warn', options);
export const fail = (error, options) => toast(error && error.message ? error.message : String(error), 'fail', options);

// Wrap an action started from a button: errors become a toast showing the API's message.
export async function attempt(fn, done) {
  try {
    const result = await fn();
    if (done) ok(typeof done === 'function' ? done(result) : done);
    return result;
  } catch (error) {
    fail(error);
    return undefined;
  }
}
