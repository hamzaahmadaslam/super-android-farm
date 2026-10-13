// Pieces several pages share: headings, buttons, badges, meters, key/value lists, empty
// states, and the two farm-wide rules of the panel (how capacity is worded, how a start
// over capacity is confirmed).

import { get, post } from './api.js';
import { confirmDialog } from './components/dialog.js';
import { help } from './components/help.js';
import { finished } from './components/tasks.js';
import { attempt } from './components/toast.js';
import { h, mb, selectorOf, single, text } from './dom.js';
import { icon } from './icons.js';

// Every page starts the same way: what this is, one line on what it is for, and the
// actions that matter most on the right.
export function pageHead(title, sub, ...actions) {
  const list = actions.flat(Infinity).filter(Boolean);
  return h('header', { class: 'page-head' },
    h('div', { class: 'page-title' }, h('h1', null, title), sub && h('p', { class: 'sub' }, sub)),
    list.length ? h('div', { class: 'page-actions' }, list) : null);
}

export function card(title, ...body) {
  return h('section', { class: 'card' }, title && h('h2', null, title), body);
}

// A card whose heading carries an icon and, optionally, controls on the right.
export function panel(title, { icon: name, actions, topic } = {}, ...body) {
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('h2', null, name && icon(name), title, topic && help(topic)), actions),
    body);
}

// An empty state says what belongs here and offers the first step.
export function emptyState(name, title, hint, ...actions) {
  const list = actions.flat(Infinity).filter(Boolean);
  return h('div', { class: 'empty' },
    h('span', { class: 'empty-icon' }, icon(name)),
    h('strong', null, title), hint && h('span', null, hint),
    list.length ? h('div', { class: 'row' }, list) : null);
}

export const empty = (title, hint, ...actions) => emptyState('inbox', title, hint, ...actions);

export function button(label, onclick, kind = '', iconName = '') {
  return h('button', { type: 'button', class: `btn ${kind}`, onclick }, iconName && icon(iconName), label);
}

// A button that shows only an icon; `label` is what assistive technology and the tooltip say.
export function iconButton(iconName, label, onclick, kind = '') {
  return h('button', { type: 'button', class: `btn icon-only ${kind}`, 'aria-label': label, title: label, onclick }, icon(iconName));
}

export const linkButton = (label, href, kind = '', iconName = '') => h('a', { class: `btn ${kind}`, href }, iconName && icon(iconName), label);

// ------------------------------------------------------------------ status
// A state is never colour alone: every badge is an icon, a word and a colour.
const STATE = {
  booted: ['ok', 'check'], running: ['info', 'play'], starting: ['info', 'spinner'], error: ['fail', 'alert'],
  stopped: ['', 'stop'], created: ['', 'stop'], missing: ['', 'dot'],
  connected: ['ok', 'check'], direct: ['ok', 'check'], isolated: ['ok', 'check'],
  ok: ['ok', 'check'], warn: ['warn', 'alert'], fail: ['fail', 'x'], skip: ['', 'dot'],
  done: ['ok', 'check'], failed: ['fail', 'x'], cancelled: ['warn', 'x'], queued: ['', 'clock'],
  needs_attention: ['warn', 'alert'], reconnecting: ['warn', 'spinner'], connecting: ['info', 'spinner'], auth_failed: ['fail', 'x'],
  unverified: ['warn', 'help'], confirmed: ['ok', 'check'], waiting_approval: ['warn', 'clock'], idle: ['', 'dot'],
  approved: ['ok', 'check'], denied: ['fail', 'x'], expired: ['warn', 'clock'], declined: ['fail', 'x'],
  info: ['info', 'info'],
};

export function badge(state, label) {
  const [kind, name] = STATE[state] || ['', 'dot'];
  const shown = label || String(state || 'unknown').replaceAll('_', ' ');
  return h('span', { class: `badge ${kind}` }, icon(name, name === 'spinner' ? 'spin' : ''), shown);
}

// What each state of an Android means, for the tooltip and for the legend.
export const ANDROID_STATES = {
  booted: 'Android finished starting and answers',
  running: 'the container is up; the boot is not confirmed yet',
  starting: 'being started now',
  stopped: 'stopped; its data is kept',
  created: 'containers exist but were never started',
  missing: 'no containers yet; they are made at the first start',
  error: 'the last start failed',
};
export const UP = new Set(['running', 'booted', 'starting']);

export function stateBadge(instance) {
  const node = badge(instance.state);
  node.title = `${instance.state}: ${ANDROID_STATES[instance.state] || 'unknown state'}`;
  return node;
}

