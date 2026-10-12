// The parts of Automation that are data: hand-written flows (YAML, validated by the
// server) and the values that programs and flows type.

import { del, get, post, put } from '../api.js';
import { badge, button, card, empty, field, loadInstances, option } from '../common.js';
import { confirmDialog, openDialog } from '../components/dialog.js';
import { dataTable } from '../components/table.js';
import { attempt, ok } from '../components/toast.js';
import { h, replace, when } from '../dom.js';

const API = '/api/automation';
const flowPath = (name) => `${API}/flows/${name.split('/').map(encodeURIComponent).join('/')}`;

const TEMPLATE = `# A flow is a list of steps run on each selected Android on its own.
description: What this flow does
package: com.example.app
steps:
  - launch: com.example.app
  - wait_for: {text: "Sign in"}
  - tap: {text: "Sign in"}
`;

// "name=value" lines -> { name: value } for a run's variables.
export function parseVariables(text) {
  const out = {};
  for (const line of text.split('\n').map((l) => l.trim()).filter(Boolean)) {
    const at = line.indexOf('=');
    if (at < 1) throw new Error(`"${line}" is not name=value`);
    out[line.slice(0, at).trim()] = line.slice(at + 1);
  }
  return out;
}

// Start a program or flow on a selection. `names` are the values it types ({name}).
export async function runDialog(kind, name, names = []) {
  const selector = h('input', { type: 'text', value: 'running', spellcheck: false });
  const variables = h('textarea', { rows: 3, spellcheck: false, placeholder: 'name=value, one per line', value: names.map((n) => `${n}=`).join('\n') });
  const dry = h('input', { type: 'checkbox' });
  await openDialog({
    title: `Run ${kind} "${name}"`,
    body: [
      field('Androids (selector)', selector),
      field('Values for this run only', variables, 'Kept in memory, never written to disk. Lines left empty, and values not listed, come from the Values tab (per Android, shared, pools).'),
      field('Dry run: read the screens and report what would be done, touch nothing', dry),
    ],
    actions: [{ label: 'Run', kind: 'primary', run: async () => {
      const given = Object.fromEntries(Object.entries(parseVariables(variables.value)).filter(([, value]) => value !== ''));
      await post(`${API}/runs`, { kind, name, selector: selector.value.trim(), variables: given, dry_run: dry.checked });
      location.hash = '#/automation/runs';
    } }],
  });
}

