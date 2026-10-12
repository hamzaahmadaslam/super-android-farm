// A hash router: "#/androids", "#/android/5/apps". Hash routing needs no server
// rewrite rules, so the panel stays a folder of static files.

import { h, replace, scope } from './dom.js';

let current = null;

export function parseHash(hash) {
  const parts = String(hash || '').replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  return { name: parts[0] || '', params: parts.slice(1) };
}

export const go = (path) => { location.hash = `#/${path}`; };

// routes: { name: render(root, params, scope) }. A page registers what it starts on
// the scope; leaving the page disposes it (timers, streams, listeners).
export function startRouter(root, routes, fallback, onChange) {
  const show = async () => {
    let { name, params } = parseHash(location.hash);
    if (!routes[name]) {
      name = fallback;
      params = [];
    }
    if (current) current.dispose();
    const mine = scope();
    current = mine;
    replace(root);
    onChange(name);
    try {
      await routes[name](root, params, mine);
    } catch (error) {
      // Only report for the page still on screen; a slow page the operator already left is silent.
      if (current === mine) {
        root.append(h('div', { class: 'banner fail', role: 'alert' },
          h('div', { class: 'grow' }, h('strong', null, 'This page could not be loaded. '), error.message),
          h('button', { class: 'btn', type: 'button', onclick: show }, 'Try again')));
      }
    }
    root.focus({ preventScroll: true });
  };
  window.addEventListener('hashchange', show);
  show();
  return { refresh: show, stop() { window.removeEventListener('hashchange', show); if (current) current.dispose(); current = null; } };
}
