// Androids: the fleet as a grid of live cards (small farms) or a dense table (large
// ones), with quick filters, multi-select and a bar of bulk actions. Every per-Android
// option can be set here: at creation ("New Android"), for one Android (its Settings
// tab) and for any selection (the bulk bar).

import { patch, post } from '../api.js';
import { affectedList, androidsText, ANDROID_STATES, button, eachInstance, emptyState, field, fleetAction, iconButton, loadInstances, networkOf, pageHead, plural, renderingOf, stateBadge, UP } from '../common.js';
import { openDialog } from '../components/dialog.js';
import { thumbnails } from '../components/live.js';
import { menuButton, openMenu } from '../components/popover.js';
import { pageShortcut } from '../components/shortcuts.js';
import { dataTable, selectorBox } from '../components/table.js';
import { attempt, fail, ok } from '../components/toast.js';
import { h, replace, selectorOf } from '../dom.js';
import { icon } from '../icons.js';
import { instanceForm, loadOptions } from '../instance-form.js';
import { getPref, setPref } from '../prefs.js';
import { rememberInstances, rememberSelection } from '../state.js';

const VIEW = 'androids-view';
const FILTERS = 'androids-filters';
const TABLE_ABOVE = 60;               // more Androids than this open as a table unless the operator chose
const STATE_ORDER = ['booted', 'running', 'starting', 'stopped', 'created', 'missing', 'error'];

async function newAndroid(reload) {
  const options = await attempt(loadOptions);
  if (!options) return;
  const count = h('input', { type: 'number', min: 1, max: 500, value: 1 });
  const startNow = h('input', { type: 'checkbox' });
  const form = instanceForm(options, { mode: 'create' });
  await openDialog({
    title: 'New Android',
    icon: 'plus',
    wide: true,
    body: [
      h('p', { class: 'muted' }, 'Only the count is needed. Everything left empty follows the farm defaults from Settings and can be changed later.'),
      h('div', { class: 'fields' }, field('How many', count, null, { path: 'count' }), field('Start them now', startNow, null, { topic: 'start-pace' })),
      form.el,
    ],
    actions: [{
      label: 'Create', kind: 'primary',
      run: async () => {
        const template = form.value();
        const name = template.name || '';
        delete template.name;
        // The count goes out as typed: the server says what is wrong with 0 or -3, at the field.
        if (count.validity.badInput) throw Object.assign(new Error('How many: this is not a number.'), { detail: [{ field: 'count', message: 'This is not a number.' }] });
        const data = await post('/api/instances', { count: count.value === '' ? 1 : Number(count.value), name, template });
        const ids = data.instances.map((spec) => spec.id);
        ok(`Created ${androidsText(ids)}.`);
        await reload();
        if (startNow.checked) fleetAction('start', ids).then(reload);
      },
    }],
  });
}

// One form for a selection. `groups` narrows it to a part (rendering, network).
async function bulkSettings(ids, rows, title, groups, reload) {
  const options = await attempt(loadOptions);
  if (!options) return;
  const form = instanceForm(options, { mode: 'bulk', groups });
  const force = h('input', { type: 'checkbox' });
  await openDialog({
    title: `${title} for ${plural(ids.length, 'Android')}`,
    icon: 'settings',
    wide: true,
    body: [
      h('p', { class: 'muted' }, `${androidsText(ids)}. Only the fields marked "change this" are written; an empty one returns that setting to the farm default. Changes take effect at each Android's next start.`),
      h('details', null, h('summary', null, `Show the ${plural(ids.length, 'Android')} this applies to`), affectedList(ids, rows)),
      form.el,
      field('Force changes that can lose data (another Android version on existing data)', force),
    ],
    actions: [{
      label: `Apply to ${ids.length}`, kind: 'primary',
      run: async () => {
        const changes = form.value();
        if (!Object.keys(changes).length) throw new Error('Nothing is marked to change.');
        const failures = await eachInstance(ids, (id) => patch(`/api/instances/${id}`, { patch: changes, force: force.checked }));
        if (failures.length) fail(`${failures.length} of ${ids.length} not changed. ${failures[0]}`);
        else ok(`Saved for ${plural(ids.length, 'Android')}. Restart them to apply.`);
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
    title: `Tags for ${plural(rows.length, 'Android')}`,
    icon: 'tag',
    body: [
      h('p', { class: 'muted' }, `${androidsText(rows.map((row) => row.id))}. A tag groups Androids so they can be selected together with tag:name.`),
      h('div', { class: 'fields' }, field('Add tags', adding, 'Comma-separated.'), field('Remove tags', removing))],
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
        else ok(`Tags saved on ${plural(rows.length, 'Android')}.`);
        await reload();
      },
    }],
  });
}