// -------------------------------------------------------------------- flows
export async function flowsTab(panel) {
  const list = h('div', { class: 'pick-list', role: 'group', 'aria-label': 'Flows' });
  const name = h('input', { type: 'text', spellcheck: false, placeholder: 'my-flow', maxLength: 80 });
  const text = h('textarea', { class: 'code', spellcheck: false, 'aria-label': 'Flow (YAML)', wrap: 'off' });
  const verdict = h('div', { role: 'status' });
  let flows = [];

  const say = (kind, title, lines = []) => replace(verdict, h('div', { class: `banner ${kind}` },
    h('div', { class: 'grow' }, h('strong', null, title), lines.length ? h('ol', { class: 'steps small' }, lines.map((line) => h('li', null, line))) : null)));

  async function load() {
    flows = (await get(`${API}/flows`)).flows;
    replace(list, flows.length === 0 ? h('p', { class: 'muted' }, 'No flows yet.')
      : flows.map((flow) => h('button', { type: 'button', class: 'btn', 'aria-pressed': String(flow.name === name.value), onclick: () => open(flow.name) },
        flow.name, flow.builtin ? [' ', badge('', 'example')] : null, flow.error ? [' ', badge('fail', 'invalid')] : null)));
  }

  async function open(flowName) {
    const data = await attempt(() => get(flowPath(flowName)));
    if (!data) return;
    name.value = data.name;
    text.value = data.text;
    if (data.error) say('fail', 'This flow does not parse: ', [data.error]);
    else say('info', data.builtin ? 'A bundled example (read-only). Change the name to save your own copy.' : `${data.flow.steps} step(s).`, data.flow.outline);
    await load();
  }

  // The server is the only judge: the same parser that will run the flow checks it.
  const validate = () => attempt(async () => {
    try {
      const result = await post(`${API}/flows/validate`, { text: text.value, name: name.value.trim() || null });
      say('ok', `Valid: ${result.flow.steps} step(s).`, result.flow.outline);
      return true;
    } catch (error) {
      if (error.status !== 400 && error.status !== 422) throw error;
      say('fail', 'Not valid: ', [error.message]);
      return false;
    }
  });

  const save = () => attempt(async () => {
    if (!name.value.trim()) { name.focus(); throw new Error('Give the flow a name.'); }
    try {
      const result = await put(flowPath(name.value.trim()), { text: text.value });
      say('ok', `Saved: ${result.flow.steps} step(s).`, result.flow.outline);
      ok(`Flow "${name.value.trim()}" saved.`);
      await load();
    } catch (error) {
      if (error.status !== 400 && error.status !== 422) throw error;
      say('fail', 'Not saved, the flow is not valid: ', [error.message]);
    }
  });

  panel.append(h('div', { class: 'split' },
    card('Flows', list, h('div', { class: 'row' }, button('New flow', () => { name.value = ''; text.value = TEMPLATE; replace(verdict); name.focus(); load(); }, 'small'))),
    h('div', { class: 'stack' },
      h('div', { class: 'toolbar' }, field('Name', name),
        button('Validate', validate), button('Save', save, 'primary'),
        button('Run…', () => {
          const flow = flows.find((f) => f.name === name.value.trim());
          if (!flow) { ok('Save the flow first, then run it.'); return; }
          runDialog('flow', flow.name, Object.keys(flow.variables || {}));
        }),
        button('Delete', async () => {
          const target = name.value.trim();
          if (!target || !await confirmDialog({ title: 'Delete this flow?', message: `"${target}" is removed.`, confirm: 'Delete', danger: true })) return;
          if (await attempt(() => del(flowPath(target)), 'Flow deleted.')) { name.value = ''; text.value = ''; replace(verdict); await load(); }
        }, 'danger')),
      text, verdict,
      h('p', { class: 'small muted' }, 'Steps: launch, stop_app, tap, long_press, type, clear, key, back, home, swipe, scroll_to, wait_for, wait_gone, assert, sleep, if, repeat, set, call, screenshot, fail. Targets are matched on the live screen by id, text or description, never by fixed coordinates. Bundled examples show each step.'))));
  text.value = TEMPLATE;
  await load();
}

