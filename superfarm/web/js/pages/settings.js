// Settings: generated from the config schema the daemon publishes, so every option is
// here with its description and none is described twice. The server validates.

import { get, patch } from '../api.js';
import { button, pageHead } from '../common.js';
import { schemaForm } from '../components/form.js';
import { tabs } from '../components/tabs.js';
import { fail, ok, warn } from '../components/toast.js';
import { applyFieldErrors, clearFieldErrors, focusField } from '../components/validation.js';
import { h, replace } from '../dom.js';

export async function render(root, params, scope) {
  const [schema, current] = await Promise.all([get('/api/config/schema'), get('/api/config')]);
  const body = h('div');
  const pending = h('span', { class: 'muted', role: 'status' });
  let form = null;
  let view = null;
  let active = params[0];

  function build(config) {
    form = schemaForm(schema, config);
    // A section taken off the page keeps what was typed into it (its inputs live on in
    // the form object), so edits survive switching tabs and one Save covers them all.
    view = tabs(form.sections.map((section) => ({
      id: section.name, label: section.title,
      render: (panel) => {
        panel.append(h('div', { class: 'stack' },
          h('div', null, h('h2', null, section.title), section.description ? h('p', { class: 'muted' }, section.description) : null),
          section.el));
        mark();
      },
    })), { active, parent: scope, vertical: true, label: 'Settings sections', onChange: (id) => { active = id; history.replaceState(null, '', `#/settings/${id}`); } });
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
    pending.className = changed.length ? 'badge warn' : 'small muted';
    pending.textContent = changed.length ? `${changed.length} unsaved change(s): ${changed.slice(0, 4).join(', ')}${changed.length > 4 ? '\u2026' : ''}` : 'No unsaved changes.';
  }
  body.addEventListener('input', mark);

  const save = async () => {
    const sections = form.sections.map((section) => section.el);
    clearFieldErrors(sections);
    try {
      const changes = form.changes();
      if (!changes) throw new Error('Nothing was changed.');
      const result = await patch('/api/config', { patch: changes });
      ok(`Saved ${result.changed.length} setting(s) to config.yaml.`);
      if (result.restart_android.length) warn(`Restart running Androids to apply: ${result.restart_android.join(', ')}`, { timeout: 0 });
      if (result.restart_daemon.length) warn(`Restart the daemon (sf serve) to apply: ${result.restart_daemon.join(', ')}`, { timeout: 0 });
      build(result.config);
    } catch (error) {
      // What the server refused is marked on the field itself, on whichever tab it is.
      const marked = applyFieldErrors(error, sections);
      if (marked.length) {
        const section = form.sections.find((candidate) => candidate.el.contains(marked[0]));
        if (section && section.name !== active) { active = section.name; view.show(section.name); }
        focusField(marked[0]);
        fail(new Error(`Not saved: ${marked.length} setting(s) were refused. ${error.message}`));
      } else {
        fail(error);
      }
    }
  };

  root.append(
    pageHead('Settings', 'Farm-wide defaults. Most can be overridden per Android. Saved to config.yaml in the farm home.',
      pending, button('Discard changes', async () => build((await get('/api/config')).config)), button('Save', save, 'primary', 'check')),
    body);
  build(current.config);
}
