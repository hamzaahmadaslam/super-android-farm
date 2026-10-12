// Dashboard: is the farm healthy, how full is the machine, what is happening now.

import { get } from '../api.js';
import { badge, button, capacityCard, card, empty, loadInstances, meter, pageHead } from '../common.js';
import { toggle as toggleTasks } from '../components/tasks.js';
import { h, mb, replace } from '../dom.js';

function counts(instances) {
  const by = {};
  for (const instance of instances) by[instance.state] = (by[instance.state] || 0) + 1;
  return by;
}

function stat(value, label, href) {
  return h('a', { class: 'card stat', href }, h('span', { class: 'value' }, String(value)), h('span', { class: 'label' }, label));
}

function gpuMeters(gpus) {
  return gpus.map((gpu) => {
    const name = gpu.name || gpu.vendor;
    const parts = [];
    if (gpu.util_percent != null) parts.push(meter(`${name} load`, gpu.util_percent, 100, `${Math.round(gpu.util_percent)}%`));
    if (gpu.vram_total_mb) parts.push(meter(`${name} video memory`, gpu.vram_used_mb || 0, gpu.vram_total_mb, `${mb(gpu.vram_used_mb || 0)} of ${mb(gpu.vram_total_mb)}`));
    if (!parts.length) parts.push(h('p', { class: 'muted' }, `${name}: this driver reports no load figure.`));
    if (gpu.temp_c != null) parts.push(h('p', { class: 'small muted' }, `${name}: ${gpu.temp_c} °C`));
    return parts;
  });
}

export async function render(root, _params, scope) {
  const stats = h('div', { class: 'grid cols-4' });
  const capacity = card('Capacity');
  const host = card('Host');
  const tasks = card('Active tasks');
  const alerts = card('Host checks');
  root.append(
    pageHead('Dashboard', null, h('a', { class: 'btn primary', href: '#/androids' }, 'Manage Androids')),
    h('div', { class: 'stack' }, stats, h('div', { class: 'grid cols-2' }, capacity, host), h('div', { class: 'grid cols-2' }, tasks, alerts)));

  const fleet = async () => {
    const [instances, cap, active] = await Promise.all([
      loadInstances(), get('/api/system/capacity'), get('/api/tasks', { active: true }),
    ]);
    const by = counts(instances);
    const up = (by.booted || 0) + (by.running || 0);
    replace(stats,
      stat(instances.length, 'Androids defined', '#/androids'),
      stat(up, 'running', '#/androids'),
      stat(by.starting || 0, 'starting', '#/androids'),
      stat(by.error || 0, 'with an error', '#/androids'));
    replace(capacity, h('h2', null, 'Capacity'), capacityCard(cap, { advice: 2 }),
      h('p', null, h('a', { href: '#/system' }, 'How the estimate is counted, and all advice')));
    const list = active.tasks || [];
    replace(tasks, h('h2', null, 'Active tasks'),
      list.length === 0 ? h('p', { class: 'muted' }, 'Nothing is running in the background.')
        : h('ul', null, list.map((task) => h('li', null, task.title, ' ', badge(task.state), ' ',
          h('span', { class: 'muted' }, task.message)))),
      button('Open the task list', () => toggleTasks(true), 'small'));
    if (instances.length === 0 && !root.querySelector('.empty')) {
      root.append(empty('No Androids yet', 'Create the first one, then start it.',
        h('a', { class: 'btn primary', href: '#/androids/new' }, 'New Android')));
    }
  };

  const metrics = async () => {
    const data = await get('/api/system/metrics');
    const memory = data.host.memory || {};
    replace(host, h('h2', null, 'Host'), h('div', { class: 'stack' },
      meter('CPU', data.host.cpu_percent || 0, 100, `${Math.round(data.host.cpu_percent || 0)}% of ${data.host.cpu_count} threads`),
      meter('Memory', memory.used_mb || 0, memory.total_mb || 0, `${mb(memory.used_mb)} of ${mb(memory.total_mb)}`),
      gpuMeters(data.gpus || []),
      h('p', { class: 'small muted' },
        [`load ${(data.host.load || []).join(' ')}`,
          ...(data.host.temps || []).map((t) => `${t.sensor} ${t.celsius} °C`),
          `Androids use ${Math.round(data.totals.cpu_percent)}% CPU and ${mb(data.totals.mem_mb)}`].join(' · '))));
  };

  const doctor = async () => {
    const data = await get('/api/system/doctor');
    const problems = data.checks.filter((check) => check.status === 'fail' || check.status === 'warn');
    replace(alerts, h('h2', null, 'Host checks'),
      problems.length === 0 ? h('p', null, badge('ok', 'all good'), ' Every host check passes.')
        : h('ul', null, problems.slice(0, 8).map((check) => h('li', null, badge(check.status), ' ', h('strong', null, check.title), ' ', h('span', { class: 'muted' }, check.detail.length > 140 ? `${check.detail.slice(0, 140)}…` : check.detail)))),
      h('p', null, h('a', { href: '#/system/checks' }, problems.length ? 'See all checks and how to fix them' : 'See all checks')));
  };

  // A card that cannot load says so in place; the rest of the page still works.
  const guarded = (fn, target, title) => () => fn().catch((error) => {
    replace(target, h('h2', null, title), h('p', { class: 'fail' }, error.message));
  });
  await scope.every(5000, fleet);
  scope.every(5000, guarded(metrics, host, 'Host'));
  scope.every(60000, guarded(doctor, alerts, 'Host checks'));
  scope.on(window, 'sf:changed', () => fleet().catch(() => {}));
}