// How an Android draws and how it reaches the network, as the API reports them.
export const renderingOf = (i) => (i.gpu ? i.gpu.mode || `${i.gpu.requested} (at next start)` : '');
export const networkOf = (i) => (i.net ? [i.net.mode, i.net.profile || i.net.gateway || i.net.proxy].filter(Boolean).join(' · ') : '');

export function meter(label, value, max, caption) {
  const ratio = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
  const fill = h('span', { class: 'fill' });
  fill.style.width = `${(ratio * 100).toFixed(1)}%`;
  return h('div', { class: `meter ${ratio >= 0.95 ? 'fail' : ratio >= 0.8 ? 'warn' : ''}` },
    h('div', { class: 'meter-head' }, h('span', null, label), h('span', { class: 'muted' }, caption)),
    h('div', {
      class: 'track', role: 'meter', 'aria-label': label,
      'aria-valuemin': '0', 'aria-valuemax': String(max), 'aria-valuenow': String(value), 'aria-valuetext': caption,
    }, fill));
}

// An object as a definition list. `labels` picks and names the rows: { key: 'Label' }.
export function kv(object, labels) {
  const rows = labels ? Object.entries(labels).map(([key, label]) => [label, object ? object[key] : undefined])
    : Object.entries(object || {});
  return h('dl', { class: 'kv' }, rows.filter(([, value]) => value != null && value !== '')
    .map(([label, value]) => [h('dt', null, label), h('dd', null, value instanceof Node ? value : text(value))]));
}

// The same, one row per fact with the value on the right: for narrow side columns.
export function facts(object) {
  return h('dl', { class: 'facts' }, Object.entries(object || {}).filter(([, value]) => value != null && value !== '' && value !== false)
    .map(([label, value]) => h('div', { class: 'fact' }, h('dt', null, label), h('dd', null, value instanceof Node || Array.isArray(value) ? value : text(value)))));
}

// A label, its control and optional help. `path` names the field the way the API does
// ("resources.memory_mb"), so a validation message can be shown right under it;
// `topic` adds a "?" with the farm's own description of the option.
export function field(label, control, helpText, { path, topic } = {}) {
  if (!control.id) control.id = `c${Math.random().toString(36).slice(2, 9)}`;
  const check = control.type === 'checkbox';
  const lab = h('label', { for: control.id }, label);
  const named = topic ? h('div', { class: 'label-row' }, lab, help(topic)) : lab;
  return h('div', { class: `field ${check ? 'check' : ''}`, dataset: path ? { path } : null }, check ? [control, named] : [named, control],
    helpText && h('div', { class: 'help' }, helpText));
}

export const option = (value, label = value, extra = {}) => h('option', { value, ...extra }, label);

// A line of loading placeholders, shown until the first answer arrives.
export function skeleton(lines = 3) {
  return h('div', { 'aria-hidden': 'true' }, Array.from({ length: lines }, (_, i) => h('span', { class: `skeleton line ${['w-80', 'w-60', 'w-40'][i % 3]}` })));
}

// ----------------------------------------------------------------- capacity
const LIMIT = { ram: 'memory (RAM)', cpu: 'CPU', vram: 'video memory', cards: 'GPU slots' };

// One wording everywhere: "7 running of about 13 this machine can run".
export function capacityText(capacity) {
  const estimate = capacity.estimate || {};
  return `${capacity.running} running of about ${estimate.max_instances ?? '?'} this machine can run`;
}

export const limitText = (capacity) => LIMIT[(capacity.estimate || {}).binding] || (capacity.estimate || {}).binding || 'unknown';
export const limitName = (name) => LIMIT[name] || name;

// What the estimate assumed. The rendering mode and the workload are shown exactly as
// the API names them, and the reason for the mode in the API's own words.
export function capacityAssumptions(capacity) {
  const estimate = capacity.estimate || {};
  return h('div', { class: 'stack tight' },
    h('div', { class: 'limits' },
      estimate.mode != null && h('span', { class: 'info-chip' }, icon('layers'), h('span', { class: 'k' }, 'rendering mode'), h('code', { class: 'v' }, estimate.mode)),
      estimate.workload != null && h('span', { class: 'info-chip' }, icon('gauge'), h('span', { class: 'k' }, 'workload'), h('code', { class: 'v' }, estimate.workload)),
      capacity.memory_available_mb != null && h('span', { class: 'info-chip' }, icon('memory'), h('span', { class: 'k' }, 'RAM available now'), h('span', { class: 'v num' }, mb(capacity.memory_available_mb)))),
    estimate.mode_reason && h('p', { class: 'chip-reason' }, estimate.mode_reason));
}

