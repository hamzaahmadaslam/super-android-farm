// Dashboard: mission control. Is the farm healthy, how full is the machine, what is the
// fleet doing, what is happening now, and what needs a look.

import { get } from '../api.js';
import { badge, button, capacityText, emptyState, limitName, limitText, linkButton, loadInstances, pageHead, panel, skeleton, stateBadge } from '../common.js';
import { gauge, liveMeter, segmentedBar, series } from '../components/charts.js';
import { toggle as toggleTasks } from '../components/tasks.js';
import { h, mb, replace } from '../dom.js';
import { icon } from '../icons.js';
import { setPref } from '../prefs.js';
import { rememberInstances } from '../state.js';
import { guideCard, guideHidden, loadGuideState } from './guide.js';

// Samples outlive the page: coming back to the dashboard continues the same lines.
const history = { cpu: series(), memory: series(), gpu: new Map() };

// The fleet in five words. `states` are the API's states that count into each.
const GROUPS = [
  { key: 'booted', label: 'booted', kind: 'ok', icon: 'check', states: ['booted'] },
  { key: 'running', label: 'running', kind: 'info', icon: 'play', states: ['running'] },
  { key: 'starting', label: 'starting', kind: 'info', icon: 'spinner', states: ['starting'] },
  { key: 'stopped', label: 'stopped', kind: 'idle', icon: 'stop', states: ['stopped', 'created', 'missing'] },
  { key: 'error', label: 'error', kind: 'fail', icon: 'alert', states: ['error'] },
];

const clip = (words, most = 160) => (words.length > most ? `${words.slice(0, most)}…` : words);

