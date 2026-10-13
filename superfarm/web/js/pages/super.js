// Super Control: the operations centre. Everything the farm is doing on one screen, and
// the controls to act on it without leaving.
//
//   top       six figures: Androids, online, running tasks, stopped, what needs
//             attention, host load
//   units     every Android as a card with a live still; a click shows it in the live
//             feed, the tick box adds it to the selection for start, stop and restart
//   live      the focused Android, interactive: touch it, type, press its buttons.
//             With "broadcast" on, the same input goes to every ticked running Android
//   below     host resources, capacity, the task queue, the fleet by state, the
//             network, a timeline of what changed, alerts, and a command console
//
// Each panel can be switched off and on; the choice is remembered in this browser.
//
// Nothing here is special: it reads the routes the other pages read and acts through
// the same helpers, so the same rules hold. A fleet action asks before a risky step,
// input only reaches running Androids, and a request from an AI assistant is answered
// in the inbox only, never from here. Text from a device, a task or a model is shown
// as text.

import { get, post } from '../api.js';
import { approvals, subscribe as onApprovals } from '../approvals.js';
import {
  badge, button, deviceActions, emptyState, fleetAction, h264Offered, limitName, limitText, linkButton, loadInstances, networkOf, pageHead, plural,
  renderingOf, stateBadge, UP,
} from '../common.js';
import { donut, gauge, series, sparkline } from '../components/charts.js';
import { liveView, thumbnails } from '../components/live.js';
import { toggle as toggleTasks } from '../components/tasks.js';
import { attempt, fail } from '../components/toast.js';
import { h, mb, parseSelector, replace, scope as newScope, selectorOf } from '../dom.js';
import { icon, svg } from '../icons.js';
import { getPref, setPref } from '../prefs.js';
import { rememberInstances } from '../state.js';

const LIVE = new Set(['running', 'booted']);
const ACTIVE = new Set(['queued', 'running']);
const LINES_MAX = 150;
// row: which band of the page a panel sits in. Panels of one band share its width.
const PANELS = [
  { key: 'units', label: 'Units', icon: 'devices', row: 0 },
  { key: 'live', label: 'Live feed', icon: 'control', row: 0 },
  { key: 'resources', label: 'Resources', icon: 'system', row: 1 },
  { key: 'capacity', label: 'Capacity', icon: 'gauge', row: 1 },
  { key: 'tasks', label: 'Tasks', icon: 'tasks', row: 1 },
  { key: 'status', label: 'Fleet status', icon: 'layers', row: 1 },
  { key: 'network', label: 'Network', icon: 'network', row: 2 },
  { key: 'timeline', label: 'Timeline', icon: 'clock', row: 2 },
  { key: 'alerts', label: 'Alerts', icon: 'alert', row: 2 },
  { key: 'console', label: 'Console', icon: 'terminal', row: 2 },
];
const FILTERS = [
  ['all', 'All', () => true],
  ['up', 'Running', (i) => UP.has(i.state)],
  ['down', 'Stopped', (i) => !UP.has(i.state) && i.state !== 'error'],
  ['error', 'Errors', (i) => i.state === 'error'],
];
const STATUS_PARTS = [
  { kind: 'ok', label: 'booted', states: ['booted'] },
  { kind: 'info', label: 'starting', states: ['running', 'starting'] },
  { kind: 'idle', label: 'stopped', states: ['stopped', 'created', 'missing'] },
  { kind: 'fail', label: 'error', states: ['error'] },
];
const TASK_ICON = { done: 'check', failed: 'x', cancelled: 'x', running: 'spinner', queued: 'clock' };
const HELP = [
  ['help', 'this list'],
  ['select <selector>', 'tick Androids: all, running, stopped, tag:name, 3, 1-5, !2, or none'],
  ['focus <id>', 'show one Android in the live feed'],
  ['start | stop | restart [selector]', 'act on the selector, or on the ticked Androids'],
  ['key <NAME>', 'send a key (HOME, BACK, ENTER, ...) to where input goes'],
  ['type <text>', 'type text where input goes'],
  ['status', 'the farm in one line'],
  ['clear', 'empty this console'],
];

// Samples outlive the page, so coming back continues the same lines.
const history = { cpu: series(), memory: series(), gpu: series(), vram: series() };
const two = (n) => String(n).padStart(2, '0');
const timeOf = (date) => `${two(date.getHours())}:${two(date.getMinutes())}:${two(date.getSeconds())}`;
const clamp = (value) => Math.min(1, Math.max(0, value || 0));
const tone = (ratio) => (ratio >= 0.9 ? 'fail' : ratio >= 0.75 ? 'warn' : 'ok');
const clip = (words, most = 140) => (words.length > most ? `${words.slice(0, most)}…` : words);

// The cards of a machine as one figure: the average load of those that report one, the
// busiest of them, and the video memory of all cards that state theirs.
function gpuLoad(metrics) {
  const cards = (metrics.gpus || []).filter((g) => g.util_percent != null);
  const withMemory = (metrics.gpus || []).filter((g) => g.vram_total_mb);
  return {
    cards: cards.length,
    util: cards.length ? cards.reduce((sum, g) => sum + g.util_percent, 0) / cards.length : null,
    busiest: cards.reduce((top, g) => (!top || g.util_percent > top.util_percent ? g : top), null),
    vramCards: withMemory.length,
    vramUsed: withMemory.reduce((sum, g) => sum + (g.vram_used_mb || 0), 0),
    vramTotal: withMemory.reduce((sum, g) => sum + g.vram_total_mb, 0),
  };
}

// Redraw a part of the page only when what it shows changed: a poll every few seconds
// must not take the keyboard focus or a half-typed word away.
const drawnAs = new WeakMap();
function once(el, key, draw) {
  const now = JSON.stringify(key);
  if (drawnAs.get(el) === now) return;
  drawnAs.set(el, now);
  draw();
}

