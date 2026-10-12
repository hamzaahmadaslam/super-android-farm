// Keyboard shortcuts for the whole panel, and the sheet that lists them ("?").
//
// Single keys only act when the operator is not typing: never inside a text field, a
// select, or the live view of a device (which sends every key to Android), and never
// while a dialog is open. "g" followed by a letter goes to a page.

import { h } from '../dom.js';
import { openDialog } from './dialog.js';

const PREFIX_MS = 1500;
const pageKeys = new Map();        // key -> { label, run }: registered by the page on screen
let config = { pages: {}, palette() {}, global: [] };
let prefix = false;
let prefixTimer = 0;
let sheetOpen = false;

const typing = (target) => target instanceof Element && Boolean(
  target.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"], .live-surface'));

// pages: { d: ['dashboard', 'Dashboard'] }; global: [{ key, label, run }].
export function initShortcuts(options) {
  config = options;
  document.addEventListener('keydown', onKey);
}

// A shortcut that exists only while one page is on screen.
export function pageShortcut(scope, key, label, run) {
  pageKeys.set(key, { label, run });
  scope.add(() => { if (pageKeys.get(key) && pageKeys.get(key).run === run) pageKeys.delete(key); });
}

function onKey(event) {
  if (event.defaultPrevented) return;
  if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    config.palette();
    return;
  }
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (typing(event.target) || document.querySelector('dialog[open]')) return;
  const key = event.key;
  if (prefix) {
    prefix = false;
    clearTimeout(prefixTimer);
    const page = config.pages[key.toLowerCase()];
    if (page) { event.preventDefault(); config.go(page[0]); }
    return;
  }
  if (key === 'g') {
    prefix = true;
    clearTimeout(prefixTimer);
    prefixTimer = setTimeout(() => { prefix = false; }, PREFIX_MS);
    return;
  }
  if (key === '/') { event.preventDefault(); config.palette(); return; }
  if (key === '?') { event.preventDefault(); showShortcuts(); return; }
  const hit = pageKeys.get(key) || config.global.find((item) => item.key === key);
  if (hit) { event.preventDefault(); hit.run(); }
}

const keys = (...names) => h('dd', null, names.map((name, i) => [i ? h('span', null, 'then') : null, h('kbd', null, name)]));
const row = (label, ...names) => h('div', { class: 'cheat' }, h('dt', null, label), keys(...names));

export function showShortcuts() {
  if (sheetOpen) return;
  sheetOpen = true;
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || '');
  const page = [...pageKeys.entries()];
  openDialog({
    title: 'Keyboard shortcuts',
    icon: 'keyboard',
    wide: true,
    body: h('div', { class: 'cheats' },
      h('section', null, h('h3', null, 'Anywhere'), h('dl', null,
        h('div', { class: 'cheat' }, h('dt', null, 'Search and run commands'), h('dd', null, h('kbd', null, mac ? '⌘ K' : 'Ctrl K'), h('span', null, 'or'), h('kbd', null, '/'))),
        row('This list', '?'),
        config.global.map((item) => row(item.label, item.key)),
        row('Close a dialog, a menu or the task list', 'Esc'))),
      h('section', null, h('h3', null, 'Go to'), h('dl', null,
        Object.entries(config.pages).map(([key, [, label]]) => row(label, 'g', key)))),
      page.length ? h('section', null, h('h3', null, 'On this page'), h('dl', null, page.map(([key, item]) => row(item.label, key)))) : null,
      h('section', null, h('h3', null, 'In a live view'), h('dl', null,
        row('Type on the device', 'any key'),
        row('Back', 'Esc'),
        row('Leave the live view', 'Tab'),
        row('Paste text to the device', mac ? '⌘ V' : 'Ctrl V')))),
  }).then(() => { sheetOpen = false; });
}