export async function render(root, params, scope) {
  let instances = [];
  let loaded = false;
  let view = getPref(VIEW, null);
  const saved = getPref(FILTERS, null) || {};
  const filters = { q: '', state: saved.state || '', tag: saved.tag || '', version: saved.version || '', gpu: saved.gpu || '', net: saved.net || '' };
  const facet = {
    state: (i) => i.state, version: (i) => i.android_version,
    gpu: (i) => (i.gpu || {}).mode || (i.gpu || {}).requested, net: (i) => (i.net || {}).mode,
  };
  const FACETS = [['tag', 'Tag', 'tag'], ['version', 'Android', 'device'], ['gpu', 'Rendering', 'layers'], ['net', 'Network', 'network']];
  const valuesOf = (key, i) => (key === 'tag' ? (i.tags || []) : [facet[key](i)]).filter((v) => v != null && v !== '').map(String);

  const actions = (i, small = true) => {
    const up = UP.has(i.state);
    return [
      h('button', {
        type: 'button', class: `btn ${small ? 'small' : ''}`, dataset: { focus: `${up ? 'stop' : 'start'}:${i.id}`, action: `power:${i.id}` },
        'aria-label': `${up ? 'Stop' : 'Start'} ${i.name}`, onclick: () => act(up ? 'stop' : 'start', [i.id]),
      }, icon(up ? 'stop' : 'play'), up ? 'Stop' : 'Start'),
      h('a', { class: `btn icon-only ${small ? 'small' : ''}`, href: `#/android/${i.id}`, 'aria-label': `Open ${i.name}`, title: 'Open', dataset: { focus: `go:${i.id}` } }, icon('open')),
      more(i, small),
    ];
  };
  function more(i, small) {
    const trigger = h('button', {
      type: 'button', class: `btn icon-only ${small ? 'small' : ''}`, 'aria-haspopup': 'menu', 'aria-expanded': 'false',
      'aria-label': `More actions for ${i.name}`, title: 'More…', dataset: { focus: `more:${i.id}` },
      onclick: () => openMenu(trigger, [
        { label: 'Restart', icon: 'restart', run: () => act('restart', [i.id]) },
        { label: 'Recreate', icon: 'recreate', run: () => act('recreate', [i.id]) },
        'sep',
        { label: 'Control', icon: 'control', href: `#/control/${i.id}` },
        { label: 'Settings', icon: 'settings', href: `#/android/${i.id}/settings` },
        { label: 'Logs', icon: 'terminal', href: `#/android/${i.id}/logs` },
        'sep',
        { label: 'Delete…', icon: 'trash', danger: true, run: () => act('delete', [i.id]) },
      ], { label: `Actions for ${i.name}`, align: 'end' }),
    }, icon('more'));
    return trigger;
  }

  const table = dataTable({
    selectable: true,
    dense: true,
    label: 'Androids',
    columns: [
      { title: 'ID', class: 'num', cell: (i) => i.id },
      { title: 'Name', class: 'one-line', cell: (i) => h('a', { class: 'clip', href: `#/android/${i.id}`, title: i.name }, i.name) },
      { title: 'State', cell: (i) => [stateBadge(i), i.busy ? h('span', { class: 'small muted' }, ` ${i.busy}`) : null,
        i.last_error && i.state === 'error' ? h('div', { class: 'small fail' }, i.last_error) : null,
        i.config_error ? h('div', { class: 'small fail' }, i.config_error) : null] },
      { title: 'Android', cell: (i) => i.android_version || '' },
      { title: 'Rendering', class: 'one-line', cell: renderingOf },
      { title: 'Network', class: 'one-line', cell: networkOf },
      { title: 'Screen', class: 'one-line', cell: (i) => (i.screen ? h('span', { class: 'num small' }, `${i.screen.width}×${i.screen.height} @${i.screen.fps}`) : '') },
      { title: 'Tags', cell: (i) => (i.tags || []).map((tag) => [h('span', { class: 'tag' }, tag), ' ']) },
      { title: h('span', { class: 'visually-hidden' }, 'Actions'), class: 'actions one-line', cell: (i) => h('span', { class: 'row end' }, actions(i)) },
    ],
  });

  const grid = h('div', { class: 'fleet', role: 'list', 'aria-label': 'Androids' });
  const thumbs = thumbnails(scope);
  const bulk = h('div', { class: 'bulkbar glass', role: 'toolbar', 'aria-label': 'Actions for the selection', hidden: true });
  const body = h('div');
  const stateChips = h('div', { class: 'chips', role: 'group', 'aria-label': 'Filter by state' });
  const facetChips = h('div', { class: 'chips' });
  const shownCount = h('span', { class: 'fleet-count', role: 'status' });
  const search = h('input', { type: 'search', placeholder: 'Filter by name, id or tag', 'aria-label': 'Filter Androids by name, id or tag', spellcheck: false, autocomplete: 'off' });
  const viewGrid = h('button', { type: 'button', onclick: () => setView('grid') }, icon('grid'), 'Cards');
  const viewTable = h('button', { type: 'button', onclick: () => setView('table') }, icon('rows'), 'Table');
  const viewToggle = h('div', { class: 'segmented', role: 'group', 'aria-label': 'View' }, viewGrid, viewTable);

  // Cards are kept between refreshes (keyed by id) so a thumbnail is not thrown away
  // and fetched again every time the list is polled, and focus stays where it was.
  const cards = new Map();
  function drawGrid(rows) {
    const keep = new Set(rows.map((i) => i.id));
    for (const [id, card] of cards) {
      if (!keep.has(id)) { if (card.img) thumbs.forget(card.img); cards.delete(id); }
    }
    const active = document.activeElement;
    const focusKey = grid.contains(active) ? active.dataset.focus : null;
    replace(grid, rows.map((i) => {
      let card = cards.get(i.id);
      if (!card) {
        const box = h('input', { type: 'checkbox', dataset: { focus: `check:${i.id}` }, onchange: () => {
          const next = new Set(table.selection);
          next[box.checked ? 'add' : 'delete'](i.id);
          table.setSelection(next);
        } });
        card = {
          box, link: h('a', { href: `#/android/${i.id}`, dataset: { focus: `open:${i.id}` } }), state: h('span', { class: 'acard-state' }), shot: h('div', { class: 'acard-shot' }),
          meta: h('div', { class: 'acard-meta' }), tags: h('div', { class: 'row' }), error: h('div', { class: 'acard-error', role: 'note' }),
          buttons: h('div', { class: 'acard-actions' }), img: null, live: null, signature: '',
        };
        card.el = h('article', { class: 'acard', role: 'listitem', dataset: { thumbOwner: String(i.id) } },
          card.shot, h('label', { class: 'acard-check' }, box), card.state,
          h('div', { class: 'acard-body' },
            h('div', { class: 'acard-title' }, card.link, h('span', { class: 'acard-id' }, `#${i.id}`)),
            card.meta, card.tags, card.error, card.buttons));
        cards.set(i.id, card);
      }
      card.box.checked = table.selection.has(i.id);
      card.box.setAttribute('aria-label', `Select ${i.name}`);
      card.el.classList.toggle('selected', table.selection.has(i.id));
      card.link.textContent = i.name;
      replace(card.state, stateBadge(i));
      // A picture only once Android is up: while it is still starting there is none to fetch.
      const live = i.state === 'booted' || i.state === 'running';
      if (live !== card.live) {
        card.live = live;
        if (card.img) { thumbs.forget(card.img); card.img = null; }
        if (live) {
          card.img = thumbs.image(i.id, `Screen of ${i.name}`);
          replace(card.shot, card.img);
        }
      }
      if (!live) {
        replace(card.shot, h('div', { class: 'off' }, icon(i.state === 'starting' ? 'spinner' : 'device', i.state === 'starting' ? 'spin' : ''),
          h('span', null, i.state === 'starting' ? 'starting…' : i.state === 'error' ? 'last start failed' : 'not running')));
      }
      replace(card.meta,
        i.android_version && h('span', { title: 'Android version' }, icon('device'), `Android ${i.android_version}`),
        renderingOf(i) && h('span', { title: 'Rendering' }, icon('layers'), renderingOf(i)),
        networkOf(i) && h('span', { title: 'Network' }, icon('network'), networkOf(i)));
      replace(card.tags, (i.tags || []).map((tag) => h('span', { class: 'tag' }, tag)));
      card.tags.hidden = !(i.tags || []).length;
      const problem = i.config_error || (i.state === 'error' ? i.last_error : '') || '';
      card.error.textContent = problem;
      card.error.title = problem;
      card.error.hidden = !problem;
      // Rebuilt only when they would differ, so an open menu keeps its button.
      const signature = `${UP.has(i.state)}:${i.name}`;
      if (card.signature !== signature) {
        card.signature = signature;
        replace(card.buttons, actions(i));
      }
      return card.el;
    }));
    if (focusKey) {
      const again = [...grid.querySelectorAll('[data-focus]')].find((node) => node.dataset.focus === focusKey);
      if (again) again.focus({ preventScroll: true });
    }
  }

  const matchesText = (i, q) => i.name.toLowerCase().includes(q) || String(i.id) === q || `#${i.id}` === q
    || (i.tags || []).some((tag) => tag.includes(q) || `tag:${tag}` === q);
  const passes = (i, skip) => {
    if (filters.q && !matchesText(i, filters.q)) return false;
    for (const key of ['state', 'tag', 'version', 'gpu', 'net']) {
      if (key !== skip && filters[key] && !valuesOf(key, i).includes(filters[key])) return false;
    }
    return true;
  };
  const visible = () => instances.filter((i) => passes(i));
  const anyFilter = () => Object.values(filters).some(Boolean);

  function setFilter(key, value) {
    filters[key] = value;
    const { q: _typed, ...kept } = filters;       // what was typed is for now; the chips are remembered
    setPref(FILTERS, Object.values(kept).some(Boolean) ? kept : null);
    draw();
  }
  function clearFilters() {
    for (const key of Object.keys(filters)) filters[key] = '';
    search.value = '';
    setPref(FILTERS, null);
    draw();
  }

  function drawFilters() {
    // Each chip counts what it would show with the other filters as they are.
    const countBy = (key) => {
      const counts = new Map();
      for (const i of instances) {
        if (!passes(i, key)) continue;
        for (const value of valuesOf(key, i)) counts.set(value, (counts.get(value) || 0) + 1);
      }
      return counts;
    };
    const states = countBy('state');
    const total = instances.filter((i) => passes(i, 'state')).length;
    const chip = (label, count, on, pick, title) => h('button', { type: 'button', class: 'chip', 'aria-pressed': String(on), title, onclick: pick },
      label, h('span', { class: 'count' }, count));
    const focusKey = document.activeElement && stateChips.contains(document.activeElement) ? document.activeElement.textContent : null;
    replace(stateChips,
      chip('All', total, !filters.state, () => setFilter('state', '')),
      STATE_ORDER.filter((state) => states.has(state) || filters.state === state)
        .map((state) => chip(state, states.get(state) || 0, filters.state === state, () => setFilter('state', filters.state === state ? '' : state), ANDROID_STATES[state])));
    if (focusKey) {
      const again = [...stateChips.children].find((node) => node.textContent === focusKey);
      if (again) again.focus({ preventScroll: true });
    }
    replace(facetChips, FACETS.map(([key, label, picture]) => {
      const counts = countBy(key);
      // Which chips exist depends on the whole fleet, so they do not come and go while filtering.
      const values = [...new Set(instances.flatMap((i) => valuesOf(key, i)))].sort();
      if (values.length < 2 && !filters[key]) return null;      // nothing to choose between
      const trigger = h('button', {
        type: 'button', class: `chip ${filters[key] ? 'on' : ''}`, 'aria-haspopup': 'menu', 'aria-expanded': 'false', dataset: { facet: key },
        'aria-label': filters[key] ? `${label}: ${filters[key]}. Change the filter` : `Filter by ${label.toLowerCase()}`,
        onclick: () => openMenu(trigger, [
          { label: `Any ${label.toLowerCase()}`, checked: !filters[key], run: () => setFilter(key, '') },
          ...values.map((value) => ({ label: value, count: counts.get(value) || 0, checked: filters[key] === value, run: () => setFilter(key, value) })),
        ], { label: `Filter by ${label.toLowerCase()}` }),
      }, icon(picture), filters[key] ? `${label}: ${filters[key]}` : label, icon('chevron-down'));
      return trigger;
    }),
    anyFilter() && h('button', { type: 'button', class: 'chip', onclick: clearFilters }, icon('x'), 'Clear filters'));
  }

  function setView(next) {
    view = next;
    setPref(VIEW, next);
    draw();
  }

  function draw() {
    const rows = visible();
    const current = view || (instances.length > TABLE_ABOVE ? 'table' : 'grid');
    viewGrid.setAttribute('aria-pressed', String(current === 'grid'));
    viewTable.setAttribute('aria-pressed', String(current === 'table'));
    drawFilters();
    shownCount.textContent = anyFilter() ? `${rows.length} of ${instances.length} shown` : plural(instances.length, 'Android');
    table.update(rows);
    if (!loaded) return;
    if (instances.length === 0) {
      replace(body, emptyState('devices', 'No Androids yet',
        'An Android is one container with its own data, network and identity. Create one or many here, then start them; every option can be changed later.',
        button('New Android', () => newAndroid(reload), 'primary', 'plus')));
      tools.hidden = true;
      return;
    }
    tools.hidden = false;
    if (rows.length === 0) {
      replace(body, emptyState('filter', 'No Androids match these filters', `${plural(instances.length, 'Android')} hidden by the filters above.`,
        button('Clear filters', clearFilters, '', 'x')));
      return;
    }
    if (current === 'grid') {
      drawGrid(rows);
      if (body.firstChild !== grid) replace(body, grid);
    } else if (body.firstChild !== table.el) {
      replace(body, table.el);
    }
  }

  async function reload() {
    instances = await loadInstances();
    loaded = true;
    rememberInstances(instances);
    draw();
  }

  async function act(action, ids) {
    await fleetAction(action, ids, instances);
    await reload().catch(() => {});
  }

  const selectedRows = () => instances.filter((i) => table.selection.has(i.id));
  table.onSelection((selection) => {
    rememberSelection(selection);
    bulk.hidden = selection.size === 0;
    const ids = () => [...table.selection];
    const keepFocus = bulk.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
    const b = (label, picture, run, kind = '') => h('button', { type: 'button', class: `btn ${kind}`, dataset: { focus: label }, onclick: run }, icon(picture), label);
    replace(bulk,
      h('span', { class: 'count', role: 'status', title: androidsText(selection) }, h('span', { class: 'n' }, selection.size), 'selected'),
      b('Start', 'play', () => act('start', ids())),
      b('Stop', 'stop', () => act('stop', ids())),
      b('Restart', 'restart', () => act('restart', ids())),
      menuButton([icon('settings'), 'Change', icon('chevron-down')], () => [
        { label: 'Rendering…', icon: 'layers', run: () => bulkSettings(ids(), selectedRows(), 'Rendering', ['Rendering and screen'], reload) },
        { label: 'Network…', icon: 'network', run: () => bulkSettings(ids(), selectedRows(), 'Network', ['Network'], reload) },
        { label: 'Tags…', icon: 'tag', run: () => bulkTags(selectedRows(), reload) },
        { label: 'All settings…', icon: 'settings', run: () => bulkSettings(ids(), selectedRows(), 'Settings', null, reload) },
        'sep',
        { label: 'Recreate', icon: 'recreate', run: () => act('recreate', ids()) },
      ], { label: 'Change the selection' }),
      h('a', { class: 'btn', href: `#/control/${selectorOf(selection)}`, dataset: { focus: 'Control' } }, icon('control'), 'Control'),
      b('Delete', 'trash', () => act('delete', ids()), 'danger'),
      h('button', { type: 'button', class: 'btn ghost icon-only', 'aria-label': 'Clear the selection', title: 'Clear the selection', dataset: { focus: 'clear' }, onclick: () => table.setSelection([]) }, icon('x')));
    if (keepFocus) {
      const again = [...bulk.querySelectorAll('[data-focus]')].find((node) => node.dataset.focus === keepFocus);
      if (again) again.focus({ preventScroll: true });
    }
    if (loaded && body.firstChild === grid) drawGrid(visible());
  });
  scope.add(() => rememberSelection([]));

  let typing = 0;
  search.addEventListener('input', () => {
    clearTimeout(typing);
    typing = setTimeout(() => { filters.q = search.value.trim().toLowerCase(); draw(); }, 120);
  });
  scope.add(() => clearTimeout(typing));

  const tools = h('div', { class: 'fleet-tools', hidden: true },
    h('div', { class: 'toolbar' },
      h('span', { class: 'search' }, icon('search'), search), stateChips, h('span', { class: 'grow' }), shownCount, viewToggle),
    h('div', { class: 'toolbar' }, facetChips, h('span', { class: 'grow' }),
      selectorBox(table, () => instances),
      button('Select shown', () => table.setSelection(visible().map((i) => i.id)), 'small')));

  root.append(
    pageHead('Androids', 'Every Android of this farm. Filter, select any number of them and act on the selection at once.',
      button('New Android', () => newAndroid(reload), 'primary', 'plus')),
    tools, body, bulk);
  replace(body, h('div', { class: 'fleet', 'aria-hidden': 'true' }, Array.from({ length: 6 }, () => h('span', { class: 'skeleton block' }))));

  pageShortcut(scope, 'n', 'New Android', () => newAndroid(reload));
  pageShortcut(scope, 'v', 'Switch between cards and table', () => setView(viewGrid.getAttribute('aria-pressed') === 'true' ? 'table' : 'grid'));
  pageShortcut(scope, 'f', 'Filter by name, id or tag', () => search.focus());

  await scope.every(4000, reload);
  scope.on(window, 'sf:changed', () => reload().catch(() => {}));
  if (params[0] === 'new') {
    history.replaceState(null, '', '#/androids');      // so a reload does not open the dialog again
    newAndroid(reload);
  }
}
