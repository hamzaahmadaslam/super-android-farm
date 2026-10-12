// Automation for any app: record on the live view, train a program from several
// recordings, write flows by hand, keep the values they type, run on any selection and
// answer devices that need attention.

import { del, get, patch, post } from '../api.js';
import { badge, button, card, empty, field, loadInstances, option, pageHead } from '../common.js';
import { confirmDialog, openDialog } from '../components/dialog.js';
import { liveView } from '../components/live.js';
import { dataTable } from '../components/table.js';
import { tabs } from '../components/tabs.js';
import { finished } from '../components/tasks.js';
import { attempt, fail, ok } from '../components/toast.js';
import { duration, h, replace, scope as newScope, when } from '../dom.js';
import { flowsTab, runDialog, valuesTab } from './automation-data.js';

const API = '/api/automation';
const RUNNING = new Set(['running', 'booted']);

// ------------------------------------------------------------------- record
function recordTab(panel, scope) {
  const datasets = dataTable({
    label: 'Datasets',
    key: (d) => d.slug,
    columns: [
      { title: 'Dataset', cell: (d) => [h('strong', null, d.name), d.description ? h('div', { class: 'small muted' }, d.description) : null] },
      { title: 'App', cell: (d) => (d.package ? h('code', null, d.package) : 'any') },
      { title: 'Recordings', class: 'num', cell: (d) => d.recordings },
      { title: 'Steps', class: 'num', cell: (d) => d.steps },
      { title: 'Program', cell: (d) => (d.trained ? badge('ok', 'trained') : badge('', 'not trained')) },
      { title: '', class: 'actions', cell: (d) => [
        button('Recordings', () => showDataset(d.slug), 'small'), ' ',
        button('Train', () => attempt(async () => {
          const data = await post(`${API}/datasets/${encodeURIComponent(d.slug)}/train`);
          const task = await finished(data.task);
          if (task.state === 'done') ok(`Trained "${d.name}": ${task.output.steps} step(s).${(task.output.warnings || []).length ? ` Warnings: ${task.output.warnings.join('; ')}` : ''}`);
          await load();
        }), 'small primary'), ' ',
        button('Delete', async () => {
          if (!await confirmDialog({ title: 'Delete this dataset?', message: `"${d.name}" and its ${d.recordings} recording(s) are removed. A trained program stays until deleted on the Programs tab.`, confirm: 'Delete', danger: true })) return;
          await attempt(() => del(`${API}/datasets/${encodeURIComponent(d.slug)}`), 'Dataset deleted.');
          await load();
        }, 'small danger'),
      ] },
    ],
    empty: empty('No datasets yet', 'A dataset collects recordings of one task, for example "log in". Create one, record the task two or three times, then train.'),
  });
  const name = h('input', { type: 'text', maxLength: 120 });
  const pkg = h('input', { type: 'text', placeholder: 'optional: com.example.app', spellcheck: false });
  const pickDataset = h('select', { 'aria-label': 'Dataset to record into' });
  const pickDevice = h('select', { 'aria-label': 'Android to record on' });
  const stage = h('div');
  let list = [];

  async function load() {
    list = (await get(`${API}/datasets`)).datasets;
    datasets.update(list);
    const keep = pickDataset.value;
    replace(pickDataset, list.map((d) => option(d.slug, d.name)));
    if (keep) pickDataset.value = keep;
  }

  async function showDataset(slug) {
    const data = (await get(`${API}/datasets/${encodeURIComponent(slug)}`)).dataset;
    const body = h('div', { class: 'stack' });
    const draw = (recordings) => replace(body, recordings.length === 0 ? h('p', { class: 'muted' }, 'No recordings yet.')
      : recordings.map((r) => h('div', { class: 'task' },
        h('div', { class: 'task-head' }, h('strong', null, `${r.id} · ${r.steps} step(s), ${duration(r.seconds)}`),
          h('span', { class: 'small muted' }, `Android ${r.instance ?? '?'} · ${when(r.created)}`),
          button('Delete', async () => {
            await attempt(() => del(`${API}/datasets/${encodeURIComponent(slug)}/recordings/${encodeURIComponent(r.id)}`), 'Recording deleted.');
            draw((await get(`${API}/datasets/${encodeURIComponent(slug)}`)).dataset.recordings_list);
            load().catch(() => {});
          }, 'small danger')),
        h('ol', { class: 'steps small' }, r.preview.map((line) => h('li', null, line))),
        r.steps > r.preview.length ? h('span', { class: 'small muted' }, `… and ${r.steps - r.preview.length} more`) : null)));
    draw(data.recordings_list);
    await openDialog({ title: `Recordings of "${data.name}"`, wide: true, body });
  }

  // One recording session on screen: the live view sends every gesture to the recorder,
  // which reads the screen first, notes the element under the point, then performs it.
  function session(id, instance) {
    const status = h('div', { class: 'stack' });
    const expect = h('input', { type: 'checkbox' });
    // The view gets a scope of its own so its stream ends with the recording, not only
    // when the tab is left.
    const own = newScope();
    scope.add(() => own.dispose());
    const base = `${API}/record/${id}`;
    const refresh = async () => {
      const s = await get(base);
      replace(status,
        h('p', null, badge(s.active ? 'info' : '', s.active ? 'recording' : 'stopped'), ` ${s.steps} step(s), ${duration(s.seconds)} into "${s.dataset}"`),
        s.last_problem && h('div', { class: 'banner warn' }, s.last_problem),
        h('ol', { class: 'steps small', start: Math.max(1, s.steps - s.preview.length + 1) }, s.preview.map((line) => h('li', null, line))));
    };
    const record = (action, body) => post(`${base}/${action}`, body).then(refresh, (error) => { fail(error); });
    const send = (kind, payload) => {
      if (kind === 'tap' && expect.checked) { expect.checked = false; return record('assert-visible', payload); }
      return record(kind, payload);
    };
    const ask = async (title, label, placeholder, make) => {
      const input = h('input', { type: 'text', placeholder, spellcheck: false });
      await openDialog({ title, body: field(label, input), actions: [{ label: 'Record', kind: 'primary', run: () => record(...make(input.value.trim())) }] });
    };
    const end = (action, message) => attempt(async () => {
      const result = await post(`${base}/${action}`);
      own.dispose();
      replace(stage, startForm());
      await load();
      return result;
    }, (result) => result.summary || message);
    replace(stage, h('div', { class: 'split' },
      liveView(id, own, { send, label: instance ? instance.name : `Android ${id}`, aspect: instance && instance.screen && [instance.screen.width, instance.screen.height] }),
      h('div', { class: 'stack' },
        h('p', { class: 'muted' }, 'Do the task on the screen to the left. Each tap is recorded with the element it landed on, so the program finds that element again on any device.'),
        h('div', { class: 'row' },
          button('Type a named value…', () => ask('Type a value by name', 'Name of the value', 'username', (v) => ['text', { name: v }])),
          button('Launch app…', () => ask('Launch an app', 'Package name', 'com.example.app', (v) => ['launch', { package: v }])),
          button('Wait…', () => ask('Wait', 'Seconds', '2', (v) => ['wait', { seconds: Number(v) }])),
          h('label', null, expect, ' Next tap only checks the element is visible'),
          button('Undo last step', () => record('undo'))),
        h('div', { class: 'row' },
          button('Stop and save', () => end('stop', 'Recording saved.'), 'primary'),
          button('Discard', async () => {
            if (await confirmDialog({ title: 'Discard this recording?', message: 'The steps recorded so far are thrown away.', confirm: 'Discard', danger: true })) end('cancel', 'Recording discarded.');
          }, 'danger')),
        status)));
    refresh().catch(() => {});
  }

  function startForm() {
    return h('form', { class: 'toolbar', onsubmit: (event) => {
      event.preventDefault();
      attempt(async () => {
        if (!pickDataset.value) throw new Error('Create a dataset first.');
        if (!pickDevice.value) throw new Error('Start an Android first: recording needs a running device.');
        const id = Number(pickDevice.value);
        await post(`${API}/record/start`, { dataset: pickDataset.value, id });
        session(id, instances.find((i) => i.id === id));
      });
    } }, pickDataset, pickDevice, h('button', { type: 'submit', class: 'btn primary' }, 'Start recording'));
  }

  let instances = [];
  panel.append(h('div', { class: 'stack' },
    card('Record', stage),
    h('section', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', null, 'Datasets')),
      h('form', { class: 'toolbar', onsubmit: (event) => {
        event.preventDefault();
        attempt(async () => {
          await post(`${API}/datasets`, { name: name.value.trim(), package: pkg.value.trim() });
          name.value = '';
          await load();
        }, 'Dataset created.');
      } }, field('New dataset', name), field('App', pkg), h('button', { type: 'submit', class: 'btn' }, 'Create')),
      datasets.el)));

  return (async () => {
    [instances] = await Promise.all([loadInstances(), load()]);
    replace(pickDevice, instances.filter((i) => RUNNING.has(i.state)).map((i) => option(String(i.id), `${i.id} · ${i.name}`)));
    // A recording left open (a reloaded page) is picked up again rather than orphaned.
    const open = (await get(`${API}/record`)).recordings.find((r) => r.active);
    if (open) session(open.instance, instances.find((i) => i.id === open.instance));
    else replace(stage, startForm());
  })();
}

