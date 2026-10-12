// Androids: the fleet as a table or a thumbnail grid, with filters, multi-select and
// bulk actions. Every per-Android option can be set here: at creation ("New Android"),
// for one Android (its Settings tab) and for any selection (the bulk bar).

import { patch, post } from '../api.js';
import { badge, button, eachInstance, empty, field, fleetAction, loadInstances, option, pageHead } from '../common.js';
import { openDialog } from '../components/dialog.js';
import { thumbnails } from '../components/live.js';
import { dataTable, selectorBox } from '../components/table.js';
import { attempt, fail, ok } from '../components/toast.js';
import { h, replace, selectorOf } from '../dom.js';
import { instanceForm, loadOptions } from '../instance-form.js';

const VIEW_KEY = 'sf-androids-view';
const RUNNING = new Set(['running', 'booted', 'starting']);

const rendering = (i) => (i.gpu ? i.gpu.mode || `${i.gpu.requested} (at next start)` : '');
const network = (i) => (i.net ? [i.net.mode, i.net.profile || i.net.gateway || i.net.proxy].filter(Boolean).join(' · ') : '');

async function newAndroid(reload) {
  const options = await attempt(loadOptions);
  if (!options) return;
  const count = h('input', { type: 'number', min: 1, max: 500, value: 1 });
  const startNow = h('input', { type: 'checkbox' });
  const form = instanceForm(options, { mode: 'create' });
  await openDialog({
    title: 'New Android',
    wide: true,
    body: [
      h('div', { class: 'fields' }, field('How many', count), field('Start them now', startNow)),
      form.el,
    ],
    actions: [{
      label: 'Create', kind: 'primary',
      run: async () => {
        const template = form.value();
        const name = template.name || '';
        delete template.name;
        const data = await post('/api/instances', { count: Number(count.value) || 1, name, template });
        const ids = data.instances.map((spec) => spec.id);
        ok(`Created Android ${selectorOf(ids)}.`);
        await reload();
        if (startNow.checked) fleetAction('start', ids).then(reload);
      },
    }],
  });
}

// One form for a selection. `groups` narrows it to a part (rendering, network).
async function bulkSettings(ids, title, groups, reload) {
  const options = await attempt(loadOptions);
  if (!options) return;
  const form = instanceForm(options, { mode: 'bulk', groups });
  const force = h('input', { type: 'checkbox' });
  await openDialog({
    title: `${title} for ${ids.length} Android(s)`,
    wide: true,
    body: [
      h('p', { class: 'muted' }, `Android ${selectorOf(ids)}. Only the fields marked "change this" are written; an empty one returns that setting to the farm default. Changes take effect at each Android's next start.`),
      form.el,
      field('Force changes that can lose data (another Android version on existing data)', force),
    ],
    actions: [{
      label: 'Apply', kind: 'primary',
      run: async () => {
        const changes = form.value();
        if (!Object.keys(changes).length) throw new Error('Nothing is marked to change.');
        const failures = await eachInstance(ids, (id) => patch(`/api/instances/${id}`, { patch: changes, force: force.checked }));
        if (failures.length) fail(`${failures.length} of ${ids.length} not changed. ${failures[0]}`);
        else ok(`Saved for ${ids.length} Android(s). Restart them to apply.`);
        await reload();
      },
    }],
  });
}

async function bulkTags(rows, reload) {
  const adding = h('input', { type: 'text', placeholder: 'eu, batch-2' });
  const removing = h('input', { type: 'text', placeholder: 'spare' });
  const list = (input) => input.value.split(',').map((t) => t.trim().toLowerCase()).filter(Boolean);
  await openDialog({
    title: `Tags for ${rows.length} Android(s)`,
    body: h('div', { class: 'fields' }, field('Add tags', adding, 'Comma-separated.'), field('Remove tags', removing)),
    actions: [{
      label: 'Apply', kind: 'primary',
      run: async () => {
        const add = list(adding);
        const remove = new Set(list(removing));
        if (!add.length && !remove.size) throw new Error('Type at least one tag to add or remove.');
        const tagsOf = Object.fromEntries(rows.map((row) => [row.id, row.tags || []]));
        const failures = await eachInstance(rows.map((row) => row.id), (id) => {
          const tags = [...new Set([...tagsOf[id], ...add])].filter((tag) => !remove.has(tag));
          return patch(`/api/instances/${id}`, { patch: { tags } });
        });
        if (failures.length) fail(failures[0]);
        else ok('Tags saved.');
        await reload();
      },
    }],
  });
}

