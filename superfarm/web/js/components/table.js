// A table with optional multi-select, and the selector box that goes with it.
//
// update(rows) redraws the body only. The selection lives outside the DOM, so a
// refresh every few seconds never clears what the operator ticked, and keyboard focus
// is put back on the same row's control.

import { h, parseSelector, replace, selectorOf, uid } from '../dom.js';

// columns: [{ title, cell(row) -> node | text, class? }]
export function dataTable({ columns, key = (row) => row.id, selectable = false, empty, label }) {
  const selection = new Set();
  const listeners = [];
  let rows = [];
  const all = h('input', { type: 'checkbox', 'aria-label': 'Select all', onchange: () => {
    setSelection(all.checked ? rows.map(key) : []);
  } });
  const body = h('tbody');
  const table = h('table', { 'aria-label': label },
    h('thead', null, h('tr', null,
      selectable && h('th', { class: 'check', scope: 'col' }, all),
      columns.map((column) => h('th', { scope: 'col', class: column.class }, column.title)))),
    body);
  const wrap = h('div', { class: 'table-wrap' }, table);
  const placeholder = h('div');
  const el = h('div', null, wrap, placeholder);

  function notify() {
    for (const fn of listeners) fn(selection);
  }

  function draw() {
    const active = document.activeElement;
    const focusKey = body.contains(active) ? active.dataset.focus : null;
    replace(body, rows.map((row) => {
      const id = key(row);
      const box = selectable && h('input', {
        type: 'checkbox', checked: selection.has(id), 'aria-label': `Select ${row.name || id}`,
        dataset: { focus: `check:${id}` },
        onchange: () => { selection[box.checked ? 'add' : 'delete'](id); sync(); notify(); },
      });
      const tr = h('tr', { 'aria-selected': selectable ? String(selection.has(id)) : null, dataset: { key: String(id) } },
        selectable && h('td', { class: 'check' }, box),
        columns.map((column) => h('td', { class: column.class }, column.cell(row))));
      return tr;
    }));
    if (focusKey) {
      const again = [...body.querySelectorAll('[data-focus]')].find((node) => node.dataset.focus === focusKey);
      if (again) again.focus({ preventScroll: true });
    }
    wrap.hidden = rows.length === 0;
    replace(placeholder, rows.length === 0 ? empty : null);
    sync();
  }

  // Tick marks and row highlight without a redraw, so a click never loses focus.
  function sync() {
    for (const tr of body.children) {
      const picked = selection.has(rowKey(tr));
      if (selectable) tr.setAttribute('aria-selected', String(picked));
      const box = tr.querySelector('td.check input');
      if (box) box.checked = picked;
    }
    const count = rows.filter((row) => selection.has(key(row))).length;
    all.checked = rows.length > 0 && count === rows.length;
    all.indeterminate = count > 0 && count < rows.length;
  }

  const rowKey = (tr) => {
    const row = rows.find((candidate) => String(key(candidate)) === tr.dataset.key);
    return row ? key(row) : tr.dataset.key;
  };

  function setSelection(ids) {
    selection.clear();
    for (const id of ids) selection.add(id);
    sync();
    notify();
  }

  return {
    el,
    selection,
    get rows() { return rows; },
    update(next) {
      rows = next;
      // Something deleted elsewhere must not stay selected invisibly.
      const known = new Set(rows.map(key));
      let dropped = false;
      for (const id of [...selection]) {
        if (!known.has(id)) { selection.delete(id); dropped = true; }
      }
      draw();
      if (dropped) notify();
    },
    setSelection,
    onSelection(fn) { listeners.push(fn); },
  };
}

// A text box that shows the current selection as a selector ("1-4,9") and accepts one
// typed by hand ("running,!tag:spare"). `target` is anything with selection,
// setSelection and onSelection (a dataTable, or the device wall).
export function selectorBox(target, instances) {
  const id = uid('sel');
  const input = h('input', {
    id, class: 'selector', type: 'text', spellcheck: false, autocomplete: 'off',
    placeholder: 'all, running, tag:name, 1-10, !3',
    'aria-describedby': `${id}-hint`,
  });
  const hint = h('span', { id: `${id}-hint`, class: 'small muted' }, 'none selected');
  const apply = () => {
    try {
      target.setSelection(parseSelector(input.value, instances()));
      input.setCustomValidity('');
    } catch (error) {
      input.setCustomValidity(error.message);
      input.reportValidity();
    }
  };
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); apply(); }
  });
  input.addEventListener('change', apply);
  target.onSelection((selection) => {
    if (document.activeElement !== input) input.value = selectorOf(selection);
    hint.textContent = selection.size ? `${selection.size} selected` : 'none selected';
  });
  return h('span', { class: 'row grow selector-wrap' },
    h('label', { for: id, class: 'visually-hidden' }, 'Selection (selector)'), input, hint);
}