// ----------------------------------------------------------------- programs
async function programsTab(panel) {
  const table = dataTable({
    label: 'Trained programs',
    key: (p) => p.slug,
    columns: [
      { title: 'Program', cell: (p) => [h('strong', null, p.name), p.goal ? h('div', { class: 'small muted' }, p.goal) : null] },
      { title: 'App', cell: (p) => (p.package ? h('code', null, p.package) : 'any') },
      { title: 'Steps', class: 'num', cell: (p) => `${p.steps}${p.optional ? ` (${p.optional} optional)` : ''}` },
      { title: 'Values typed', cell: (p) => (p.dynamic || []).join(', ') },
      { title: 'Built', cell: (p) => [when(p.created), ` from ${p.from_recordings} recording(s)`, (p.warnings || []).length ? h('div', { class: 'small warn' }, p.warnings.join('; ')) : null] },
      { title: '', class: 'actions', cell: (p) => [
        button('Run…', () => runDialog('program', p.slug, p.dynamic || []), 'small primary'), ' ',
        button('Steps', async () => {
          const data = await attempt(() => get(`${API}/programs/${encodeURIComponent(p.slug)}`));
          if (data) openDialog({ title: `Steps of "${p.name}"`, wide: true, body: h('ol', { class: 'steps' }, data.program.outline.map((s) => h('li', null, s.label, s.optional ? ' (optional)' : '', s.has_selector ? '' : [' ', badge('warn', 'by position only')]))) });
        }, 'small'), ' ',
        button('Delete', async () => {
          if (!await confirmDialog({ title: 'Delete this program?', message: `"${p.name}" is removed. Its dataset and recordings stay; training builds it again.`, confirm: 'Delete', danger: true })) return;
          await attempt(() => del(`${API}/programs/${encodeURIComponent(p.slug)}`), 'Program deleted.');
          await load();
        }, 'small danger'),
      ] },
    ],
    empty: empty('No programs yet', 'A program is trained from a dataset’s recordings: record a task a few times on the Record tab, then press Train.'),
  });
  const model = h('p', { class: 'small muted' });
  const load = async () => table.update((await get(`${API}/programs`)).programs);
  panel.append(h('div', { class: 'stack' }, table.el, model));
  await load();
  const llm = await get(`${API}/llm`);
  model.textContent = `Model: ${llm.provider}${llm.ok ? '' : ' (not reachable)'}. ${llm.detail} Programs are built by comparing recordings; a model only describes steps.`;
}

