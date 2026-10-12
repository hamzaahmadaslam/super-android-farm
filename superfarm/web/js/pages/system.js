// System: what this machine is, how many Androids it can run and why, its GPUs, the
// host checklist with the exact fix for each problem, and the Android images.

import { get, post } from '../api.js';
import { badge, button, capacityCard, emptyState, kv, pageHead, panel, skeleton } from '../common.js';
import { help } from '../components/help.js';
import { dataTable } from '../components/table.js';
import { finished } from '../components/tasks.js';
import { attempt, ok } from '../components/toast.js';
import { copyText, h, mb, replace } from '../dom.js';
import { icon } from '../icons.js';

const ORDER = { fail: 0, warn: 1, skip: 2, ok: 3 };

function limits(capacity) {
  const estimate = capacity.estimate || {};
  const l = estimate.limits || {};
  const rows = [];
  if (l.ram) rows.push(['ram', 'Memory (RAM)', l.ram.max, `${mb(l.ram.per_android_mb)} per Android; ${mb(l.ram.total_mb)} installed, ${mb(l.ram.host_reserve_mb)} kept for the host${l.ram.max_idle_with_zram ? `; about ${l.ram.max_idle_with_zram} mostly idle Androids with compressed swap` : ''}`]);
  if (l.cpu) rows.push(['cpu', 'CPU', l.cpu.max, `${l.cpu.percent_per_android}% of one thread per Android (${l.cpu.workload} workload) on ${l.cpu.threads} threads, planned to ${Math.round(l.cpu.target_utilisation * 100)}%`]);
  if (l.vram) rows.push(['vram', 'Video memory', l.vram.max, `${mb(l.vram.per_android_mb)} per Android on ${l.vram.cards} card(s) with ${mb(l.vram.total_mb)}`]);
  return h('div', { class: 'table-wrap' }, h('table', { 'aria-label': 'Capacity by resource' },
    h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Resource'), h('th', { scope: 'col', class: 'num' }, 'Androids'), h('th', { scope: 'col' }, 'How it is counted'))),
    h('tbody', null, rows.map(([key, name, max, how]) => h('tr', null,
      h('th', { scope: 'row' }, name, key === estimate.binding ? [' ', badge('warn', 'limits first')] : null),
      h('td', { class: 'num' }, max), h('td', null, how))))));
}

