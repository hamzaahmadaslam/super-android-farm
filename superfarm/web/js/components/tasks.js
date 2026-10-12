// The task drawer: every long operation the daemon runs, with progress, per-device
// results and a cancel button. Works by polling; a push on /api/events only makes the
// next poll happen sooner.

import { get, post } from '../api.js';
import { ago, h, replace } from '../dom.js';
import { attempt, fail, ok, warn } from './toast.js';

const ACTIVE = new Set(['queued', 'running']);
const KIND = { done: 'ok', failed: 'fail', cancelled: 'warn', running: 'info', queued: '' };
const watched = new Map();      // task id -> [resolve]: callers waiting for the end of a task
let tasks = [];
let timer = 0;
let drawer, button, count;
let wired = false;
let signedIn = false;

export const isOpen = () => drawer && !drawer.hidden;

export function initTasks() {
  drawer = document.getElementById('tasks-drawer');
  button = document.getElementById('tasks-button');
  count = document.getElementById('tasks-count');
  if (!wired) {
    wired = true;
    button.addEventListener('click', () => toggle());
    drawer.addEventListener('keydown', (event) => { if (event.key === 'Escape') toggle(false); });
    // Polling pauses in a hidden tab; pick it up again the moment the tab is back.
    document.addEventListener('visibilitychange', () => { if (!document.hidden && signedIn) refresh(); });
  }
  signedIn = true;
  refresh();
}

export function stopTasks() {
  signedIn = false;
  clearTimeout(timer);
  tasks = [];
  watched.clear();
  if (drawer) toggle(false);
}

export function toggle(open = drawer.hidden) {
  drawer.hidden = !open;
  button.setAttribute('aria-expanded', String(open));
  if (open) {
    draw();
    refresh();
    drawer.querySelector('button').focus();
  }
}

// Called by api.js whenever a response carries a task.
export function track(task) {
  watched.set(task.id, watched.get(task.id) || []);
  ok(`Started: ${task.title}`, { action: { label: 'Show', run: () => toggle(true) } });
  merge(task);
  refresh();
}

// Resolves with the finished task, so a page can reload its data afterwards.
export function finished(task) {
  if (!task || !ACTIVE.has(task.state)) return Promise.resolve(task);
  return new Promise((resolve) => {
    watched.set(task.id, [...(watched.get(task.id) || []), resolve]);
    refresh();
  });
}

function merge(task) {
  const index = tasks.findIndex((existing) => existing.id === task.id);
  if (index >= 0) tasks[index] = task;
  else tasks.unshift(task);
}

export async function refresh() {
  clearTimeout(timer);
  if (!signedIn) return;
  try {
    tasks = (await get('/api/tasks')).tasks;
  } catch {
    // Keep what we have; the next turn tries again.
  }
  for (const task of tasks) {
    if (watched.has(task.id) && !ACTIVE.has(task.state)) announce(task);
  }
  const active = tasks.filter((task) => ACTIVE.has(task.state)).length;
  count.hidden = active === 0;
  count.textContent = String(active);
  if (isOpen()) draw();
  if (signedIn && (!document.hidden || active)) timer = setTimeout(refresh, active || watched.size || isOpen() ? 1500 : 10000);
}

function announce(task) {
  const waiting = watched.get(task.id) || [];
  watched.delete(task.id);
  const failures = Object.entries(task.results || {}).filter(([, result]) => !result.ok);
  const show = { action: { label: 'Details', run: () => toggle(true) } };
  if (task.state === 'failed') fail(`${task.title}: ${task.error || 'failed'}`, show);
  else if (task.state === 'cancelled') warn(`${task.title}: cancelled`, show);
  else if (failures.length) fail(`${task.title}: ${failures.length} of ${Object.keys(task.results).length} failed. Android ${failures[0][0]}: ${failures[0][1].detail}`, show);
  else ok(`${task.title}: done`);
  for (const resolve of waiting) resolve(task);
}

function draw() {
  const open = new Set([...drawer.querySelectorAll('details[open]')].map((node) => node.dataset.task));
  replace(drawer,
    h('div', { class: 'drawer-head' },
      h('h2', null, 'Tasks'),
      h('button', { type: 'button', class: 'btn small', onclick: () => toggle(false) }, 'Close')),
    h('div', { class: 'drawer-body' },
      tasks.length === 0 && h('div', { class: 'empty' }, h('strong', null, 'No tasks yet'),
        'Starting Androids, installing apps, backups and automation runs show up here with their progress.'),
      tasks.map((task) => card(task, open.has(task.id)))));
}

function card(task, open) {
  const results = Object.entries(task.results || {});
  const active = ACTIVE.has(task.state);
  return h('div', { class: 'task' },
    h('div', { class: 'task-head' },
      h('strong', null, task.title || task.kind),
      h('span', { class: `badge ${KIND[task.state] || ''}` }, task.state),
      active && h('button', {
        type: 'button', class: 'btn small danger',
        onclick: () => attempt(() => post(`/api/tasks/${encodeURIComponent(task.id)}/cancel`).then(refresh), 'Cancel requested'),
      }, 'Cancel')),
    task.total > 0 && h('progress', { max: task.total, value: task.current, 'aria-label': `${task.current} of ${task.total}` }),
    h('div', { class: 'small muted' },
      [task.message && !task.error ? task.message : '', task.error, ago(task.finished || task.started || task.created)].filter(Boolean).join(' · ')),
    results.length > 0 && h('details', { open, dataset: { task: task.id } },
      h('summary', null, `${results.length} device result(s)${task.failed ? `, ${task.failed} failed` : ''}`),
      h('ul', null, results.map(([id, result]) => h('li', { class: 'small' },
        h('span', { class: `badge ${result.ok ? 'ok' : 'fail'}` }, `Android ${id}`), ' ', result.detail)))));
}