function miniRing() {
  const radius = 17;
  const length = 2 * Math.PI * radius;
  const value = svg('circle', { class: 'value', cx: 22, cy: 22, r: radius, 'stroke-dasharray': length.toFixed(1), 'stroke-dashoffset': length.toFixed(1) });
  const el = svg('svg', { class: 'sc-ring', viewBox: '0 0 44 44', 'aria-hidden': 'true' }, svg('circle', { class: 'track', cx: 22, cy: 22, r: radius }), value);
  return { el, set(ratio) { value.setAttribute('stroke-dashoffset', (length * (1 - clamp(ratio))).toFixed(1)); } };
}

function miniBars(count = 18) {
  const bars = Array.from({ length: count }, () => h('span'));
  const el = h('span', { class: 'sc-bars', 'aria-hidden': 'true' }, bars);
  return { el, set(values) {
    const tail = values.slice(-count);
    const top = Math.max(10, ...tail);
    bars.forEach((bar, index) => {
      const value = tail[index - (count - tail.length)];
      bar.style.height = `${value == null ? 8 : Math.max(8, Math.round((value / top) * 100))}%`;
      bar.classList.toggle('off', value == null);
    });
  } };
}

function kpi(label, visual, { href, onclick } = {}) {
  const value = h('span', { class: 'v' }, '–');
  const sub = h('span', { class: 'sub' });
  const el = h(href ? 'a' : onclick ? 'button' : 'div', { class: 'sc-kpi', href: href || null, type: onclick ? 'button' : null, onclick: onclick || null },
    h('span', { class: 'sc-kpi-text' }, h('span', { class: 'k' }, label), value, sub), visual);
  return { el, set(text, under, kind = '') {
    value.textContent = text;
    sub.textContent = under;
    sub.title = under;
    el.dataset.tone = kind;
  } };
}

const tileIcon = (name, kind) => h('span', { class: 'sc-kpi-icon', dataset: { tone: kind } }, icon(name));

function panelBox(key, title, extras, ...body) {
  return h('section', { class: `sc-panel sc-${key}`, 'aria-label': title },
    h('header', { class: 'sc-head' }, h('h2', null, title), extras), body);
}