// `advice` is how many of the estimate's suggestions to list (most effective first).
export function capacityCard(capacity, { advice = 0 } = {}) {
  const estimate = capacity.estimate || {};
  const max = estimate.max_instances || 0;
  const limits = estimate.limits || {};
  const ram = limits.ram || {};
  const reasons = capacity.reasons || [];
  const lines = (capacity.advice || estimate.advice || []).slice(0, advice);
  return h('div', { class: 'stack' },
    meter(capacityText(capacity), capacity.running, Math.max(max, capacity.running, 1),
      capacity.over ? 'no room for more right now' : `room for about ${capacity.headroom} more`),
    h('p', null, 'Limited first by ', h('strong', null, limitText(capacity)), '. By resource: ',
      Object.entries(limits).map(([name, limit]) => `${LIMIT[name] || name} ${limit.max}`).join(' · '), '. ',
      ram.max_now != null && ram.max_now < max ? `With what else is running on this machine now: about ${ram.max_now}.` : ''),
    h('div', null, h('h3', null, 'The estimate assumes'), capacityAssumptions(capacity)),
    capacity.over && h('div', { class: 'banner warn', role: 'status' }, icon('alert'),
      h('div', { class: 'grow' }, h('strong', null, 'At the limit of the estimate. '),
        reasons.length ? `${reasons.join('; ')}. ` : '',
        capacity.enforced ? 'Starting more asks for confirmation ("Run anyway").'
          : 'This is advice, not a limit: you can start more, but expect slower frames and, when memory runs out, apps being stopped.')),
    lines.length > 0 && h('div', null,
      h('h3', null, 'How to fit more'),
      h('ul', null, lines.map((line) => h('li', null, line)))));
}

export const loadCapacity = () => get('/api/system/capacity');

// The estimate is advice. Going over it asks once, then runs with force: true.
export function confirmOverCapacity(capacity, wanted, message) {
  return confirmDialog({
    title: 'Over the capacity estimate',
    icon: 'gauge',
    message: message || `Starting ${wanted} more would go over the estimate for this machine.`,
    details: capacity && capacity.estimate ? [
      h('p', null, capacityText(capacity), `; limited by ${limitText(capacity)}.`,
        (capacity.reasons || []).length ? ` ${capacity.reasons.join('; ')}.` : ''),
      h('p', { class: 'muted' }, 'You can run more anyway. Expect slower frames; if memory runs out the kernel stops apps or whole Androids.'),
    ] : null,
    confirm: 'Run anyway',
  });
}

// Is the H.264 live view offered? /api/system says so directly when it reports the stream
// capability; otherwise the part merely existing is taken as "worth one try" (the view
// falls back to MJPEG by itself, once, when the device stream does not start).
export function h264Offered(system) {
  if (system && system.h264) return Boolean(system.h264.enabled);
  return Boolean(system && system.components && system.components.h264 && system.components.h264.available);
}

// ------------------------------------------------------------ fleet actions
export const loadInstances = async () => (await get('/api/instances')).instances;

// "Android 4, 7, 9" for a sentence; ranges stay ranges ("1-3, 7").
export const androidsText = (ids) => `Android ${selectorOf(ids).replaceAll(',', ', ')}`;
export const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

// The list a confirmation shows: exactly which Androids, by id, name and state.
export function affectedList(ids, instances = []) {
  const byId = new Map(instances.map((i) => [i.id, i]));
  return h('ul', { class: 'affected', 'aria-label': 'Affected Androids' }, [...ids].map(Number).sort((a, b) => a - b).map((id) => {
    const instance = byId.get(id);
    return h('li', null, h('span', { class: 'id' }, `#${id}`), h('span', { class: 'grow' }, instance ? instance.name : `Android ${id}`), instance && stateBadge(instance));
  }));
}

// start | stop | restart | recreate | delete on a set of ids. Returns the finished task
// (null when the operator backed out or the request was refused). `instances` (the rows
// the page already has) lets a confirmation name the Androids without another request.
export function fleetAction(action, ids, instances) {
  ids = [...ids];
  if (!ids.length) return Promise.resolve(null);
  // The same action on the same Androids asked twice at once (a double press, the card
  // and the bulk bar together) is one confirmation and one request.
  return single(`fleet:${action}:${selectorOf(ids)}`, () => runFleetAction(action, ids, instances));
}

