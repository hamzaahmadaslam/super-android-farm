// Tabs following the WAI-ARIA pattern: arrow keys move between tabs, only the active
// tab is in the tab order, and each panel is rendered when it is first shown.

import { h, replace, scope, uid } from '../dom.js';

// items: [{ id, label, render(panel, scope) }]. Returns { el, show(id) }.
// Each panel gets its own scope so its timers stop when another tab is opened.
export function tabs(items, { active, onChange, parent } = {}) {
  const base = uid('tab');
  const panel = h('div', { role: 'tabpanel', tabindex: '0' });
  let inner = null;
  const buttons = items.map((item) => h('button', {
    type: 'button', role: 'tab', id: `${base}-${item.id}`, 'aria-selected': 'false', tabindex: '-1',
    onclick: () => show(item.id, true),
  }, item.label));
  const list = h('div', { class: 'tabs', role: 'tablist' }, buttons);
  list.addEventListener('keydown', (event) => {
    const index = buttons.indexOf(document.activeElement);
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (index < 0 || !step) return;
    event.preventDefault();
    const next = buttons[(index + step + buttons.length) % buttons.length];
    next.focus();
    next.click();
  });

  function show(id, notify = false) {
    const index = Math.max(0, items.findIndex((item) => item.id === id));
    buttons.forEach((button, i) => {
      button.setAttribute('aria-selected', String(i === index));
      button.tabIndex = i === index ? 0 : -1;
    });
    if (inner) inner.dispose();
    inner = scope();
    panel.setAttribute('aria-labelledby', buttons[index].id);
    replace(panel);
    const mine = inner;
    Promise.resolve(items[index].render(panel, inner)).catch((error) => {
      if (inner === mine) panel.append(h('div', { class: 'banner fail', role: 'alert' }, error.message));
    });
    if (notify && onChange) onChange(items[index].id);
  }

  if (parent) parent.add(() => inner && inner.dispose());
  show(active);
  return { el: h('div', null, list, panel), show };
}