// --------------------------------------------------------------------- runs
async function runsTab(panel, scope) {
  const box = h('div', { class: 'stack' });
  const kind = h('select', { 'aria-label': 'Kind' }, option('flow', 'Flow'), option('program', 'Program'));
  const target = h('select', { 'aria-label': 'What to run' });
  let names = { flow: [], program: [] };
  const fill = () => replace(target, names[kind.value].map((n) => option(n.id, n.label)));
  kind.addEventListener('change', fill);

  const resolve = (run, id, action) => attempt(() => post(`${API}/runs/${encodeURIComponent(run.id)}/resolve`, { id: Number(id), action }).then(load),
    { resume: 'Resumed.', skip: 'Step skipped.', abort: 'Stopped on that Android.' }[action]);

  function device(run, id, d) {
    return h('li', null,
      h('a', { href: `#/android/${id}` }, `Android ${id}`), ' ', badge(d.waiting ? 'needs_attention' : d.state, d.waiting ? 'needs attention' : d.state),
      d.steps ? ` step ${d.step} of ${d.steps}` : '', d.step_label ? ` · ${d.step_label}` : '',
      (d.error || d.message) ? h('div', { class: 'small muted' }, d.error || d.message) : null,
      d.waiting && h('div', { class: 'banner warn' },
        h('div', { class: 'grow' }, h('strong', null, `Waiting for you: ${d.waiting.what || 'unknown screen'}. `), d.waiting.reason || '',
          ' Open the Android, put it on the expected screen, then resume.'),
        button('Resume', () => resolve(run, id, 'resume'), 'small primary'),
        button('Skip this step', () => resolve(run, id, 'skip'), 'small'),
        button('Stop this Android', () => resolve(run, id, 'abort'), 'small danger')));
  }

  async function load() {
    const runs = (await get(`${API}/runs`)).runs;
    replace(box, runs.length === 0 ? empty('No runs yet', 'Choose a flow or program above and a selection to run it on.')
      : runs.map((run) => {
        const active = run.state === 'running' || run.state === 'queued';
        return h('div', { class: 'task' },
          h('div', { class: 'task-head' },
            h('strong', null, `${run.kind} "${run.name}"${run.dry_run ? ' (dry run)' : ''}`),
            badge(run.waiting.length ? 'needs_attention' : run.state, run.waiting.length ? `${run.waiting.length} need attention` : run.state),
            h('span', { class: 'small muted' }, when(run.created)),
            active && button('Cancel run', () => attempt(() => post(`${API}/runs/${encodeURIComponent(run.id)}/cancel`).then(load), 'Cancel requested.'), 'small danger')),
          h('div', { class: 'small muted' }, Object.entries(run.counts).map(([state, n]) => `${n} ${state}`).join(' · ')),
          h('ul', null, Object.entries(run.devices).map(([id, d]) => device(run, id, d))));
      }));
  }

  panel.append(h('div', { class: 'stack' },
    card('Start a run', h('form', { class: 'toolbar', onsubmit: (event) => {
      event.preventDefault();
      if (!target.value) { fail('There is nothing to run yet: write a flow or train a program.'); return; }
      const chosen = names[kind.value].find((n) => n.id === target.value);
      runDialog(kind.value, target.value, chosen ? chosen.values : []).then(load);
    } }, kind, target, h('button', { type: 'submit', class: 'btn primary' }, 'Run…'))),
    box));

  const [flows, programs] = await Promise.all([get(`${API}/flows`), get(`${API}/programs`)]);
  names = {
    flow: flows.flows.filter((f) => !f.error).map((f) => ({ id: f.name, label: f.name, values: Object.keys(f.variables || {}) })),
    program: programs.programs.map((p) => ({ id: p.slug, label: p.name, values: p.dynamic || [] })),
  };
  if (!names.flow.length && names.program.length) kind.value = 'program';
  fill();
  await scope.every(2000, load);
}