// ------------------------------------------------------------------- values
export async function valuesTab(panel) {
  const sharedTable = dataTable({
    label: 'Shared values',
    key: (v) => v.name,
    columns: [
      { title: 'Name', cell: (v) => h('code', null, v.name) },
      { title: 'Value', cell: (v) => (v.hidden ? h('span', { class: 'muted' }, 'stored, never shown') : v.value) },
      { title: '', class: 'actions', cell: (v) => button('Delete', () => attempt(() => del(`${API}/values/shared/${encodeURIComponent(v.name)}`), 'Value deleted.').then(load), 'small danger') },
    ],
    empty: empty('No shared values', 'A shared value is typed wherever a step says {name}; it is never used up.'),
  });
  const poolTable = dataTable({
    label: 'Pools',
    key: (p) => p.name,
    columns: [
      { title: 'Pool', cell: (p) => h('code', null, p.name) },
      { title: 'Lines left', class: 'num', cell: (p) => p.remaining },
      { title: 'Used', class: 'num', cell: (p) => p.used },
      { title: '', class: 'actions', cell: (p) => [
        button('History', async () => {
          const data = await attempt(() => get(`${API}/values/pools/${encodeURIComponent(p.name)}`));
          if (data) openDialog({ title: `Pool "${p.name}": ${data.remaining} left`, body: data.used.length === 0 ? h('p', { class: 'muted' }, 'Nothing used yet.')
            : h('ul', { class: 'small' }, data.used.map((u) => h('li', null, h('code', null, u.value), ` · ${when(u.at)}`, u.instance != null ? ` · Android ${u.instance}` : '', u.run ? ` · run ${u.run}` : ''))) });
        }, 'small'), ' ',
        button('Delete', async () => {
          if (!await confirmDialog({ title: 'Delete this pool?', message: `"${p.name}" and its ${p.remaining} unused line(s) are removed.`, confirm: 'Delete', danger: true })) return;
          await attempt(() => del(`${API}/values/pools/${encodeURIComponent(p.name)}`), 'Pool deleted.');
          await load();
        }, 'small danger'),
      ] },
    ],
    empty: empty('No pools', 'A pool is a list of lines; each use takes one line and it is never used again (for example one account per Android).'),
  });
  const own = h('div');
  const pick = h('select', { 'aria-label': 'Android' });
  const sharedName = h('input', { type: 'text', spellcheck: false });
  const sharedValue = h('input', { type: 'text', spellcheck: false });
  const poolName = h('input', { type: 'text', spellcheck: false });
  const poolLines = h('textarea', { rows: 4, spellcheck: false, placeholder: 'one value per line' });
  const ownName = h('input', { type: 'text', spellcheck: false, 'aria-label': 'Value name' });
  const ownValue = h('input', { type: 'text', spellcheck: false, 'aria-label': 'Value' });

  async function load() {
    const data = await get(`${API}/values`);
    sharedTable.update([...Object.entries(data.shared.values).map(([name, value]) => ({ name, value })),
      ...data.shared.hidden.map((name) => ({ name, hidden: true }))]);
    poolTable.update(data.pools);
  }

  async function loadOwn() {
    if (!pick.value) { replace(own, h('p', { class: 'muted' }, 'No Androids yet.')); return; }
    const data = await get(`${API}/values/instances/${pick.value}`);
    const entries = Object.entries(data.values);
    replace(own, entries.length === 0 && data.hidden.length === 0 ? h('p', { class: 'muted' }, 'This Android has no values of its own; it uses the shared ones.')
      : h('ul', null, entries.map(([name, value]) => h('li', null, h('code', null, name), ` = ${value} `,
        button('Delete', () => attempt(() => del(`${API}/values/instances/${pick.value}/${encodeURIComponent(name)}`), 'Value deleted.').then(loadOwn), 'small danger'))),
      data.hidden.map((name) => h('li', null, h('code', null, name), ' stored, never shown'))));
  }
  pick.addEventListener('change', () => loadOwn().catch(() => {}));

  panel.append(h('div', { class: 'stack' },
    h('p', { class: 'muted' }, 'When a step types {name}, the value is looked up for that Android first, then in the shared values, then taken from a pool of that name. Names that look like secrets (password, pin, otp …) are never stored: give those when starting a run.'),
    h('div', { class: 'grid cols-2' },
      card('Shared values',
        h('form', { class: 'toolbar', onsubmit: (event) => {
          event.preventDefault();
          attempt(async () => {
            await put(`${API}/values/shared/${encodeURIComponent(sharedName.value.trim())}`, { value: sharedValue.value });
            sharedName.value = '';
            sharedValue.value = '';
            await load();
          }, 'Value stored.');
        } }, field('Name', sharedName), field('Value', sharedValue), h('button', { type: 'submit', class: 'btn' }, 'Store')),
        sharedTable.el),
      card('Pools',
        h('form', { class: 'stack', onsubmit: (event) => {
          event.preventDefault();
          attempt(async () => {
            const lines = poolLines.value.split('\n').map((l) => l.trim()).filter(Boolean);
            if (!lines.length) throw new Error('Paste at least one line.');
            const result = await post(`${API}/values/pools/${encodeURIComponent(poolName.value.trim())}`, { lines });
            poolLines.value = '';
            await load();
            return result;
          }, (result) => `Added ${result.added} line(s); ${result.remaining} left in the pool.`);
        } }, field('Pool name', poolName), field('Lines to add', poolLines), h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Add to pool'))),
        poolTable.el)),
    card('Values of one Android',
      h('form', { class: 'toolbar', onsubmit: (event) => {
        event.preventDefault();
        attempt(async () => {
          if (!pick.value) throw new Error('Create an Android first.');
          await put(`${API}/values/instances/${pick.value}/${encodeURIComponent(ownName.value.trim())}`, { value: ownValue.value });
          ownName.value = '';
          ownValue.value = '';
          await loadOwn();
        }, 'Value stored.');
      } }, pick, ownName, ownValue, h('button', { type: 'submit', class: 'btn' }, 'Store')),
      own)));

  const instances = await loadInstances();
  replace(pick, instances.map((i) => option(String(i.id), `${i.id} · ${i.name}`)));
  ownName.placeholder = 'name';
  ownValue.placeholder = 'value';
  await Promise.all([load(), loadOwn()]);
}
