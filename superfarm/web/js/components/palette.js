// The command palette: one box that jumps to any page or any Android (by name, id or
// tag) and runs the common actions. Opened with Ctrl/Cmd+K or "/", driven entirely from
// the keyboard: type, arrows to move, Enter to run, Escape to close.
//
// It is a combobox over a listbox (the WAI-ARIA pattern): focus stays in the text box
// and the highlighted row is announced through aria-activedescendant.

import { get } from '../api.js';
import { h, replace, shield, uid } from '../dom.js';
import { icon } from '../icons.js';
import { fleet, rememberInstances } from '../state.js';

const MOST_ANDROIDS = 40;
let provider = () => [];
let dialog = null;

// commands(): [{ group, title, hint?, icon, keywords?, run }], read each time it opens.
export function initPalette(commands) {
  provider = commands;
}

// How well `words` (already lower-case) match an entry; 0 = not at all.
function score(entry, words) {
  let total = 0;
  for (const word of words) {
    const title = entry.titleLower;
    let best = 0;
    if (title === word) best = 100;
    else if (title.startsWith(word)) best = 80;
    else if (title.split(/[\s·:#/-]+/).some((part) => part.startsWith(word))) best = 60;
    else if (title.includes(word)) best = 40;
    else if (entry.keywordsLower.includes(word)) best = 25;
    if (!best) return 0;
    total += best;
  }
  return total + (entry.boost || 0);
}

function androidEntries(go) {
  return fleet.instances.map((i) => ({
    group: 'Androids',
    title: `${i.name}`,
    hint: [`#${i.id}`, i.state, ...(i.tags || []).map((tag) => `tag:${tag}`)].join(' · '),
    icon: 'device',
    keywords: [`${i.id}`, `#${i.id}`, `android ${i.id}`, i.state, ...(i.tags || []), ...(i.tags || []).map((tag) => `tag:${tag}`)].join(' '),
    id: i.id,
    run: () => go(`android/${i.id}`),
  }));
}

export function openPalette(go) {
  if (dialog) return;
  const listId = uid('pal');
  let entries = [];
  let shown = [];
  let active = 0;

  const input = h('input', {
    type: 'text', role: 'combobox', autocomplete: 'off', spellcheck: false,
    placeholder: 'Go to a page or an Android, or run an action…',
    'aria-label': 'Search pages, Androids and actions', 'aria-expanded': 'true', 'aria-controls': listId, 'aria-autocomplete': 'list',
  });
  const list = h('div', { id: listId, class: 'palette-list', role: 'listbox', 'aria-label': 'Results' });

  const collect = () => {
    entries = [...provider(), ...androidEntries(go)].map((entry) => ({
      ...entry, titleLower: entry.title.toLowerCase(), keywordsLower: `${entry.keywords || ''} ${entry.hint || ''}`.toLowerCase(),
    }));
  };

  function mark() {
    shown.forEach((entry, index) => entry.node.setAttribute('aria-selected', String(index === active)));
    const current = shown[active];
    if (current) {
      input.setAttribute('aria-activedescendant', current.node.id);
      current.node.scrollIntoView({ block: 'nearest' });
    } else {
      input.removeAttribute('aria-activedescendant');
    }
  }

  function draw() {
    const words = input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    let matched;
    if (words.length) {
      matched = entries.map((entry) => ({ entry, rank: score(entry, words) + (entry.id != null && words.length === 1 && String(entry.id) === words[0] ? 200 : 0) }))
        .filter((hit) => hit.rank > 0).sort((a, b) => b.rank - a.rank).map((hit) => hit.entry);
    } else {
      matched = entries;
    }
    // Grouped in a fixed order; within a group the best match comes first.
    const order = ['Actions', 'Go to', 'Androids'];
    const groups = order.map((name) => [name, matched.filter((entry) => entry.group === name)]).filter(([, rows]) => rows.length);
    shown = [];
    const nodes = [];
    for (const [name, rows] of groups) {
      const limit = name === 'Androids' ? (words.length ? MOST_ANDROIDS : 6) : rows.length;
      nodes.push(h('div', { class: 'palette-group', role: 'presentation' }, name));
      for (const entry of rows.slice(0, limit)) {
        const index = shown.length;
        const node = h('div', {
          class: 'palette-item', role: 'option', id: `${listId}-${index}`, 'aria-selected': 'false',
          onclick: () => run(index),
          onpointermove: () => { if (active !== index) { active = index; mark(); } },
        }, icon(entry.icon || 'dot'), h('span', { class: 'title' }, entry.title), entry.hint && h('span', { class: 'hint' }, entry.hint));
        shown.push({ ...entry, node });
        nodes.push(node);
      }
      if (rows.length > limit) {
        nodes.push(h('div', { class: 'palette-group', role: 'presentation' }, `and ${rows.length - limit} more: type a name, an id or a tag`));
      }
    }
    replace(list, nodes.length ? nodes : h('div', { class: 'palette-empty', role: 'presentation' },
      `Nothing matches "${input.value.trim()}". Try a page name, an Android's name, id or tag.`));
    active = 0;
    mark();
  }

  // One Enter (or click) runs one command. When the palette closes the focus is back on
  // the page with the key still down, so input is shielded for a moment (dom.js).
  let ran = false;
  function run(index) {
    const entry = shown[index];
    if (!entry || ran) return;
    ran = true;
    dialog.close();
    shield(400);
    entry.run();
  }

  input.addEventListener('input', draw);
  input.addEventListener('keydown', (event) => {
    const step = { ArrowDown: 1, ArrowUp: -1 }[event.key];
    if (step && shown.length) {
      event.preventDefault();
      active = (active + step + shown.length) % shown.length;
      mark();
    } else if (event.key === 'Home' && shown.length) { event.preventDefault(); active = 0; mark(); }
    else if (event.key === 'End' && shown.length) { event.preventDefault(); active = shown.length - 1; mark(); }
    else if (event.key === 'PageDown' && shown.length) { event.preventDefault(); active = Math.min(shown.length - 1, active + 8); mark(); }
    else if (event.key === 'PageUp' && shown.length) { event.preventDefault(); active = Math.max(0, active - 8); mark(); }
    else if (event.key === 'Enter') { event.preventDefault(); run(active); }
  });

  dialog = h('dialog', { class: 'palette', 'aria-label': 'Command palette' },
    h('div', { class: 'palette-input' }, icon('search'), input, h('kbd', null, 'Esc')),
    list,
    h('div', { class: 'palette-foot' },
      h('span', null, h('kbd', null, '↑'), h('kbd', null, '↓'), 'move'),
      h('span', null, h('kbd', null, 'Enter'), 'run'),
      h('span', null, h('kbd', null, 'Esc'), 'close')));
  const self = dialog;
  // A click on the dimmed background closes it, like every command palette.
  dialog.addEventListener('pointerdown', (event) => { if (event.target === self) self.close(); });
  dialog.addEventListener('close', () => { self.remove(); if (dialog === self) dialog = null; });
  document.body.append(dialog);
  collect();
  draw();
  dialog.showModal();
  input.focus();

  // The fleet may have changed since a page last read it.
  get('/api/instances').then((data) => {
    if (dialog !== self) return;
    rememberInstances(data.instances);
    const keep = shown[active] && shown[active].title;
    collect();
    draw();
    const again = shown.findIndex((entry) => entry.title === keep);
    if (again > 0) { active = again; mark(); }
  }).catch(() => {});
}

export const paletteOpen = () => Boolean(dialog);