export async function render(root, params, scope) {
  const head = (title, name, topic, actions) => h('div', { class: 'card-head' }, h('h2', null, icon(name), title, topic && help(topic)), actions);
  const host = h('section', { class: 'card' }, head('This machine', 'system'), skeleton(4));
  const capacity = h('section', { class: 'card' }, head('Capacity estimate', 'gauge', 'capacity'), skeleton(4));
  const gpus = h('section', { class: 'card' }, head('Graphics', 'layers', 'rendering'), skeleton(3));
  const checks = h('section', { class: 'card', id: 'checks' }, head('Host checks', 'shield'), skeleton(5));
  const images = panel('Android images', { icon: 'layers' });
  images.id = 'images';
  root.append(
    pageHead('System', 'What this machine is, how many Androids it can run and why, and whether the host is set up for them.',
      button('Run doctor', () => attempt(() => loadChecks(true), 'Host checks ran again.').then(() => checks.scrollIntoView({ block: 'start' })), '', 'shield')),
    h('div', { class: 'stack' }, h('div', { class: 'cols-machine' }, h('div', { class: 'stack' }, host, gpus), capacity), checks, images));

  const system = await get('/api/system');
  replace(host, head('This machine', 'system'), kv({
    'Super Farm': system.version, 'Farm home': system.home,
    System: `${system.host.distro} (${system.host.family})`, Kernel: `${system.host.kernel} ${system.host.arch}`,
    Processor: `${system.host.cpu.model}, ${system.host.cpu.threads} threads`,
    Memory: `${mb(system.host.ram_mb)} RAM, ${mb(system.host.swap_mb)} swap`,
  }),
  h('h3', null, 'Parts of the farm'),
  h('ul', null, Object.entries(system.components || {}).filter(([, c]) => !c.available).map(([name, c]) => h('li', null, badge('warn', name), ' not available: ', c.reason))),
  Object.values(system.components || {}).every((c) => c.available) && h('p', null, badge('ok', 'all available'), ' Every part of the farm is available on this machine.'));

  const loadCapacity = async () => {
    const cap = await get('/api/system/capacity');
    replace(capacity, head('Capacity estimate', 'gauge', 'capacity'),
      cap.estimate && cap.estimate.error ? h('div', { class: 'banner fail' }, icon('alert'), h('div', { class: 'grow' }, `The estimate could not be computed: ${cap.estimate.error}`))
        : [capacityCard(cap, { advice: 50 }), limits(cap),
          h('p', { class: 'small muted' }, cap.estimate.assumptions ? `Measured on: ${cap.estimate.assumptions.reference}. ` : '',
            cap.enforced ? 'performance.enforce_capacity is on: a start over the estimate asks for "Run anyway".'
              : 'The estimate is advice. performance.enforce_capacity is off, so starts over it only warn.')]);
  };

  const loadGpus = async () => {
    const data = await get('/api/system/gpus');
    const relays = data.relay.relays || [];
    const runtime = data.relay.runtime || {};
    replace(gpus, head('Graphics', 'layers', 'rendering'),
      data.gpus.length === 0 ? emptyState('layers', 'No GPU was found', 'Androids draw on the CPU (software rendering), which costs several times more CPU.')
        : h('ul', { class: 'gpu-list', 'aria-label': 'Graphics cards' }, data.gpus.map((g) => h('li', { class: 'gpu-item' },
          h('strong', null, g.name),
          h('div', { class: 'row small muted' },
            h('span', null, (g.integrated ?? !g.discrete) ? 'integrated' : 'discrete'), '·', h('span', null, `driver ${g.driver}`), '·',
            h('code', null, g.node), '·', h('span', null, g.vram_mb ? `${mb(g.vram_mb)} video memory` : 'shared memory')),
          h('div', { class: 'row small' }, h('span', { class: 'muted' }, 'usable as'),
            (g.usable_modes || []).length ? g.usable_modes.map((mode) => h('code', null, mode)) : h('span', { class: 'warn' }, g.note || 'not usable'))))),
      h('h3', null, 'Rendering choices for an Android'),
      h('div', null, (data.options || []).map((o) => h('div', { class: 'alert' }, badge(o.available ? 'ok' : '', o.available ? 'available' : 'unavailable'),
        h('div', { class: 'body' }, h('strong', null, o.label), ' ', h('code', null, o.node ? `${o.value}@${o.node}` : o.value),
          o.reason ? h('div', { class: 'chip-reason' }, o.reason) : null)))),
      data.gpus.some((g) => g.vendor === 'nvidia') && [
        h('h3', null, 'NVIDIA relay'),
        h('p', null, badge(runtime.ready ? 'ok' : 'warn', runtime.ready ? 'runtime installed' : 'runtime incomplete'), ' ',
          runtime.ready ? `Built for ${runtime.built_for}.` : `Missing: ${(runtime.missing || []).join(', ')}`),
        h('ul', null, relays.map((r) => h('li', null, badge(r.running ? 'ok' : '', r.running ? 'running' : 'not running'), ' ',
          h('code', null, r.node), r.running ? ` (pid ${r.pid})` : ' — started automatically with the first NVIDIA Android'))),
      ]);
  };

  // A declaration, not a const: the page-head button may be pressed before this line runs.
  async function loadChecks(fresh) {
    const data = await get('/api/system/doctor', fresh ? { fresh: true } : null);
    const sorted = [...data.checks].sort((a, b) => ORDER[a.status] - ORDER[b.status]);
    const counts = data.summary.counts;
    const problems = sorted.filter((check) => check.status !== 'ok');
    const passing = sorted.filter((check) => check.status === 'ok');
    const wasOpen = Boolean(checks.querySelector('details[open]'));
    const row = (check) => h('div', { class: 'alert' },
      badge(check.status),
      h('div', { class: 'body grow' }, h('strong', null, check.title), ' ', h('span', { class: 'muted' }, check.detail),
        check.fix && check.status !== 'ok' && h('div', { class: 'fix' }, h('pre', null, check.fix),
          button('Copy', () => copyText(check.fix).then(() => ok('Command copied.'), () => {}), 'small', 'copy'))));
    replace(checks,
      head('Host checks', 'shield', null, [
        h('span', { class: 'row' }, badge('ok', `${counts.ok} ok`), counts.warn ? badge('warn', `${counts.warn} warnings`) : null,
          counts.fail ? badge('fail', `${counts.fail} failed`) : null, counts.skip ? badge('skip', `${counts.skip} skipped`) : null),
        button('Run again', () => attempt(() => loadChecks(true), 'Host checks ran again.'), 'small', 'restart')]),
      // What needs a look comes first and stays open; what passes folds away.
      problems.length === 0 && h('p', null, badge('ok', 'all good'), ' Every host check passes.'),
      h('div', null, problems.map(row)),
      passing.length > 0 && h('details', { open: wasOpen || problems.length === 0 },
        h('summary', null, `${passing.length} passing check(s)`), h('div', null, passing.map(row))));
  }

  const imageTable = dataTable({
    label: 'Android images',
    key: (i) => i.version,
    columns: [
      { title: 'Android', cell: (i) => i.version },
      { title: 'Base image', cell: (i) => [h('code', null, i.base), ' ', i.base_present ? badge('ok', 'pulled') : badge('', 'not pulled')] },
      { title: 'Farm image', cell: (i) => [h('code', null, i.tag), ' ', i.built ? badge(i.stale ? 'warn' : 'ok', i.stale ? `stale: ${i.stale_reason}` : 'built') : badge('', i.custom ? 'missing' : 'not built')] },
      { title: 'Features', cell: (i) => Object.entries(i.features || {}).map(([name, f]) => [
        h('span', { class: `badge ${f.available ? 'ok' : ''}`, title: f.unsupported || f.reason || null },
          `${name}: ${f.available ? 'ready' : f.supported ? 'not built' : 'not on this version'}`), ' ']) },
      { title: '', class: 'actions', cell: (i) => [
        button('Prepare', () => imageTask(i.version, 'ensure'), i.built ? 'small' : 'small primary'), ' ',
        button('Rebuild', () => imageTask(i.version, 'build'), 'small'),
      ] },
    ],
  });
  const loadImages = async () => imageTable.update((await get('/api/images')).images);
  const imageTask = (version, action) => attempt(async () => {
    const data = await post(`/api/images/${encodeURIComponent(version)}/${action}`);
    await finished(data.task);
    await loadImages();
  });
  images.append(h('p', { class: 'small muted' }, '"Prepare" pulls the base image if needed and builds the farm image; it also happens automatically at the first start of an Android of that version.'), imageTable.el);

  const guarded = (fn, target, title) => fn().catch((error) => replace(target, h('h2', null, title), h('p', { class: 'fail' }, error.message)));
  // "#/system/checks/run" is the command palette's "Run doctor": read the host again.
  const rerun = params[0] === 'checks' && params[1] === 'run';
  if (rerun) history.replaceState(null, '', '#/system/checks');
  await Promise.all([guarded(loadCapacity, capacity, 'Capacity estimate'), guarded(loadGpus, gpus, 'Graphics'),
    guarded(() => loadChecks(rerun), checks, 'Host checks'), guarded(loadImages, images, 'Android images')]);
  if (rerun) ok('Host checks ran again.');
  if (params[0] === 'checks') checks.scrollIntoView({ block: 'start' });
  if (params[0] === 'images') images.scrollIntoView({ block: 'start' });
  scope.every(10000, loadCapacity).catch(() => {});
}