export async function render(root, _params, scope) {
  const guide = h('div', { class: 'span-12', hidden: true });
  const ring = gauge();
  const capacityText_ = h('div', { class: 'capacity-text' }, skeleton(3));
  const capacity = panel('Capacity', { icon: 'gauge', topic: 'capacity', actions: h('a', { class: 'small', href: '#/system' }, 'How it is counted') },
    h('div', { class: 'capacity' }, ring.el, capacityText_));
  const fleetBody = h('div', { class: 'stack' }, skeleton(4));
  const fleet = panel('Fleet', { icon: 'devices', actions: h('a', { class: 'small', href: '#/androids' }, 'All Androids') }, fleetBody);
  const hostBody = h('div', { class: 'stack' }, skeleton(4));
  const host = panel('Host', { icon: 'system', actions: h('a', { class: 'small', href: '#/system' }, 'System') }, hostBody);
  const tasksBody = h('div', null, skeleton(2));
  const tasks = panel('Active tasks', { icon: 'tasks', actions: button('Open the task list', () => toggleTasks(true), 'small') }, tasksBody);
  const alertsBody = h('div', null, skeleton(3));
  const alerts = panel('Alerts', { icon: 'alert', actions: h('a', { class: 'small', href: '#/system/checks' }, 'All host checks') }, alertsBody);
  capacity.classList.add('span-7', 'wide-md', 'even');
  fleet.classList.add('span-5', 'wide-md', 'even');
  host.classList.add('span-7', 'wide-md');
  const side = h('div', { class: 'stack span-5 wide-md' }, tasks, alerts);

  root.append(
    pageHead('Dashboard', 'The whole farm at a glance: capacity, fleet state, host load, running tasks and anything that needs a look.',
      linkButton('Control wall', '#/control/running', '', 'control'),
      linkButton('New Android', '#/androids/new', 'primary', 'plus')),
    h('div', { class: 'dash' }, guide, capacity, fleet, host, side));

  let instances = [];
  let cap = null;
  let checks = null;
  let guideState = null;

  // ------------------------------------------------------------- guide
  const drawGuide = () => {
    const node = guideCard(guideState, instances, drawGuide);
    guide.hidden = !node;
    replace(guide, node);
  };
  const readGuide = async () => {
    if (guideHidden()) return;
    guideState = await loadGuideState();
    if (guideState.doctor) checks = guideState.doctor;
    drawGuide();
    drawAlerts();
  };

  // ------------------------------------------------------------- fleet
  function drawCapacity() {
    const estimate = cap.estimate || {};
    const max = estimate.max_instances;
    if (max == null) {
      ring.set(cap.running, Math.max(cap.running, 1), 'running', `${cap.running} running; no capacity estimate`, '');
      replace(capacityText_, h('p', { class: 'lead' }, `${cap.running} running.`),
        h('p', { class: 'muted' }, estimate.error ? `The estimate could not be computed: ${estimate.error}` : 'This machine has no capacity estimate yet.'));
      return;
    }
    ring.set(cap.running, Math.max(max, cap.running, 1), `of about ${max}`, capacityText(cap), cap.over ? 'warn' : undefined);
    const limits = Object.entries(estimate.limits || {});
    const advice = (cap.advice || estimate.advice || [])[0];
    replace(capacityText_,
      h('p', { class: 'lead' }, h('span', { class: 'num' }, cap.running), ' running of about ', h('span', { class: 'num' }, max), ' this machine can run'),
      h('p', null, cap.over ? [badge('warn', 'at the limit'), ' ', (cap.reasons || []).join('; ') || 'No room for more right now.']
        : ['Room for about ', h('strong', { class: 'num' }, cap.headroom), ' more. Limited first by ', h('strong', null, limitText(cap)), '.']),
      h('div', { class: 'limits' },
        limits.map(([name, limit]) => h('span', { class: 'info-chip', title: name === estimate.binding ? 'This resource runs out first' : null },
          h('span', { class: 'k' }, limitName(name)), h('span', { class: 'v num' }, limit.max), name === estimate.binding && icon('alert'))),
        estimate.mode != null && h('span', { class: 'info-chip', title: estimate.mode_reason || null }, icon('layers'), h('span', { class: 'k' }, 'rendering mode'), h('code', { class: 'v' }, estimate.mode))),
      advice && h('p', { class: 'small muted', title: advice }, clip(advice, 210), ' ', h('a', { href: '#/system' }, 'All advice')));
  }

  function drawFleet() {
    if (instances.length === 0) {
      replace(fleetBody, emptyState('device', 'No Androids yet', 'An Android is one container with its own data, network and identity. Create one, then start it.',
        linkButton('New Android', '#/androids/new', '', 'plus')));
      return;
    }
    const count = (group) => instances.filter((i) => group.states.includes(i.state)).length;
    const parts = GROUPS.map((group) => ({ ...group, value: count(group) }));
    replace(fleetBody,
      h('div', { class: 'fleet-total' }, h('span', { class: 'n' }, instances.length), h('span', { class: 'muted' }, instances.length === 1 ? 'Android defined' : 'Androids defined')),
      segmentedBar(parts, 'Fleet by state'),
      h('div', { class: 'legend' }, parts.map((part) => h('a', {
        href: '#/androids', title: `Show these on the Androids page (${part.states.join(', ')})`,
        // The Androids page opens with this state already filtered.
        onclick: () => setPref('androids-filters', part.states.length === 1 ? { state: part.states[0] } : null),
      }, h('span', { class: 'n' }, part.value), h('span', { class: `l ${part.kind === 'idle' ? '' : part.kind}` }, icon(part.icon), part.label)))));
  }

  function drawTasks(list) {
    replace(tasksBody, list.length === 0 ? h('p', { class: 'muted' }, 'Nothing is running in the background.')
      : list.slice(0, 6).map((task) => h('div', { class: 'task-line' },
        badge(task.state), h('span', { class: 'title' }, task.title || task.kind),
        task.total > 0 ? h('span', { class: 'num small muted nowrap' }, `${task.current} / ${task.total}`) : null,
        h('span', { class: 'grow small muted' }, task.message))),
    list.length > 6 && h('p', { class: 'small muted' }, `and ${list.length - 6} more`));
  }

  function drawAlerts() {
    const rows = [];
    if (cap && cap.over) {
      rows.push([badge('warn', 'capacity'), [h('strong', null, 'At the limit of the capacity estimate. '), (cap.reasons || []).join('; ')], '#/system']);
    }
    for (const i of instances) {
      if (i.state === 'error' || i.config_error) {
        rows.push([stateBadge(i), [h('a', { href: `#/android/${i.id}` }, i.name), ' ', h('span', { class: 'muted' }, clip(i.config_error || i.last_error || 'the last start failed'))]]);
      }
    }
    for (const check of checks ? checks.checks : []) {
      if (check.status === 'fail' || check.status === 'warn') {
        rows.push([badge(check.status), [h('strong', null, check.title), ' ', h('span', { class: 'muted' }, clip(check.detail))]]);
      }
    }
    rows.sort((a, b) => Number(b[0].classList.contains('fail')) - Number(a[0].classList.contains('fail')));
    if (!cap && !checks) return;
    replace(alertsBody, rows.length === 0 ? h('p', null, badge('ok', 'all good'), ' Nothing needs attention: every host check passes and no Android is in an error state.')
      : [rows.slice(0, 7).map(([mark, body]) => h('div', { class: 'alert' }, mark, h('div', { class: 'body' }, body))),
        rows.length > 7 && h('p', { class: 'small muted' }, `and ${rows.length - 7} more on the System page`)]);
  }

  const readFleet = async () => {
    const [rows, capacityNow, active] = await Promise.all([
      loadInstances(), get('/api/system/capacity'), get('/api/tasks', { active: true }),
    ]);
    instances = rows;
    cap = capacityNow;
    rememberInstances(rows);
    drawCapacity();
    drawFleet();
    drawTasks(active.tasks || []);
    drawAlerts();
    if (guideState) drawGuide();
  };

  // -------------------------------------------------------------- host
  const cpu = liveMeter('CPU', { unit: '%' });
  const memory = liveMeter('Memory', { unit: ' MB' });
  const gpuMeters = new Map();
  const gpuBox = h('div', { class: 'stack' });
  const hostNote = h('p', { class: 'small muted' });
  let hostBuilt = false;

  const readMetrics = async () => {
    const data = await get('/api/system/metrics');
    const mem = data.host.memory || {};
    history.cpu.push(data.host.cpu_percent || 0);
    history.memory.push(mem.used_mb || 0);
    cpu.set(data.host.cpu_percent || 0, 100, `${Math.round(data.host.cpu_percent || 0)}% of ${data.host.cpu_count} threads`, history.cpu.values);
    memory.set(mem.used_mb || 0, mem.total_mb || 0, `${mb(mem.used_mb)} of ${mb(mem.total_mb)}`, history.memory.values);
    const notes = [];
    for (const gpu of data.gpus || []) {
      const name = gpu.name || gpu.vendor;
      const key = `${gpu.index ?? name}`;
      if (!gpuMeters.has(key)) {
        const entry = { load: null, vram: null, el: h('div', { class: 'stack tight' }) };
        gpuMeters.set(key, entry);
        gpuBox.append(entry.el);
      }
      const entry = gpuMeters.get(key);
      if (!history.gpu.has(key)) history.gpu.set(key, series());
      if (gpu.util_percent != null) {
        if (!entry.load) { entry.load = liveMeter(`${name} load`, { unit: '%' }); entry.el.append(entry.load.el); }
        history.gpu.get(key).push(gpu.util_percent);
        entry.load.set(gpu.util_percent, 100, `${Math.round(gpu.util_percent)}%`, history.gpu.get(key).values);
      }
      if (gpu.vram_total_mb) {
        if (!entry.vram) { entry.vram = liveMeter(`${name} video memory`, { spark: false }); entry.el.append(entry.vram.el); }
        entry.vram.set(gpu.vram_used_mb || 0, gpu.vram_total_mb, `${mb(gpu.vram_used_mb || 0)} of ${mb(gpu.vram_total_mb)}`);
      }
      if (gpu.util_percent == null && !gpu.vram_total_mb) notes.push(`${name}: this driver reports no load figure`);
      if (gpu.temp_c != null) notes.push(`${name} ${gpu.temp_c} °C`);
    }
    hostNote.textContent = [`load ${(data.host.load || []).join(' ')}`,
      ...(data.host.temps || []).map((t) => `${t.sensor} ${t.celsius} °C`), ...notes,
      `Androids use ${Math.round(data.totals.cpu_percent)}% CPU and ${mb(data.totals.mem_mb)}`].join(' · ');
    if (!hostBuilt) {
      hostBuilt = true;
      replace(hostBody, cpu.el, memory.el, gpuBox, hostNote);
    }
  };

  const readDoctor = async () => {
    checks = await get('/api/system/doctor');
    if (guideState) { guideState.doctor = checks; drawGuide(); }
    drawAlerts();
  };

  // A card that cannot load says so in place; the rest of the page still works.
  const guarded = (fn, target) => () => fn().catch((error) => {
    replace(target, h('p', { class: 'fail' }, error.message));
  });
  await scope.every(5000, readFleet);
  scope.every(5000, () => readMetrics().catch((error) => {
    hostBuilt = false;                      // the meters come back with the next good answer
    replace(hostBody, h('p', { class: 'fail' }, error.message));
  }));
  scope.every(60000, guarded(readDoctor, alertsBody));
  scope.every(20000, () => readGuide().catch(() => {}));
  scope.on(window, 'sf:changed', () => readFleet().catch(() => {}));
  scope.on(window, 'sf:guide', () => readGuide().catch(() => {}));
}