// ---------------------------------------------------------------- schedules
async function schedulesTab(panel) {
  const table = dataTable({
    label: 'Schedules',
    columns: [
      { title: 'Name', cell: (s) => s.name },
      { title: 'Runs', cell: (s) => `${s.kind} "${s.target}" on ${s.selector}${s.dry_run ? ' (dry run)' : ''}` },
      { title: 'When', cell: (s) => (s.cron ? h('code', null, s.cron) : `every ${duration(s.every)}`) },
      { title: 'Next', cell: (s) => (s.enabled ? when(s.next_run) : badge('', 'paused')) },
      { title: 'Last', cell: (s) => [when(s.last_run) || 'never', s.last_result ? h('div', { class: 'small muted' }, s.last_result) : null] },
      { title: '', class: 'actions', cell: (s) => [
        button(s.enabled ? 'Pause' : 'Resume', () => attempt(() => patch(`${API}/schedules/${encodeURIComponent(s.id)}`, { enabled: !s.enabled }).then(load)), 'small'), ' ',
        button('Delete', async () => {
          if (!await confirmDialog({ title: 'Delete this schedule?', message: `"${s.name}" will not run again.`, confirm: 'Delete', danger: true })) return;
          await attempt(() => del(`${API}/schedules/${encodeURIComponent(s.id)}`), 'Schedule deleted.');
          await load();
        }, 'small danger'),
      ] },
    ],
    empty: empty('No schedules', 'Run a flow or program regularly: every N minutes, or on a cron line.'),
  });
  const load = async () => table.update((await get(`${API}/schedules`)).schedules);
  const f = {
    name: h('input', { type: 'text', maxLength: 80 }),
    kind: h('select', null, option('flow', 'Flow'), option('program', 'Program')),
    target: h('input', { type: 'text', spellcheck: false, placeholder: 'flow or program name' }),
    selector: h('input', { type: 'text', value: 'running', spellcheck: false }),
    cron: h('input', { type: 'text', spellcheck: false, placeholder: '*/30 * * * *' }),
    every: h('input', { type: 'number', min: 1, placeholder: 'or minutes between runs' }),
  };
  panel.append(h('div', { class: 'stack' },
    card('Add a schedule', h('form', { class: 'fields', onsubmit: (event) => {
      event.preventDefault();
      attempt(async () => {
        await post(`${API}/schedules`, {
          name: f.name.value.trim(), kind: f.kind.value, target: f.target.value.trim(), selector: f.selector.value.trim(),
          cron: f.cron.value.trim(), every: f.cron.value.trim() ? 0 : Math.round(Number(f.every.value || 0) * 60),
        });
        f.name.value = '';
        await load();
      }, 'Schedule added.');
    } },
    field('Name', f.name), field('Kind', f.kind), field('Flow or program', f.target), field('Androids (selector)', f.selector),
    field('Cron line', f.cron, 'minute hour day-of-month month day-of-week'), field('Or every N minutes', f.every),
    h('div', { class: 'field' }, h('span', null, '\u00a0'), h('button', { type: 'submit', class: 'btn primary' }, 'Add')))),
    table.el));
  await load();
}

export async function render(root, params, scope) {
  const view = tabs([
    { id: 'record', label: 'Record & train', render: recordTab },
    { id: 'programs', label: 'Programs', render: programsTab },
    { id: 'flows', label: 'Flows', render: flowsTab },
    { id: 'values', label: 'Values & pools', render: valuesTab },
    { id: 'runs', label: 'Runs', render: runsTab },
    { id: 'schedules', label: 'Schedules', render: schedulesTab },
  ], { active: params[0] || 'record', parent: scope, onChange: (id) => history.replaceState(null, '', `#/automation/${id}`) });
  root.append(pageHead('Automation', 'Works with any app: nothing here is specific to one.'), view.el);
}