export async function render(root, _params, scope) {
  let instances = [];
  let metrics = null;
  let capacity = null;
  let tasks = [];
  let gateways = null;
  let checks = null;
  let focus = getPref('super-focus', 0);
  let filter = 'all';
  let broadcast = false;
  // Small cards once the fleet is large, unless the operator chose a size.
  let dense = getPref('super-dense', null);
  const shown = new Set(getPref('super-panels', PANELS.map((panel) => panel.key)));
  const selection = new Set();
  const thumbs = thumbnails(scope, { width: 240 });
  const system = await get('/api/system').catch(() => ({}));
  const h264 = h264Offered(system);

  const byId = (id) => instances.find((i) => i.id === id);
  const selected = () => [...selection].sort((a, b) => a - b);
  const selectedLive = () => selected().filter((id) => { const i = byId(id); return i && LIVE.has(i.state); });
  // Where a touch, a key or typed text goes: the focused Android, or every ticked
  // running one while broadcast is on.
  const targets = () => {
    if (broadcast && selectedLive().length) return selectedLive();
    const one = byId(focus);
    return one && LIVE.has(one.state) ? [one.id] : [];
  };

  // ------------------------------------------------------------- timeline
  const timeline = h('ol', { class: 'sc-events', 'aria-live': 'polite', 'aria-label': 'What changed' });
  const note = (kind, words, at = new Date()) => {
    timeline.prepend(h('li', { class: kind }, h('span', { class: 'at' }, timeOf(at)), h('span', { class: 'what' }, words)));
    while (timeline.children.length > LINES_MAX) timeline.lastChild.remove();
  };

  // ---------------------------------------------------------------- alerts
  function alerts() {
    const rows = [];
    for (const i of instances) {
      if (i.state === 'error' || i.config_error) rows.push({ kind: 'fail', title: `#${i.id} ${i.name}`, detail: clip(i.config_error || i.last_error || 'the last start failed'), href: `#/android/${i.id}` });
    }
    if (capacity && capacity.over) rows.push({ kind: 'warn', title: 'Capacity', detail: (capacity.reasons || []).join('; ') || 'at the limit of the estimate', href: '#/system' });
    if (approvals.count) rows.push({ kind: 'warn', title: 'Approvals', detail: `${plural(approvals.count, 'request')} from an AI assistant ${approvals.count === 1 ? 'waits' : 'wait'} for your answer`, href: '#/approvals' });
    for (const gateway of gateways || []) {
      if (gateway.state && gateway.state !== 'connected' && gateway.state !== 'stopped') rows.push({ kind: 'warn', title: `Gateway ${gateway.name}`, detail: clip(gateway.detail || gateway.state), href: '#/network' });
    }
    for (const check of checks ? checks.checks : []) {
      if (check.status === 'fail' || check.status === 'warn') rows.push({ kind: check.status, title: check.title, detail: clip(check.detail || ''), href: '#/system/checks' });
    }
    return rows.sort((a, b) => Number(b.kind === 'fail') - Number(a.kind === 'fail'));
  }

  // ------------------------------------------------------------------ head
  const clock = h('time', { class: 'sc-clock' });
  const verdict = h('span', { class: 'sc-verdict', role: 'status' });
  const tick = () => {
    const now = new Date();
    clock.textContent = `${now.getFullYear()}-${two(now.getMonth() + 1)}-${two(now.getDate())} ${timeOf(now)}`;
  };
  tick();
  const ticking = setInterval(tick, 1000);
  scope.add(() => clearInterval(ticking));

  const ring = miniRing();
  const bars = miniBars();
  const kpis = {
    total: kpi('Androids', tileIcon('devices', 'accent'), { href: '#/androids' }),
    online: kpi('Online', ring.el, { href: '#/control/running' }),
    tasks: kpi('Running tasks', tileIcon('play', 'info'), { onclick: () => toggleTasks(true) }),
    stopped: kpi('Stopped', tileIcon('power', 'idle'), { href: '#/androids' }),
    attention: kpi('Needs attention', tileIcon('alert', 'warn'), { href: '#/system/checks' }),
    host: kpi('Host load', bars.el, { href: '#/system' }),
  };

  function drawTop() {
    const live = instances.filter((i) => LIVE.has(i.state)).length;
    const down = instances.filter((i) => !UP.has(i.state) && i.state !== 'error').length;
    const max = capacity && capacity.estimate ? capacity.estimate.max_instances : null;
    kpis.total.set(String(instances.length), max == null ? 'defined' : `about ${max} fit here`);
    kpis.online.set(String(live), instances.length ? `${Math.round((100 * live) / instances.length)}% of fleet` : 'none', 'ok');
    ring.set(instances.length ? live / instances.length : 0);
    const active = tasks.filter((task) => ACTIVE.has(task.state));
    kpis.tasks.set(String(active.length), active.length ? active[0].title || active[0].kind : 'idle', active.length ? 'info' : '');
    kpis.stopped.set(String(down), down ? 'data kept' : 'all are up');
    const list = alerts();
    const critical = list.filter((row) => row.kind === 'fail').length;
    kpis.attention.set(String(list.length), list.length ? `${critical} critical · ${list.length - critical} warn` : 'all clear', critical ? 'fail' : list.length ? 'warn' : 'ok');
    if (metrics) {
      const host = metrics.host || {};
      const mem = host.memory || {};
      kpis.host.set(`${Math.round(host.cpu_percent || 0)}%`, `${mb(mem.used_mb)} memory`, tone((host.cpu_percent || 0) / 100));
      bars.set(history.cpu.values);
    }
    once(verdict, [critical, list.length], () => {
      verdict.dataset.tone = critical ? 'fail' : list.length ? 'warn' : 'ok';
      replace(verdict, h('span', { class: 'beacon', 'aria-hidden': 'true' }),
        critical ? `${plural(critical, 'critical alert')}` : list.length ? `${plural(list.length, 'warning')}` : 'All systems nominal');
    });
  }

  // ----------------------------------------------------------------- units
  const search = h('input', { type: 'search', placeholder: 'Filter by name, id or tag', 'aria-label': 'Filter Androids by name, id or tag', spellcheck: false, autocomplete: 'off' });
  const chips = h('div', { class: 'chips', role: 'group', 'aria-label': 'Show' });
  const grid = h('div', { class: 'sc-units-grid' });
  const sizes = [['Large cards', 'grid', false], ['Small cards, to see many at once', 'rows', true]].map(([label, picture, value]) =>
    h('button', { type: 'button', 'aria-label': label, title: label, 'aria-pressed': 'false', onclick: () => { dense = value; setPref('super-dense', value); drawUnits(); } }, icon(picture)));
  const sizeToggle = h('div', { class: 'segmented', role: 'group', 'aria-label': 'Card size' }, sizes);
  const isDense = () => (dense == null ? instances.length > 16 : dense);
  const selInfo = h('span', { class: 'sc-sel-info', role: 'status' });
  const cards = new Map();          // id -> { el, img, off, tag, info, shot, check, shown }

  const act = (action, ids) => attempt(async () => {
    if (!ids.length) throw new Error('Tick at least one Android first.');
    note('you', `${action}: ${plural(ids.length, 'Android')} (${selectorOf(ids)})`);
    await fleetAction(action, ids, instances);
    await readFleet();
  });
  const lifecycle = ['start', 'stop', 'restart'].map((action) => button(action[0].toUpperCase() + action.slice(1), () => act(action, selected()), 'small', action === 'start' ? 'play' : action));
  const matches = (i) => {
    const words = search.value.trim().toLowerCase();
    if (!FILTERS.find(([key]) => key === filter)[2](i)) return false;
    return !words || `${i.id} ${i.name} ${(i.tags || []).join(' ')}`.toLowerCase().includes(words);
  };
  const setSelection = (ids) => {
    const known = new Set(instances.map((i) => i.id));
    selection.clear();
    for (const id of ids) if (known.has(id)) selection.add(id);
    drawUnits();
    drawLiveSide();
  };
  const setFocus = (id) => {
    focus = id;
    setPref('super-focus', id);
    drawUnits();
    drawLive();
  };

  function card(i) {
    let c = cards.get(i.id);
    if (!c) {
      const img = thumbs.image(i.id, `Screen of ${i.name}`);
      const off = h('span', { class: 'sc-unit-off' }, icon('power'), h('span', null, 'no screen'));
      const tag = h('span', { class: 'sc-live-tag' }, 'live');
      const shot = h('button', { type: 'button', class: 'sc-unit-shot', onclick: () => setFocus(i.id) }, img, off, tag);
      const check = h('input', { type: 'checkbox', onchange: () => {
        const next = new Set(selection);
        next[check.checked ? 'add' : 'delete'](i.id);
        setSelection(next);
      } });
      const info = h('div', { class: 'sc-unit-info' });
      const el = h('article', { class: 'sc-unit', dataset: { id: String(i.id), thumbOwner: '1' } }, shot,
        h('label', { class: 'sc-unit-check', title: 'Tick to act on it with the others' }, check),
        h('a', { class: 'sc-unit-open', href: `#/android/${i.id}`, 'aria-label': `Open ${i.name}`, title: 'Open this Android' }, icon('open')), info);
      c = { el, img, off, tag, info, shot, check };
      cards.set(i.id, c);
    }
    const use = metrics && metrics.instances ? metrics.instances[i.id] : null;
    const live = LIVE.has(i.state);
    once(c.info, [i.name, i.state, i.busy, networkOf(i), renderingOf(i), use && Math.round(use.cpu), use && Math.round(use.mem_mb)], () => {
      c.img.hidden = !live;
      c.off.hidden = live;
      c.tag.hidden = !live;
      if (!live) c.img.removeAttribute('src');
      c.shot.setAttribute('aria-label', `Show ${i.name} in the live feed`);
      c.check.setAttribute('aria-label', `Tick ${i.name}`);
      replace(c.info,
        h('div', { class: 'sc-unit-id' }, h('span', { class: 'num' }, `#${i.id}`), h('span', { class: 'name', title: i.name }, i.name)),
        stateBadge(i),
        h('span', { class: 'sc-unit-line', title: i.busy ? `busy: ${i.busy}` : null }, i.busy ? `busy: ${i.busy}` : [networkOf(i), renderingOf(i)].filter(Boolean).join(' · ') || 'idle'),
        use ? h('span', { class: 'sc-use', title: `${use.cpu}% CPU, ${mb(use.mem_mb)}` }, h('span', { class: 'num' }, `${Math.round(use.cpu)}%`),
          h('span', { class: 'track' }, fillOf(Math.min(100, use.cpu))), h('span', { class: 'num mem' }, mb(use.mem_mb)))
          : h('span', { class: 'sc-unit-line' }, i.state === 'error' ? clip(i.last_error || 'the last start failed', 60) : 'not running'));
    });
    c.check.checked = selection.has(i.id);
    c.el.classList.toggle('on', selection.has(i.id));
    c.el.classList.toggle('focus', i.id === focus);
    if (i.id === focus) c.shot.setAttribute('aria-current', 'true'); else c.shot.removeAttribute('aria-current');
    return c.el;
  }
  function fillOf(percent) {
    const fill = h('span', { class: 'fill' });
    fill.style.width = `${Math.round(percent)}%`;
    return fill;
  }

  function drawUnits() {
    once(chips, [filter, ...FILTERS.map(([, , test]) => instances.filter(test).length)], () => replace(chips, FILTERS.map(([key, label, test]) => {
      const count = instances.filter(test).length;
      if (key === 'error' && !count && filter !== 'error') return null;
      return h('button', { type: 'button', class: 'chip', 'aria-pressed': String(filter === key), onclick: () => { filter = key; drawUnits(); } }, label, h('span', { class: 'count' }, count));
    })));
    const rows = instances.filter(matches).sort((a, b) => a.id - b.id);
    grid.classList.toggle('dense', isDense());
    sizes[0].setAttribute('aria-pressed', String(!isDense()));
    sizes[1].setAttribute('aria-pressed', String(isDense()));
    for (const [id, c] of cards) if (!byId(id)) { thumbs.forget(c.img); cards.delete(id); }
    once(grid, [rows.map((i) => i.id), instances.length], () => {
      if (rows.length) replace(grid, rows.map(card));
      else if (instances.length) replace(grid, emptyState('search', 'Nothing matches', 'No Android fits this filter. Change it, or clear the search box.'));
      else replace(grid, emptyState('device', 'No Androids yet', 'Create one, then start it; it appears here with its screen.', linkButton('New Android', '#/androids/new', 'primary', 'plus')));
    });
    rows.forEach(card);
    const withScreen = selectedLive().length;
    selInfo.textContent = selection.size ? `${plural(selection.size, 'Android')} ticked (${selectorOf(selected())}), ${withScreen} running` : 'Tick Androids to act on them together.';
    for (const one of lifecycle) one.disabled = !selection.size;
  }
  search.addEventListener('input', drawUnits);

  // ------------------------------------------------------------------ live
  let lastError = 0;
  const report = (error) => { if (Date.now() - lastError > 4000) { lastError = Date.now(); fail(error); } };
  const send = (kind, payload) => {
    const ids = targets();
    if (!ids.length) { report(new Error('No running Android to send input to. Pick a running one, or tick some and switch broadcast on.')); return; }
    post(`/api/input/${kind}`, { ids, ...payload }).then((result) => {
      const failed = Object.entries(result.results || {}).filter(([, r]) => !r.ok);
      if (failed.length) report(new Error(`Input did not reach ${failed.length} Android(s). Android ${failed[0][0]}: ${failed[0][1].detail || failed[0][1].error || 'failed'}`));
    }).catch(report);
  };

  const liveTitle = h('span', { class: 'sc-live-title' });
  const liveStage = h('div', { class: 'sc-live-stage' });
  const liveState = h('span');
  const liveFacts = h('div', { class: 'sc-facts' });
  const liveUse = h('div', { class: 'sc-live-use' });
  const liveTarget = h('p', { class: 'sc-target', role: 'status' });
  const typed = h('input', { type: 'text', class: 'grow', placeholder: 'Text to type', 'aria-label': 'Text to type on the Android', maxLength: 10000 });
  const cast = h('button', { type: 'button', class: 'switch', role: 'switch', 'aria-checked': 'false', 'aria-label': 'Broadcast input to every ticked running Android',
    onclick: () => { broadcast = !broadcast; drawLiveSide(); } }, h('span', { class: 'knob', 'aria-hidden': 'true' }), h('span', { class: 'switch-word' }, 'Broadcast to ticked'));
  const oneButtons = ['start', 'stop', 'restart'].map((action) => button(action[0].toUpperCase() + action.slice(1), () => act(action, byId(focus) ? [focus] : []), 'small', action === 'start' ? 'play' : action));
  const liveSide = h('div', { class: 'sc-live-side' },
    h('div', { class: 'row' }, liveState, liveFacts),
    liveUse,
    h('div', { class: 'sc-group' }, h('span', { class: 'sc-label' }, 'Input'), liveTarget, cast,
      h('form', { class: 'row', onsubmit: (event) => { event.preventDefault(); if (typed.value) { send('text', { value: typed.value }); typed.value = ''; } } },
        typed, h('button', { type: 'submit', class: 'btn small' }, icon('send'), 'Type'))),
    h('div', { class: 'sc-group' }, h('span', { class: 'sc-label' }, 'This Android'), h('div', { class: 'row' }, oneButtons)),
    h('div', { class: 'sc-group' }, h('span', { class: 'sc-label' }, 'Device setting'), deviceActions(() => { const ids = targets(); return ids.length ? selectorOf(ids) : ''; })));
  let liveScope = null;
  let liveKey = '';

  function drawLiveSide() {
    const i = byId(focus);
    const ids = targets();
    cast.setAttribute('aria-checked', String(broadcast));
    liveTarget.textContent = !ids.length ? 'Nothing to send input to: this Android is not running.'
      : broadcast && selectedLive().length ? `Input goes to ${plural(ids.length, 'ticked Android')} at once.`
        : broadcast ? 'Broadcast is on, but nothing running is ticked: input goes to this Android.' : 'Input goes to this Android only.';
    if (!i) { replace(liveState); replace(liveFacts); replace(liveUse); return; }
    once(liveState, [i.id, i.state], () => replace(liveState, stateBadge(i)));
    once(liveFacts, [i.id, networkOf(i), renderingOf(i), i.tags], () => replace(liveFacts,
      networkOf(i) && h('span', { class: 'info-chip' }, icon('network'), h('span', { class: 'v' }, networkOf(i))),
      renderingOf(i) && h('span', { class: 'info-chip' }, icon('layers'), h('span', { class: 'v' }, renderingOf(i))),
      (i.tags || []).map((name) => h('span', { class: 'tag' }, name))));
    const use = metrics && metrics.instances ? metrics.instances[i.id] : null;
    once(liveUse, [i.id, use && Math.round(use.cpu), use && Math.round(use.mem_mb)], () => replace(liveUse, use ? [
      h('span', { class: 'sc-use wide' }, h('span', { class: 'k' }, 'CPU'), h('span', { class: 'track' }, fillOf(Math.min(100, use.cpu))), h('span', { class: 'num' }, `${Math.round(use.cpu)}%`)),
      h('span', { class: 'sc-use wide' }, h('span', { class: 'k' }, 'Memory'), h('span', { class: 'track' }, fillOf(use.mem_limit_mb ? Math.min(100, (100 * use.mem_mb) / use.mem_limit_mb) : 0)), h('span', { class: 'num' }, mb(use.mem_mb))),
    ] : null));
    for (const one of oneButtons) one.disabled = false;
    oneButtons[0].disabled = UP.has(i.state);
    oneButtons[1].disabled = !UP.has(i.state);
    oneButtons[2].disabled = !UP.has(i.state);
  }

  function drawLive() {
    if (!shown.has('live')) {
      // A hidden feed keeps no stream open.
      if (liveScope) { liveScope.dispose(); liveScope = null; }
      liveKey = '';
      return;
    }
    if (!byId(focus)) focus = (instances.find((i) => LIVE.has(i.state)) || instances[0] || {}).id || 0;
    const i = byId(focus);
    const key = i ? `${i.id}:${LIVE.has(i.state)}:${i.name}` : 'none';
    if (key !== liveKey) {
      liveKey = key;
      if (liveScope) { liveScope.dispose(); liveScope = null; }
      replace(liveTitle, i ? [h('span', { class: 'num' }, `#${i.id}`), ' ', i.name] : 'nothing to show');
      liveSide.hidden = !i;
      if (!i) {
        replace(liveStage, emptyState('device', 'No Androids yet', 'Create one, then start it; its screen shows here.', linkButton('New Android', '#/androids/new', 'primary', 'plus')));
      } else if (LIVE.has(i.state)) {
        liveScope = newScope();
        replace(liveStage, liveView(i.id, liveScope, {
          send, h264, fps: 8, width: 420, keys: 'side', label: i.name, aspect: i.screen && [i.screen.width, i.screen.height],
        }));
      } else {
        replace(liveStage, h('div', { class: 'sc-live-off' }, icon('power', 'xl'), h('strong', null, `${i.name} is ${i.state}`),
          h('span', null, i.state === 'error' ? clip(i.last_error || 'The last start failed.') : 'An Android that is not running has no screen to show.'),
          UP.has(i.state) ? null : button('Start it', () => act('start', [i.id]), 'primary', 'play')));
      }
    }
    drawLiveSide();
  }
  scope.add(() => { if (liveScope) liveScope.dispose(); });

  // ------------------------------------------------------------- resources
  function resource(label, kind) {
    const value = h('span', { class: 'v' }, '–');
    const sub = h('span', { class: 'sub' });
    const chart = sparkline();
    const el = h('div', { class: `sc-res ${kind}` }, h('span', { class: 'sc-res-text' }, h('span', { class: 'k' }, label), value, sub), chart.el);
    return { el, set(text, under, values, max) {
      value.textContent = text;
      sub.textContent = under;
      sub.title = under;
      chart.set(values, max, values.length > 1 ? `${label}, recent samples` : `${label}: not enough samples yet`);
    } };
  }
  const res = { cpu: resource('CPU', 'cpu'), memory: resource('Memory', 'memory'), gpu: resource('GPU', 'gpu'), vram: resource('GPU memory', 'vram') };
  const resNote = h('p', { class: 'sc-note' });
  function drawResources() {
    if (!metrics) return;
    const host = metrics.host || {};
    const mem = host.memory || {};
    res.cpu.set(`${Math.round(host.cpu_percent || 0)}%`, `${host.cpu_count || '?'} threads`, history.cpu.values, 100);
    res.memory.set(mb(mem.used_mb), `of ${mb(mem.total_mb)}`, history.memory.values, mem.total_mb || 0);
    // Every card that reports a figure counts: the average load with the busiest named,
    // and the video memory of all of them together.
    const load = gpuLoad(metrics);
    if (!(metrics.gpus || []).length) res.gpu.set('none', 'no GPU reported', [], 100);
    else if (load.util == null) res.gpu.set('–', 'this driver reports no load', [], 100);
    else res.gpu.set(`${Math.round(load.util)}%`, load.cards === 1 ? load.busiest.name || load.busiest.vendor : `${load.cards} cards · busiest ${Math.round(load.busiest.util_percent)}%`, history.gpu.values, 100);
    res.vram.set(load.vramTotal ? mb(load.vramUsed) : '–', load.vramTotal ? `of ${mb(load.vramTotal)}${load.vramCards > 1 ? ` on ${load.vramCards} cards` : ''}` : 'no figure', history.vram.values, load.vramTotal);
    const totals = metrics.totals || {};
    const hottest = Math.max(-1, ...(metrics.gpus || []).map((g) => (g.temp_c == null ? -1 : g.temp_c)));
    resNote.textContent = [`load ${(host.load || []).join(' ')}`, ...(host.temps || []).map((t) => `${t.sensor} ${t.celsius} °C`),
      hottest >= 0 && `GPU ${hottest} °C`, totals.mem_mb != null && `Androids use ${mb(totals.mem_mb)}`].filter(Boolean).join(' · ');
  }

  // -------------------------------------------------------------- capacity
  const capRing = gauge();
  const capLimits = h('div', { class: 'sc-limits' });
  const capMix = h('div', { class: 'sc-mix' });
  const capNote = h('p', { class: 'sc-note' });
  function drawCapacity() {
    if (!capacity) return;
    const estimate = capacity.estimate || {};
    const max = estimate.max_instances;
    once(capLimits, [capacity.running, max, estimate.limits, estimate.binding, capacity.over, capacity.headroom, estimate.mix], () => {
      // In balanced mode: who would get how many of a full fleet.
      const top = Math.max(1, ...(estimate.mix || []).map((share) => share.count));
      replace(capMix, (estimate.mix || []).length ? [h('span', { class: 'sc-label' }, 'A full fleet, shared'),
        estimate.mix.map((share) => h('div', { class: 'sc-limit', title: share.slots == null ? null : `this card takes ${share.slots}` },
          h('span', { class: 'k' }, share.label), h('span', { class: 'num' }, share.count), h('span', { class: 'track' }, fillOf((100 * share.count) / top))))] : null);
      if (max == null) {
        capRing.set(capacity.running, Math.max(capacity.running, 1), 'running', `${capacity.running} running; no capacity estimate`, '');
        replace(capLimits, h('p', { class: 'muted small' }, estimate.error ? `The estimate could not be computed: ${estimate.error}` : 'This machine has no capacity estimate yet.'));
        capNote.textContent = '';
        return;
      }
      capRing.set(capacity.running, Math.max(max, capacity.running, 1), `of about ${max}`, `${capacity.running} running of about ${max}`, capacity.over ? 'warn' : undefined);
      replace(capLimits, Object.entries(estimate.limits || {}).map(([name, limit]) => h('div', { class: `sc-limit ${name === estimate.binding ? 'first' : ''}`, title: name === estimate.binding ? 'This resource runs out first' : null },
        h('span', { class: 'k' }, limitName(name)), h('span', { class: 'num' }, `${capacity.running} / ${limit.max}`), h('span', { class: 'track' }, fillOf(limit.max ? Math.min(100, (100 * capacity.running) / limit.max) : 0)))));
      capNote.textContent = capacity.over ? (capacity.reasons || []).join('; ') || 'No room for more right now.' : `Room for about ${capacity.headroom} more. Limited first by ${limitText(capacity)}.`;
    });
  }

  // ----------------------------------------------------------------- tasks
  const taskList = h('div', { class: 'sc-list' });
  function drawTasks() {
    const rows = [...tasks].sort((a, b) => Number(ACTIVE.has(b.state)) - Number(ACTIVE.has(a.state)) || (b.created || 0) - (a.created || 0)).slice(0, 8);
    once(taskList, rows.map((task) => [task.id, task.state, task.current, task.total]), () => replace(taskList, rows.length === 0
      ? h('p', { class: 'muted small' }, 'No task has run yet. Starting, installing or backing up shows here.')
      : rows.map((task) => h('div', { class: `sc-task ${task.state}`, title: task.message || null },
        h('span', { class: 'sc-task-icon' }, icon(TASK_ICON[task.state] || 'dot', task.state === 'running' ? 'spin' : '')),
        h('span', { class: 'title' }, task.title || task.kind),
        h('span', { class: 'num count' }, task.total > 0 ? `${task.current}/${task.total}` : ''),
        badge(task.state),
        ACTIVE.has(task.state) ? h('span', { class: 'track' }, fillOf(task.total > 0 ? (100 * task.current) / task.total : 15)) : null))));
  }

  // ---------------------------------------------------------------- status
  const ringOfStates = donut();
  const statusLegend = h('ul', { class: 'sc-legend' });
  function drawStatus() {
    const parts = STATUS_PARTS.map((part) => ({ ...part, value: instances.filter((i) => part.states.includes(i.state)).length }));
    once(statusLegend, parts.map((part) => part.value), () => {
      ringOfStates.set(parts, instances.length, instances.length === 1 ? 'Android' : 'Androids');
      replace(statusLegend, parts.map((part) => h('li', null, h('span', { class: `dot part-${part.kind}`, 'aria-hidden': 'true' }), h('span', { class: 'num' }, part.value), h('span', { class: 'l' }, part.label),
        h('span', { class: 'num pct' }, instances.length ? `${Math.round((100 * part.value) / instances.length)}%` : ''))));
    });
  }

  // --------------------------------------------------------------- network
  const netBody = h('div', { class: 'sc-list' });
  function drawNetwork() {
    const modes = {};
    for (const i of instances) if (i.net) modes[i.net.mode] = (modes[i.net.mode] || 0) + 1;
    const tunnelled = instances.filter((i) => i.net && (i.net.mode === 'vpn' || i.net.mode === 'gateway'));
    once(netBody, [modes, gateways && gateways.map((g) => [g.name, g.state, g.public_ip]), tunnelled.map((i) => [i.id, i.name, networkOf(i), i.state])], () => replace(netBody,
      h('div', { class: 'row' }, Object.entries(modes).map(([mode, count]) => h('span', { class: 'info-chip' }, h('span', { class: 'k' }, mode), h('span', { class: 'v num' }, count)))),
      (gateways || []).map((g) => h('div', { class: 'sc-line' }, badge(g.state === 'connected' ? 'connected' : g.state === 'stopped' ? 'stopped' : 'warn', g.state || 'unknown'),
        h('span', { class: 'title' }, `gateway ${g.name}`), h('span', { class: 'rest' }, [g.public_ip, g.profile || (g.config || {}).profile].filter(Boolean).join(' · ')))),
      tunnelled.map((i) => h('div', { class: 'sc-line' }, h('span', { class: 'num id' }, `#${i.id}`), h('a', { class: 'title', href: `#/android/${i.id}/network` }, i.name), h('span', { class: 'rest' }, networkOf(i)))),
      !tunnelled.length && !(gateways || []).length ? h('p', { class: 'muted small' }, 'No Android uses a tunnel and no shared gateway is defined.') : null));
  }

  // ----------------------------------------------------------------- alerts
  let alertFilter = 'all';
  const alertTabs = h('div', { class: 'segmented', role: 'group', 'aria-label': 'Which alerts' });
  const alertBody = h('div', { class: 'sc-list' });
  function drawAlerts() {
    const list = alerts();
    const critical = list.filter((row) => row.kind === 'fail');
    const warnings = list.filter((row) => row.kind !== 'fail');
    once(alertTabs, [alertFilter, critical.length, warnings.length], () => replace(alertTabs, [['all', `All ${list.length}`], ['warn', `Warnings ${warnings.length}`], ['fail', `Critical ${critical.length}`]].map(([key, label]) =>
      h('button', { type: 'button', 'aria-pressed': String(alertFilter === key), onclick: () => { alertFilter = key; drawAlerts(); } }, label))));
    const rows = alertFilter === 'fail' ? critical : alertFilter === 'warn' ? warnings : list;
    once(alertBody, [alertFilter, rows], () => replace(alertBody, rows.length === 0
      ? h('p', { class: 'sc-clear' }, badge('ok', 'all clear'), checks ? ' Nothing in this list needs a look.' : ' Nothing so far; the host checks are still being read.')
      : rows.slice(0, 12).map((row) => h('a', { class: `sc-alert ${row.kind}`, href: row.href }, icon(row.kind === 'fail' ? 'x' : 'alert'),
        h('span', { class: 'body' }, h('strong', null, row.title), ' ', h('span', { class: 'muted' }, row.detail))))));
  }

  // --------------------------------------------------------------- console
  const out = h('div', { class: 'sc-console-out', role: 'log', 'aria-live': 'polite', 'aria-label': 'Console output', tabindex: '0' });
  const say = (kind, words) => {
    out.append(h('div', { class: kind }, words));
    while (out.children.length > LINES_MAX) out.firstChild.remove();
    out.scrollTop = out.scrollHeight;
  };
  const past = [];
  let pastAt = 0;
  const line = h('input', { type: 'text', spellcheck: false, autocomplete: 'off', autocapitalize: 'off', placeholder: 'Type a command; "help" lists them', 'aria-label': 'Command' });
  async function run(text) {
    const words = text.trim().split(/\s+/);
    const verb = words[0].toLowerCase();
    const rest = text.trim().slice(words[0].length).trim();
    say('cmd', `> ${text.trim()}`);
    if (verb === 'help') for (const [form, what] of HELP) { say('cmd', form); say('', `    ${what}`); }
    else if (verb === 'clear') replace(out);
    else if (verb === 'status') {
      const live = instances.filter((i) => LIVE.has(i.state)).length;
      say('ok', `${instances.length} Androids, ${live} online, ${tasks.filter((task) => ACTIVE.has(task.state)).length} tasks running, ${alerts().length} alerts, ${selection.size} ticked`);
    } else if (verb === 'select') {
      if (!rest) throw new Error('select needs a selector: all, running, stopped, tag:name, 3, 1-5 or none');
      setSelection(rest === 'none' ? [] : parseSelector(rest, instances));
      say('ok', selection.size ? `ticked ${selectorOf(selected())}` : 'nothing ticked');
    } else if (verb === 'focus') {
      const one = byId(Number(rest));
      if (!one) throw new Error(`there is no Android ${rest || '(no id given)'}`);
      setFocus(one.id);
      say('ok', `the live feed shows #${one.id} ${one.name}`);
    } else if (verb === 'start' || verb === 'stop' || verb === 'restart') {
      const ids = rest ? [...parseSelector(rest, instances)] : selected();
      if (!ids.length) throw new Error(rest ? `"${rest}" matches no Android` : `${verb} needs a selector, or tick some Androids first`);
      say('', `${verb} ${selectorOf(ids)} …`);
      note('you', `${verb}: ${plural(ids.length, 'Android')} (${selectorOf(ids)})`);
      const task = await fleetAction(verb, ids, instances);
      say(task && task.state === 'done' ? 'ok' : 'err', task ? `${verb}: ${task.state}${task.failed ? `, ${task.failed} failed` : ''}` : `${verb}: not done`);
      readFleet().catch(() => {});
    } else if (verb === 'key' || verb === 'type') {
      if (!rest) throw new Error(verb === 'key' ? 'key needs a name: HOME, BACK, ENTER, ...' : 'type needs the text to type');
      const ids = targets();
      if (!ids.length) throw new Error('no running Android to send input to');
      send(verb === 'key' ? 'key' : 'text', verb === 'key' ? { name: rest.toUpperCase() } : { value: rest });
      say('ok', `sent to ${selectorOf(ids)}`);
    } else throw new Error(`unknown command "${words[0]}"; type help`);
  }
  const consoleForm = h('form', { class: 'sc-console-in', onsubmit: (event) => {
    event.preventDefault();
    const text = line.value;
    if (!text.trim()) return;
    past.push(text);
    pastAt = past.length;
    line.value = '';
    run(text).catch((error) => say('err', error.message));
  } }, h('span', { class: 'prompt', 'aria-hidden': 'true' }, '>'), line);
  line.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
    if (!past.length) return;
    event.preventDefault();
    pastAt = Math.min(past.length, Math.max(0, pastAt + (event.key === 'ArrowUp' ? -1 : 1)));
    line.value = past[pastAt] || '';
  });
  say('', 'Super Control console. Type help for the commands.');

  // ---------------------------------------------------------------- panels
  const boxes = {
    units: panelBox('units', 'Active units', [chips, h('span', { class: 'search' }, icon('search'), search), sizeToggle],
      h('div', { class: 'sc-units-wrap' }, grid),
      h('div', { class: 'sc-selbar' }, selInfo, h('span', { class: 'sc-sel-actions' }, lifecycle,
        button('Tick shown', () => setSelection(instances.filter(matches).map((i) => i.id)), 'small ghost'), button('Clear', () => setSelection([]), 'small ghost')))),
    live: panelBox('live', 'Live feed', [liveTitle, h('span', { class: 'grow' }), linkButton('Control wall', '#/control/running', 'small ghost', 'control')],
      h('div', { class: 'sc-live-body' }, liveStage, liveSide)),
    resources: panelBox('resources', 'System resources', null, h('div', { class: 'sc-res-grid' }, Object.values(res).map((one) => one.el)), resNote),
    capacity: panelBox('capacity', 'Capacity', h('a', { class: 'small', href: '#/system' }, 'How it is counted'), h('div', { class: 'sc-cap' }, capRing.el, capLimits), capMix, capNote),
    tasks: panelBox('tasks', 'Task queue', button('All tasks', () => toggleTasks(true), 'small ghost', 'tasks'), taskList),
    status: panelBox('status', 'Fleet status', null, h('div', { class: 'sc-status-body' }, ringOfStates.el, statusLegend)),
    network: panelBox('network', 'Network', h('a', { class: 'small', href: '#/network' }, 'Network page'), netBody),
    timeline: panelBox('timeline', 'Timeline', button('Clear', () => replace(timeline), 'small ghost'), timeline),
    alerts: panelBox('alerts', 'Diagnostics and alerts', alertTabs, alertBody),
    console: panelBox('console', 'Command console', null, out, consoleForm),
  };
  const rows = [0, 1, 2].map((index) => h('div', { class: `sc-row sc-row-${index}` }));
  const nothing = h('p', { class: 'sc-nothing muted', hidden: true }, 'Every panel is switched off. Switch some on above.');
  const toggles = new Map();
  function place() {
    rows.forEach((rowEl, index) => {
      const mine = PANELS.filter((panel) => panel.row === index && shown.has(panel.key)).map((panel) => boxes[panel.key]);
      rowEl.hidden = !mine.length;
      once(rowEl, PANELS.filter((panel) => panel.row === index).map((panel) => shown.has(panel.key)), () => replace(rowEl, mine));
    });
    nothing.hidden = shown.size > 0;
    for (const [key, el] of toggles) el.setAttribute('aria-pressed', String(shown.has(key)));
    drawLive();
  }
  const bar = h('div', { class: 'sc-toggles', role: 'group', 'aria-label': 'Panels' }, h('span', { class: 'sc-label' }, 'Panels'),
    PANELS.map((panel) => {
      const el = h('button', { type: 'button', class: 'sc-toggle', 'aria-pressed': 'true', title: `Show or hide ${panel.label}`, onclick: () => {
        shown[shown.has(panel.key) ? 'delete' : 'add'](panel.key);
        setPref('super-panels', [...shown]);
        place();
      } }, icon(panel.icon), h('span', null, panel.label));
      toggles.set(panel.key, el);
      return el;
    }));

  // --------------------------------------------------------------- reading
  let lastStates = null;
  const taskStates = new Map();
  let firstTasks = true;
  async function readFleet() {
    const [list, m, t] = await Promise.all([loadInstances(), get('/api/system/metrics').catch(() => null), get('/api/tasks', { limit: 12 }).catch(() => null)]);
    const states = new Map(list.map((i) => [i.id, i.state]));
    if (lastStates) {
      for (const i of list) {
        const was = lastStates.get(i.id);
        if (was === undefined) note('ok', `#${i.id} ${i.name}: created`);
        else if (was !== i.state) note(i.state === 'error' ? 'fail' : i.state === 'booted' ? 'ok' : '', `#${i.id} ${i.name}: ${was} → ${i.state}`);
      }
      for (const [id] of lastStates) if (!states.has(id)) note('warn', `Android ${id}: removed`);
    }
    lastStates = states;
    if (t) {
      // The first answer fills the timeline with what already happened, at its own time.
      const order = [...(t.tasks || [])].sort((a, b) => (a.finished || a.created || 0) - (b.finished || b.created || 0));
      for (const task of order) {
        const was = taskStates.get(task.id);
        if (was === task.state) continue;
        taskStates.set(task.id, task.state);
        const stamp = firstTasks && (task.finished || task.created) ? new Date((task.finished || task.created) * 1000) : new Date();
        const name = task.title || task.kind;
        if (ACTIVE.has(task.state)) { if (!was) note('', `task started: ${name}`, stamp); } else note(task.state === 'done' ? 'ok' : task.state === 'failed' ? 'fail' : 'warn', `task ${task.state}: ${name}`, stamp);
      }
      firstTasks = false;
      tasks = t.tasks || [];
    }
    if (m) {
      metrics = m;
      const host = m.host || {};
      const load = gpuLoad(m);
      history.cpu.push(host.cpu_percent || 0);
      history.memory.push((host.memory || {}).used_mb || 0);
      if (load.util != null) history.gpu.push(load.util);
      if (load.vramTotal) history.vram.push(load.vramUsed);
    }
    instances = list;
    rememberInstances(list);
    for (const id of [...selection]) if (!states.has(id)) selection.delete(id);
    drawAll();
  }
  async function readSlow() {
    const [cap, net] = await Promise.all([get('/api/system/capacity').catch(() => null), get('/api/net/gateways').catch(() => null)]);
    if (cap) capacity = cap;
    if (net) {
      const before = gateways;
      gateways = net.gateways || [];
      if (before) {
        for (const g of gateways) {
          const was = (before.find((old) => old.name === g.name) || {}).state;
          if (was && was !== g.state) note(g.state === 'connected' ? 'ok' : 'warn', `gateway ${g.name}: ${was} → ${g.state}`);
        }
      }
    }
    drawAll();
  }
  async function readChecks() {
    checks = await get('/api/system/doctor').catch(() => checks);
    drawAll();
  }
  function drawAll() {
    drawTop();
    drawUnits();
    drawLive();
    drawResources();
    drawCapacity();
    drawTasks();
    drawStatus();
    drawNetwork();
    drawAlerts();
  }

  const asked = new Set(approvals.pending.map((item) => item.id));
  scope.add(onApprovals((state) => {
    for (const item of state.pending) if (!asked.has(item.id)) { asked.add(item.id); note('warn', `approval asked by ${item.caller}: ${item.summary}`); }
    drawTop();
    drawAlerts();
  }));

  root.append(h('div', { class: 'sc' },
    pageHead('Super Control', 'The operations centre: every Android, the host and everything in flight on one screen, with the controls to act on it.', verdict, clock),
    h('div', { class: 'sc-kpis', role: 'group', 'aria-label': 'The farm at a glance' }, Object.values(kpis).map((one) => one.el)),
    bar, nothing, rows));
  place();
  await scope.every(3000, readFleet);
  note('', 'Super Control opened');
  scope.every(10000, readSlow);
  scope.every(60000, readChecks);
}
