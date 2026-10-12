// Pieces several pages share: headings, badges, meters, key/value lists, and the two
// farm-wide rules of the panel (how capacity is worded, how a start over capacity is
// confirmed).

import { get, post } from './api.js';
import { confirmDialog } from './components/dialog.js';
import { finished } from './components/tasks.js';
import { attempt } from './components/toast.js';
import { h, mb, selectorOf, text } from './dom.js';

export function pageHead(title, sub, ...actions) {
  return h('div', { class: 'page-head' }, h('h1', null, title), actions, sub && h('p', { class: 'sub' }, sub));
}

export function card(title, ...body) {
  return h('section', { class: 'card' }, title && h('h2', null, title), body);
}

export function empty(title, hint, ...actions) {
  return h('div', { class: 'empty' }, h('strong', null, title), hint, actions.length ? h('div', { class: 'row' }, actions) : null);
}

export const button = (label, onclick, kind = '') => h('button', { type: 'button', class: `btn ${kind}`, onclick }, label);

const STATE_KIND = {
  booted: 'ok', running: 'info', starting: 'info', error: 'fail', connected: 'ok', direct: 'ok', isolated: 'ok',
  ok: 'ok', warn: 'warn', fail: 'fail', done: 'ok', failed: 'fail', cancelled: 'warn', needs_attention: 'warn',
  reconnecting: 'warn', connecting: 'info', auth_failed: 'fail',
};
export const badge = (state, label) => h('span', { class: `badge ${STATE_KIND[state] || ''}` }, label || state || 'unknown');

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

export function field(label, control, help) {
  if (!control.id) control.id = `c${Math.random().toString(36).slice(2, 9)}`;
  const check = control.type === 'checkbox';
  const lab = h('label', { for: control.id }, label);
  return h('div', { class: `field ${check ? 'check' : ''}` }, check ? [control, lab] : [lab, control],
    help && h('div', { class: 'help' }, help));
}

export const option = (value, label = value, extra = {}) => h('option', { value, ...extra }, label);

// ----------------------------------------------------------------- capacity
const LIMIT = { ram: 'memory (RAM)', cpu: 'CPU', vram: 'video memory' };

// One wording everywhere: "7 running of about 13 this machine can run".
export function capacityText(capacity) {
  const estimate = capacity.estimate || {};
  return `${capacity.running} running of about ${estimate.max_instances ?? '?'} this machine can run`;
}

export const limitText = (capacity) => LIMIT[(capacity.estimate || {}).binding] || (capacity.estimate || {}).binding || 'unknown';

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
      ram.max_now != null && ram.max_now < max ? `With what else is running on this machine now: about ${ram.max_now}. ` : '',
      h('span', { class: 'muted' }, `Assumes ${estimate.mode || 'software'} rendering and a ${estimate.workload || 'video'} workload; ${mb(capacity.memory_available_mb)} RAM is available now.`)),
    capacity.over && h('div', { class: 'banner warn', role: 'status' },
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

// start | stop | restart | recreate | delete on a set of ids. Returns the finished task
// (null when the operator backed out or the request was refused).
export async function fleetAction(action, ids) {
  ids = [...ids];
  if (!ids.length) return null;
  const body = { action, selector: selectorOf(ids) };
  if (action === 'delete') {
    const yes = await confirmDialog({
      title: `Delete ${ids.length} Android(s)?`,
      message: `Android ${selectorOf(ids)}: containers, settings and all data are removed. With backup.auto_before_risky on (the default), a backup of each is taken first.`,
      confirm: 'Delete', danger: true,
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
  return h('form', { class: 'row', onsubmit: (event) => { event.preventDefault(); apply(); } },
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
