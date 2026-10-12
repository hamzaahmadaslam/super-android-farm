// Apps: the farm's library (any APK, XAPK, APKS or APKM), actions on any selection, and
// the version matrix. Nothing here knows any particular app.

import { del, get, post, upload } from '../api.js';
import { button, card, empty, field, loadInstances, pageHead } from '../common.js';
import { confirmDialog } from '../components/dialog.js';
import { dataTable } from '../components/table.js';
import { finished } from '../components/tasks.js';
import { attempt, ok } from '../components/toast.js';
import { bytes, h, parseSelector, replace, when } from '../dom.js';

export async function render(root, _params, scope) {
  let apps = [];
  let instances = [];
  const selector = h('input', { type: 'text', value: 'running', spellcheck: false, placeholder: 'all, running, tag:name, 1-10' });
  const count = h('span', { class: 'small muted' });
  const file = h('input', { type: 'file', accept: '.apk,.xapk,.apks,.apkm', multiple: true, 'aria-label': 'App files to upload' });
  const pkg = h('input', { type: 'text', placeholder: 'com.example.app', spellcheck: false });
  const matrixBox = h('div');

  const describe = () => {
    try {
      count.textContent = `${parseSelector(selector.value, instances).size} Android(s) match`;
    } catch (error) {
      count.textContent = error.message;
    }
  };
  selector.addEventListener('input', describe);

  // Every action follows its task to the end, then the matrix is read again.
  const run = (action, body, message) => attempt(async () => {
    if (!selector.value.trim()) throw new Error('Type a selector first (for example: running).');
    const data = await post(`/api/apps/${action}`, { selector: selector.value.trim(), ...body });
    await finished(data.task);
    await matrix();
  }, message);

  const table = dataTable({
    label: 'App library',
    key: (app) => app.id,
    columns: [
      { title: 'App', cell: (app) => [h('div', null, app.label), h('code', null, app.package)] },
      { title: 'Version', cell: (app) => `${app.version_name || ''} (${app.version_code})` },
      { title: 'Format', cell: (app) => `${app.format}${app.splits && app.splits.length ? `, ${app.splits.length} splits` : ''}` },
      { title: 'CPU', cell: (app) => (app.abis || []).join(', ') || 'any' },
      { title: 'Size', class: 'num', cell: (app) => bytes(app.size) },
      { title: 'Added', cell: (app) => when(app.added) },
      { title: '', class: 'actions', cell: (app) => [
        button('Install', () => run('install', { app_id: app.id }), 'small primary'), ' ',
        button('Ensure', () => run('ensure', { app_id: app.id }), 'small'), ' ',
        button('Launch', () => run('launch', { package: app.package }), 'small'), ' ',
        button('Stop', () => run('stop', { package: app.package }), 'small'), ' ',
        button('Uninstall', async () => {
          if (await confirmDialog({ title: 'Uninstall from the selection?', message: `${app.package} and its data are removed from every Android matching "${selector.value}".`, confirm: 'Uninstall', danger: true })) run('uninstall', { package: app.package });
        }, 'small danger'), ' ',
        button('Remove file', async () => {
          if (await confirmDialog({ title: 'Remove from the library?', message: `${app.label} ${app.version_name} is deleted from the library. Androids that have it installed keep it.`, confirm: 'Remove', danger: true })) {
            await attempt(() => del(`/api/apps/library/${encodeURIComponent(app.id)}`), 'Removed from the library.');
            await load();
          }
        }, 'small'),
      ] },
    ],
    empty: empty('The library is empty', 'Upload an APK, XAPK, APKS or APKM file above. Split bundles are unpacked and the right splits are chosen per device.'),
  });

  async function load() {
    apps = (await get('/api/apps/library')).apps;
    table.update(apps);
  }

  async function matrix() {
    // The API refuses a selector that matches nothing; that is an empty matrix, not an error.
    if (parseSelector(selector.value.trim() || 'all', instances).size === 0) {
      replace(matrixBox, empty('No Android matches this selector', 'The matrix shows which version of each app is on each selected Android. Try "all", or start some Androids for "running".'));
      return;
    }
    replace(matrixBox, h('p', { class: 'muted' }, 'Reading the devices…'));
    const data = await get('/api/apps/matrix', { selector: selector.value.trim() || 'all', detail: true });
    const packages = Object.entries(data.packages);
    const newest = Object.fromEntries(apps.map((app) => [app.package, app]).reverse());
    replace(matrixBox,
      Object.keys(data.errors).length > 0 && h('div', { class: 'banner warn' }, `Could not read ${Object.keys(data.errors).length} Android(s): ${Object.entries(data.errors).map(([id, why]) => `${id} (${why})`).join('; ')}`),
      packages.length === 0 ? empty('No third-party apps installed', 'Install one from the library above.')
        : h('div', { class: 'table-wrap' }, h('table', { class: 'matrix', 'aria-label': 'App versions per Android' },
          h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Package'), data.instances.map((id) => h('th', { scope: 'col' }, h('a', { href: `#/android/${id}/apps` }, id))))),
          h('tbody', null, packages.map(([name, cells]) => h('tr', null,
            h('th', { scope: 'row' }, h('code', null, name)),
            data.instances.map((id) => {
              const cell = cells[id];
              if (!cell) return h('td', { class: 'muted' }, '—');
              const wanted = newest[name];
              const behind = wanted && cell.version_code < wanted.version_code;
              return h('td', { class: behind ? 'warn' : '', title: behind ? `the library has ${wanted.version_name}` : null },
                cell.version || cell.version_code, cell.enabled ? '' : ' (disabled)');
            })))))));
  }

  root.append(
    pageHead('Apps', 'Upload any Android app once, then install, update or remove it on any selection.'),
    h('div', { class: 'stack' },
      card('Selection',
        h('div', { class: 'row' }, field('Androids to act on (selector)', selector), count),
        h('p', { class: 'small muted' }, 'Install puts the chosen version on every match; Ensure only touches Androids where it is missing or older.')),
      card('Library',
        h('form', { class: 'toolbar', onsubmit: (event) => {
          event.preventDefault();
          attempt(async () => {
            if (!file.files.length) throw new Error('Choose one or more app files first.');
            for (const one of file.files) {
              const data = await upload('/api/apps/library', one);
              ok(`Added ${data.app.label} ${data.app.version_name}.${(data.app.warnings || []).length ? ` Note: ${data.app.warnings.join('; ')}` : ''}`);
            }
            file.value = '';
            await load();
          });
        } }, file, h('button', { type: 'submit', class: 'btn primary' }, 'Upload')),
        table.el),
      card('Any installed package',
        h('p', { class: 'small muted' }, 'For apps that are on the devices but not in the library.'),
        h('div', { class: 'toolbar' }, field('Package name', pkg),
          ['launch', 'stop', 'clear', 'uninstall'].map((action) => button(action[0].toUpperCase() + action.slice(1), async () => {
            if (!pkg.value.trim()) { pkg.focus(); return; }
            const risky = action === 'clear' || action === 'uninstall';
            if (risky && !await confirmDialog({ title: `${action === 'clear' ? 'Clear the data of' : 'Uninstall'} ${pkg.value}?`, message: `On every Android matching "${selector.value}".`, confirm: action === 'clear' ? 'Clear data' : 'Uninstall', danger: true })) return;
            run(action, { package: pkg.value.trim() });
          }, action === 'uninstall' ? 'danger' : '')))),
      h('section', { class: 'card' },
        h('div', { class: 'card-head' }, h('h2', null, 'Version matrix'), button('Refresh', () => attempt(matrix), 'small')),
        matrixBox)));

  [instances] = await Promise.all([loadInstances(), load()]);
  describe();
  attempt(matrix);
  scope.on(window, 'sf:changed', () => loadInstances().then((rows) => { instances = rows; describe(); }).catch(() => {}));
}
