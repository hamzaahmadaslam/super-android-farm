// Settings: generated from the config schema the daemon publishes, so every option is
// here with its description and none is described twice. The server validates.

import { get, patch } from '../api.js';
import { button, pageHead } from '../common.js';
import { schemaForm } from '../components/form.js';
import { tabs } from '../components/tabs.js';
import { attempt, ok, warn } from '../components/toast.js';
import { h, replace } from '../dom.js';

export async function render(root, params, scope) {
  const [schema, current] = await Promise.all([get('/api/config/schema'), get('/api/config')]);
  const body = h('div');
  const pending = h('span', { class: 'muted', role: 'status' });
  let form = null;
  let active = params[0];

  function build(config) {
    form = schemaForm(schema, config);
    // A section taken off the page keeps what was typed into it (its inputs live on in
    // the form object), so edits survive switching tabs and one Save covers them all.
    const view = tabs(form.sections.map((section) => ({
      id: section.name, label: section.title,
      render: (panel) => { panel.append(section.description ? h('p', { class: 'muted' }, section.description) : '', section.el); mark(); },
    })), { active, parent: scope, onChange: (id) => { active = id; history.replaceState(null, '', `#/settings/${id}`); } });
    replace(body, view.el);
    mark();
  }

  const at = (object, path) => path.split('.').reduce((node, key) => (node == null ? undefined : node[key]), object);

  function mark() {
    if (!form) return;
    let changes;
    try { changes = form.changes(); } catch (error) { pending.textContent = error.message; return; }
    const changed = form.paths.filter((path) => at(changes, path) !== undefined);
    for (const node of body.querySelectorAll('.field[data-path]')) node.classList.toggle('changed', changed.includes(node.dataset.path));
    pending.textContent = changed.length ? `${changed.length} unsaved change(s): ${changed.slice(0, 4).join(', ')}${changed.length > 4 ? '\u2026' : ''}` : 'No unsaved changes.';
  }
  body.addEventListener('input', mark);

  const save = () => attempt(async () => {
    const changes = form.changes();
    if (!changes) throw new Error('Nothing was changed.');
    const result = await patch('/api/config', { patch: changes });
    ok(`Saved ${result.changed.length} setting(s).`);
    if (result.restart_android.length) warn(`Restart running Androids to apply: ${result.restart_android.join(', ')}`, { timeout: 0 });
    if (result.restart_daemon.length) warn(`Restart the daemon (sf serve) to apply: ${result.restart_daemon.join(', ')}`, { timeout: 0 });
    build(result.config);
  });

  root.append(
    pageHead('Settings', 'Farm-wide defaults. Most can be overridden per Android. Saved to config.yaml in the farm home.',
      pending, button('Discard changes', async () => build((await get('/api/config')).config)), button('Save', save, 'primary')),
    body);
  build(current.config);
}
