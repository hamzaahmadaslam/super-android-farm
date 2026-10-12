// Small floating panels anchored to a button: help text and menus.
//
// They use the browser's popover top layer where it exists, so they show above modal
// dialogs and are never clipped by a scrolling parent; elsewhere they fall back to a
// fixed-position box. One is open at a time. Escape or a click elsewhere closes it and
// focus goes back to the button that opened it.

import { h } from '../dom.js';
import { icon } from '../icons.js';

const GAP = 6;
const EDGE = 8;
let current = null;

export function closePopover() {
  if (current) current(false);
}

function place(el, anchor, align) {
  const box = anchor.getBoundingClientRect();
  const width = el.offsetWidth;
  const height = el.offsetHeight;
  const viewWidth = document.documentElement.clientWidth;
  const viewHeight = window.innerHeight;
  let left = align === 'end' ? box.right - width : box.left;
  left = Math.max(EDGE, Math.min(left, viewWidth - width - EDGE));
  let top = box.bottom + GAP;
  if (top + height > viewHeight - EDGE && box.top - height - GAP >= EDGE) top = box.top - height - GAP;
  top = Math.max(EDGE, Math.min(top, viewHeight - height - EDGE));
  el.style.left = `${Math.round(left)}px`;
  el.style.top = `${Math.round(top)}px`;
}

// Returns close(). `content` is a node or a list of nodes.
export function openPopover(anchor, content, { role = 'dialog', label, className = '', align = 'start', focus = false } = {}) {
  const again = current && current.anchor === anchor;
  closePopover();
  if (again) return () => {};          // a second click on the same button only closes
  const el = h('div', { class: `popover ${className}`, role, 'aria-label': label, tabindex: '-1' }, content);
  // Inside a modal dialog everything outside it is inert, so the panel lives in the dialog.
  const host = anchor.closest('dialog') || document.body;
  const native = typeof el.showPopover === 'function';
  if (native) el.setAttribute('popover', 'manual');
  host.append(el);
  if (native) {
    try { el.showPopover(); } catch { /* shown as a plain fixed box instead */ }
  }
  place(el, anchor, align);
  anchor.setAttribute('aria-expanded', 'true');

  const outside = (event) => {
    if (!el.contains(event.target) && !anchor.contains(event.target)) close(false);
  };
  const key = (event) => {
    if (event.key !== 'Escape') return;
    // Handled here: the same key must not also close the dialog underneath.
    event.preventDefault();
    event.stopPropagation();
    close(true);
  };
  const gone = () => close(false);
  function close(refocus) {
    if (current !== close) return;
    current = null;
    document.removeEventListener('pointerdown', outside, true);
    document.removeEventListener('keydown', key, true);
    window.removeEventListener('resize', gone);
    window.removeEventListener('hashchange', gone);
    el.remove();
    anchor.setAttribute('aria-expanded', 'false');
    if (refocus && anchor.isConnected) anchor.focus();
  }
  close.anchor = anchor;
  current = close;
  document.addEventListener('pointerdown', outside, true);
  document.addEventListener('keydown', key, true);
  window.addEventListener('resize', gone);
  window.addEventListener('hashchange', gone);
  if (focus) el.focus({ preventScroll: true });
  return close;
}

// items: [{ label, icon?, run?, href?, danger?, checked?, count?, disabled? } | 'sep'].
// Arrow keys move, Enter or Space chooses, Tab leaves and closes.
export function openMenu(anchor, items, { label, align = 'start' } = {}) {
  let close = () => {};
  const radio = items.some((item) => item !== 'sep' && item.checked !== undefined);
  const nodes = items.map((item) => {
    if (item === 'sep') return h('div', { class: 'menu-sep', role: 'separator' });
    const attrs = {
      class: `menu-item ${item.danger ? 'danger' : ''}`, role: radio ? 'menuitemradio' : 'menuitem', tabindex: '-1',
      'aria-checked': radio ? String(Boolean(item.checked)) : null,
    };
    const inner = [
      radio ? icon(item.checked ? 'check' : 'dot', item.checked ? '' : 'visually-hidden') : (item.icon ? icon(item.icon) : null),
      h('span', null, item.label),
      item.count != null ? h('span', { class: 'count' }, String(item.count)) : null,
    ];
    if (item.href) return h('a', { ...attrs, href: item.href, onclick: () => close(false) }, inner);
    return h('button', { ...attrs, type: 'button', disabled: item.disabled, onclick: () => { close(true); if (item.run) item.run(); } }, inner);
  });
  const choices = nodes.filter((node) => node.classList.contains('menu-item') && !node.disabled);
  const list = h('div', {
    onkeydown: (event) => {
      const index = choices.indexOf(document.activeElement);
      const to = { ArrowDown: index + 1, ArrowUp: index - 1, Home: 0, End: choices.length - 1 }[event.key];
      if (event.key === 'Tab') { close(false); return; }
      if (to === undefined) return;
      event.preventDefault();
      choices[(to + choices.length) % choices.length].focus();
    },
  }, nodes);
  close = openPopover(anchor, list, { role: 'menu', label, className: 'menu', align });
  const start = choices.find((node) => node.getAttribute('aria-checked') === 'true') || choices[0];
  if (start) start.focus({ preventScroll: true });
  return close;
}

// A button that opens a menu. `items` may be a function, read each time it opens.
export function menuButton(content, items, { label, kind = '', align = 'start' } = {}) {
  const button = h('button', {
    type: 'button', class: `btn ${kind}`, 'aria-haspopup': 'menu', 'aria-expanded': 'false', 'aria-label': label,
    title: typeof content === 'string' ? null : label,
    onclick: () => openMenu(button, typeof items === 'function' ? items() : items, { label, align }),
  }, content);
  return button;
}