async function runFleetAction(action, ids, instances) {
  const body = { action, selector: selectorOf(ids) };
  if (action === 'delete') {
    const rows = instances || await loadInstances().catch(() => []);
    const yes = await confirmDialog({
      title: `Delete ${plural(ids.length, 'Android')} and ${ids.length === 1 ? 'its' : 'their'} data?`,
      message: `${androidsText(ids)}: containers, settings and all data are removed. With backup.auto_before_risky on (the default), a backup of each is taken first.`,
      details: affectedList(ids, rows),
      confirm: `Delete ${plural(ids.length, 'Android')}`, danger: true,
    });
    if (!yes) return null;
  }
  if (action === 'start' || action === 'restart' || action === 'recreate') {
    // Ask before the call rather than after a refusal, when we already know the answer.
    // A start adds Androids; a restart adds none, but is refused all the same while the
    // farm enforces the estimate and is over it.
    const capacity = await loadCapacity().catch(() => null);
    // Without an estimate (the probe failed) there is no headroom figure to compare with.
    const known = capacity && capacity.estimate && capacity.estimate.max_instances != null;
    const over = capacity && (action === 'start'
      ? capacity.over || (known && ids.length > capacity.headroom)
      : capacity.over && capacity.enforced);
    if (over) {
      if (!await confirmOverCapacity(capacity, ids.length,
        action === 'start' ? null : `The farm is over its capacity estimate, and ${action} would be refused without confirmation.`)) return null;
      body.force = true;
    }
  }
  const data = await attempt(() => post('/api/instances/bulk', body));
  if (!data) return null;
  return finished(data.task);
}

// Run `work(id)` for each id in turn and report once. Used where the API has no bulk
// form (per-Android settings): one failure never stops the rest.
export async function eachInstance(ids, work) {
  const failures = [];
  for (const id of ids) {
    try { await work(id); } catch (error) { failures.push(`Android ${id}: ${error.message}`); }
  }
  return failures;
}

// ------------------------------------------------------------ device settings
// Live settings of running Androids (POST /api/device/...). The same small form serves
// one Android (its Overview tab) and a whole selection (the Control page).
const DEVICE_ACTIONS = [
  ['wake', 'Wake the screen', []],
  ['reboot', 'Restart Android (soft)', []],
  ['open-url', 'Open a link', [['url', 'Link to open', 'text']]],
  ['clipboard', 'Set the clipboard', [['text', 'Text', 'text']]],
  ['locale', 'Language', [['locale', 'Locale, for example en-US', 'text']]],
  ['timezone', 'Time zone', [['timezone', 'For example Europe/Berlin', 'text']]],
  ['geo', 'Mock location', [['lat', 'Latitude', 'number'], ['lon', 'Longitude', 'number']]],
  ['proxy', 'HTTP proxy', [['proxy', 'host:port (empty switches it off)', 'text']]],
  ['dark-mode', 'Dark mode', [['enabled', 'On', 'checkbox']]],
  ['animations', 'Animations', [['enabled', 'On', 'checkbox']]],
  ['font-scale', 'Font scale', [['scale', '0.5 to 2.0', 'number']]],
];

export function deviceActions(selector) {
  const inputs = h('span', { class: 'row' });
  const pick = h('select', { 'aria-label': 'Device setting' }, DEVICE_ACTIONS.map(([id, label]) => option(id, label)));
  let controls = [];
  const draw = () => {
    const [, , spec] = DEVICE_ACTIONS.find(([id]) => id === pick.value);
    controls = spec.map(([name, label, type]) => [name, h('input', { type, 'aria-label': label, placeholder: label, step: type === 'number' ? 'any' : null }), type]);
    inputs.replaceChildren(...controls.flatMap(([, input, type]) => (type === 'checkbox' ? [h('label', null, input, ' On')] : [input])));
  };
  pick.addEventListener('change', draw);
  draw();
  const apply = () => attempt(async () => {
    const target = selector();
    if (!target) throw new Error('Select at least one Android first.');
    const body = { selector: target };
    for (const [name, input, type] of controls) {
      if (type === 'checkbox') body[name] = input.checked;
      else if (type === 'number') { if (input.value !== '') body[name] = Number(input.value); }
      else body[name] = input.value;
    }
    return post(`/api/device/${pick.value}`, body);
  });
  return h('form', { class: 'row', onsubmit: (event) => { event.preventDefault(); return apply(); } },
    pick, inputs, h('button', { type: 'submit', class: 'btn' }, 'Apply'));
}

// "pm list packages -3" typed by a person -> argv, honouring simple quotes.
export function splitCommand(line) {
  const out = [];
  const pattern = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let match;
  while ((match = pattern.exec(line))) out.push(match[1] ?? match[2] ?? match[3]);
  return out;
}
