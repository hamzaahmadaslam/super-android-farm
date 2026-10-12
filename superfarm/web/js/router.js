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
// the scope; leaving the page disposes it (timers, streams, listeners). Every visit gets
// an element of its own, so a slow page that finishes loading after the operator moved
// on writes into nothing instead of into the next page.
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
    const page = h('div', { class: 'page' });
    // Placeholders instead of a blank screen, but only when the page is not instant.
    const wait = h('div', { class: 'stack', 'aria-busy': 'true', 'aria-label': 'Loading' },
      h('span', { class: 'skeleton line w-40' }), h('span', { class: 'skeleton block' }), h('span', { class: 'skeleton block' }));
    const waiting = setTimeout(() => { if (current === mine && !page.children.length) page.append(wait); }, 200);
    const filled = new MutationObserver(() => { if ([...page.children].some((node) => node !== wait)) wait.remove(); });
    filled.observe(page, { childList: true });
    replace(root, page);
    onChange(name);
    try {
      await routes[name](page, params, mine);
    } catch (error) {
      // Only report for the page still on screen; a slow page the operator already left is silent.
      if (current === mine) {
        page.append(h('div', { class: 'banner fail page-error', role: 'alert' },
          h('div', { class: 'grow' }, h('strong', null, 'This page could not be loaded. '), error.message),
          h('button', { class: 'btn', type: 'button', onclick: show }, 'Try again')));
      }
    } finally {
      clearTimeout(waiting);
      filled.disconnect();
      wait.remove();
    }
    if (current === mine) root.focus({ preventScroll: true });
  };
  window.addEventListener('hashchange', show);
  show();
  return { refresh: show, stop() { window.removeEventListener('hashchange', show); if (current) current.dispose(); current = null; } };
}