export async function render(root, params, scope) {
  let instances = [];
  let view = 'table';
  try { view = localStorage.getItem(VIEW_KEY) || 'table'; } catch { /* storage may be blocked; the table is fine */ }
  const filters = {
    state: h('select', { 'aria-label': 'Filter by state' }),
    tag: h('select', { 'aria-label': 'Filter by tag' }),
    version: h('select', { 'aria-label': 'Filter by Android version' }),
    gpu: h('select', { 'aria-label': 'Filter by rendering' }),
    net: h('select', { 'aria-label': 'Filter by network mode' }),
  };
  const facet = { state: (i) => i.state, version: (i) => i.android_version, gpu: (i) => (i.gpu || {}).mode || (i.gpu || {}).requested, net: (i) => (i.net || {}).mode };
  const names = { state: 'Any state', tag: 'Any tag', version: 'Any version', gpu: 'Any rendering', net: 'Any network' };

  const table = dataTable({
    selectable: true,
    label: 'Androids',
    columns: [
      { title: 'ID', class: 'num', cell: (i) => i.id },
      { title: 'Name', cell: (i) => h('a', { href: `#/android/${i.id}` }, i.name) },
      { title: 'State', cell: (i) => [badge(i.state), i.busy ? h('span', { class: 'small muted' }, ` ${i.busy}`) : null,
        i.last_error && i.state === 'error' ? h('div', { class: 'small fail' }, i.last_error) : null,
        i.config_error ? h('div', { class: 'small fail' }, i.config_error) : null] },
      { title: 'Android', cell: (i) => i.android_version || '' },
      { title: 'Rendering', cell: rendering },
      { title: 'Network', cell: network },
      { title: 'Screen', cell: (i) => (i.screen ? `${i.screen.width}×${i.screen.height} @${i.screen.fps} fps` : '') },
      { title: 'Tags', cell: (i) => (i.tags || []).map((tag) => [h('span', { class: 'tag' }, tag), ' ']) },
      { title: '', class: 'actions', cell: (i) => (RUNNING.has(i.state)
        ? h('button', { type: 'button', class: 'btn small', dataset: { focus: `stop:${i.id}` }, onclick: () => act('stop', [i.id]) }, 'Stop')
        : h('button', { type: 'button', class: 'btn small', dataset: { focus: `start:${i.id}` }, onclick: () => act('start', [i.id]) }, 'Start')) },
    ],
    empty: empty('No Androids match', 'Clear the filters, or create an Android.', button('New Android', () => newAndroid(reload), 'primary')),
  });

  const grid = h('div', { class: 'wall' });
  const thumbs = thumbnails(scope);
  const bulk = h('div', { class: 'bulkbar', role: 'toolbar', 'aria-label': 'Actions for the selection', hidden: true });
  const toggle = h('button', { type: 'button', class: 'btn', 'aria-pressed': String(view === 'grid') }, 'Thumbnails');
  const body = h('div');

  // Tiles are kept between refreshes (keyed by id) so a thumbnail is not thrown away
  // and fetched again every time the list is polled.
  const tiles = new Map();
  function drawGrid(rows) {
    const keep = new Set(rows.map((i) => i.id));
    for (const [id, tile] of tiles) {
      if (!keep.has(id)) { if (tile.img) thumbs.forget(tile.img); tiles.delete(id); }
    }
    replace(grid, rows.map((i) => {
      let tile = tiles.get(i.id);
      if (!tile) {
        const box = h('input', { type: 'checkbox', onchange: () => {
          const next = new Set(table.selection);
          next[box.checked ? 'add' : 'delete'](i.id);
          table.setSelection(next);
        } });
        tile = { box, name: h('a', { class: 'name', href: `#/android/${i.id}` }), state: h('span'), picture: h('div'), img: null };
        tile.el = h('div', { class: 'tile' }, h('div', { class: 'tile-head' }, box, tile.name, tile.state), tile.picture);
        tiles.set(i.id, tile);
      }
      tile.box.checked = table.selection.has(i.id);
      tile.box.setAttribute('aria-label', `Select ${i.name}`);
      tile.el.setAttribute('aria-selected', String(table.selection.has(i.id)));
      tile.name.textContent = i.name;
      replace(tile.state, badge(i.state));
      const live = RUNNING.has(i.state);
      if (live && !tile.img) {
        tile.img = thumbs.image(i.id, `Screen of ${i.name}`);
        replace(tile.picture, tile.img);
      } else if (!live) {
        if (tile.img) thumbs.forget(tile.img);
        tile.img = null;
        replace(tile.picture, h('div', { class: 'thumb' }));
      }
      return tile.el;
    }));
    if (!rows.length) replace(grid, empty('No Androids match', 'Clear the filters, or create an Android.'));
  }

  const visible = () => instances.filter((i) => Object.entries(filters).every(([key, select]) => {
    if (!select.value) return true;
    return key === 'tag' ? (i.tags || []).includes(select.value) : String(facet[key](i)) === select.value;
  }));

  function drawFilters() {
    for (const [key, select] of Object.entries(filters)) {
      const values = key === 'tag' ? instances.flatMap((i) => i.tags || []) : instances.map(facet[key]);
      const unique = [...new Set(values.filter(Boolean).map(String))].sort();
      const current = select.value;
      replace(select, option('', names[key]), unique.map((value) => option(value)));
      select.value = unique.includes(current) ? current : '';
    }
  }

  function draw() {
    const rows = visible();
    table.update(rows);
    table.el.hidden = view !== 'table';
    grid.hidden = view !== 'grid';
    if (view === 'grid') drawGrid(rows);
    if (instances.length === 0) {
      replace(body, empty('No Androids yet', 'An Android is one container with its own data, network and identity. Create one, then start it.',
        button('New Android', () => newAndroid(reload), 'primary')));
    } else if (!body.contains(table.el)) {
      replace(body, table.el, grid);
    }
  }

  async function reload() {
    instances = await loadInstances();
    drawFilters();
    draw();
  }

  async function act(action, ids) {
    await fleetAction(action, ids);
    await reload().catch(() => {});
  }

  const selectedRows = () => instances.filter((i) => table.selection.has(i.id));
  table.onSelection((selection) => {
    bulk.hidden = selection.size === 0;
    const ids = () => [...table.selection];
    replace(bulk,
      h('strong', null, `${selection.size} selected`),
      ['start', 'stop', 'restart', 'recreate'].map((action) => button(action[0].toUpperCase() + action.slice(1), () => act(action, ids()))),
      button('Rendering…', () => bulkSettings(ids(), 'Rendering', ['Rendering and screen'], reload)),
      button('Network…', () => bulkSettings(ids(), 'Network', ['Network'], reload)),
      button('Tags…', () => bulkTags(selectedRows(), reload)),
      button('All settings…', () => bulkSettings(ids(), 'Settings', null, reload)),
      h('a', { class: 'btn', href: `#/control/${selectorOf(selection)}` }, 'Control'),
      button('Delete', () => act('delete', ids()), 'danger'));
    if (view === 'grid') draw();
  });

  for (const select of Object.values(filters)) select.addEventListener('change', draw);
  toggle.addEventListener('click', () => {
    view = view === 'grid' ? 'table' : 'grid';
    toggle.setAttribute('aria-pressed', String(view === 'grid'));
    try { localStorage.setItem(VIEW_KEY, view); } catch { /* not remembered, still works */ }
    draw();
  });

  root.append(
    pageHead('Androids', null, toggle, button('New Android', () => newAndroid(reload), 'primary')),
    h('div', { class: 'toolbar' }, selectorBox(table, () => instances), Object.values(filters)),
    bulk, body);

  await scope.every(4000, reload);
  scope.on(window, 'sf:changed', () => reload().catch(() => {}));
  if (params[0] === 'new') {
    history.replaceState(null, '', '#/androids');      // so a reload does not open the dialog again
    newAndroid(reload);
  }
}
