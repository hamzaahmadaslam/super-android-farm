// AI Pilot: teach with a model once, run everywhere without one.
//
// The operator states a goal for an app. A model operates the app on the chosen
// Androids, every action is recorded, and only the runs a check on the real screen
// confirms are kept. Those are trained into an ordinary program that replays with no
// model. The page says what was measured, not more: a small model succeeds only some of
// the time, and a failed run costs time, not correctness.
//
// Everything a model wrote (its reason for each action) and everything from a device is
// shown as plain text.

import { get, post } from '../api.js';
import { badge, button, emptyState, field, linkButton, loadInstances, option, pageHead, panel, plural, skeleton } from '../common.js';
import { thumbnails } from '../components/live.js';
import { dataTable, selectorBox } from '../components/table.js';
import { finished } from '../components/tasks.js';
import { attempt, ok } from '../components/toast.js';
import { applyFieldErrors, focusField } from '../components/validation.js';
import { duration, h, replace, selectorOf, when } from '../dom.js';
import { icon } from '../icons.js';
import { go } from '../router.js';
import { runDialog } from './automation-data.js';

const API = '/api/ai/pilot';
const UP = new Set(['running', 'booted']);
const ACTIVE = new Set(['queued', 'running']);
const CHECKS = [
  ['', 'No check of my own'], ['text', 'Text on the screen'], ['id', 'Element id'], ['desc', 'Content description'],
  ['regex', 'Text matching a pattern'], ['package', 'App in front (package)'], ['activity', 'Activity in front'],
];
const STATE_WORD = {
  queued: 'queued', running: 'running', needs_attention: 'needs your attention', done: 'done: verified', unverified: 'unverified',
  confirmed: 'confirmed by you', failed: 'failed', cancelled: 'cancelled',
};
const stateBadge = (state) => badge(state, STATE_WORD[state] || state);
// A session is a task: "done" there means it ended, not that its runs were verified.
const sessionBadge = (state) => (state === 'done' ? badge('', 'ended') : badge(state, state));
const lines = (value) => value.split(/[\n,]/).map((part) => part.trim()).filter(Boolean);

// ------------------------------------------------------------------ start form
function startForm(instances, models, config, reload) {
  const running = instances.filter((i) => UP.has(i.state));
  const goal = h('textarea', { rows: 2, maxLength: 600, placeholder: 'Open the Date & time settings' });
  const pkg = h('input', { type: 'text', spellcheck: false, placeholder: 'com.example.app', maxLength: 200 });
  const installed = h('select', { 'aria-label': 'Pick the app from what is installed' }, option('', 'Pick from the first selected Android…'));
  const model = h('select', null, models.map((m) => option(m.name, `${m.name}${m.default ? ' (default)' : ''}${m.vision ? ', reads pictures' : ''}`)));
  const preferred = models.find((m) => m.default);
  if (preferred) model.value = preferred.name;
  const checkKind = h('select', null, CHECKS.map(([value, label]) => option(value, label)));
  const checkValue = h('input', { type: 'text', spellcheck: false, placeholder: 'Set time automatically', disabled: true });
  const runs = h('input', { type: 'number', min: 1, max: 10, value: 3 });
  const maxSteps = h('input', { type: 'number', min: 1, max: 200, placeholder: config.ai.pilot.max_steps });
  const allowed = h('input', { type: 'text', spellcheck: false, placeholder: (config.ai.pilot.allowed_packages || []).join(', ') });
  const dataset = h('input', { type: 'text', spellcheck: false, maxLength: 64, placeholder: 'named after the goal' });
  const valueRows = h('div', { class: 'stack tight' });
  const checkString = h('code', { role: 'status' });

  // The Androids to teach on: a selection the shared selector box can drive.
  const selection = new Set();
  const listeners = [];
  const chosen = {
    selection,
    setSelection(ids) {
      const up = new Set(running.map((i) => i.id));
      selection.clear();
      for (const id of ids) if (up.has(id)) selection.add(id);
      for (const fn of listeners) fn(selection);
    },
    onSelection(fn) { listeners.push(fn); },
  };
  const chips = h('div', { class: 'chips', role: 'group', 'aria-label': 'Running Androids' });
  const drawChips = () => replace(chips, running.length === 0 ? h('span', { class: 'muted' }, 'No Android is running. The Pilot needs a running Android to operate.')
    : running.map((i) => h('button', { type: 'button', class: 'chip', 'aria-pressed': String(selection.has(i.id)), onclick: () => {
      const next = new Set(selection);
      next[next.has(i.id) ? 'delete' : 'add'](i.id);
      chosen.setSelection(next);
    } }, h('span', { class: 'count' }, `#${i.id}`), i.name)));
  let appsFor = 0;
  async function loadApps() {
    const first = [...selection].sort((a, b) => a - b)[0];
    if (!first || first === appsFor) return;
    appsFor = first;
    try {
      const data = await get(`/api/instances/${first}/apps`, { third_party: false });
      replace(installed, option('', `Pick from Android ${first} (${data.apps.length} apps)…`),
        data.apps.map((app) => option(app.package, app.label ? `${app.label} (${app.package})` : app.package)));
    } catch {
      replace(installed, option('', `The apps of Android ${first} could not be read; type the package`));
    }
  }
  chosen.onSelection(() => { drawChips(); loadApps(); });
  installed.addEventListener('change', () => { if (installed.value) pkg.value = installed.value; });

  const success = () => (checkKind.value && checkValue.value.trim() ? `${checkKind.value}:${checkValue.value.trim()}` : null);
  const drawCheck = () => {
    checkValue.disabled = !checkKind.value;
    checkString.textContent = success() || 'none';
  };
  checkKind.addEventListener('change', drawCheck);
  checkValue.addEventListener('input', drawCheck);

  // Named values: the model is told the names, never the contents.
  function addValue() {
    const name = h('input', { type: 'text', spellcheck: false, placeholder: 'name', 'aria-label': 'Value name', maxLength: 70 });
    const value = h('input', { type: 'text', spellcheck: false, autocomplete: 'off', placeholder: 'value', 'aria-label': 'Value' });
    const secret = h('input', { type: 'checkbox', 'aria-label': 'Hide this value on the screen' });
    secret.addEventListener('change', () => { value.type = secret.checked ? 'password' : 'text'; });
    const row = h('div', { class: 'row value-row' }, name, value, h('label', { class: 'small' }, secret, ' secret'),
      h('button', { type: 'button', class: 'btn ghost icon-only small', 'aria-label': 'Remove this value', title: 'Remove', onclick: () => row.remove() }, icon('x')));
    row.read = () => [name.value.trim(), value.value];
    valueRows.append(row);
  }

  const start = async () => {
    const body = { goal: goal.value.trim(), package: pkg.value.trim(), selector: selectorOf(selection), runs: runs.value === '' ? 1 : Number(runs.value) };
    if (model.value) body.model = model.value;
    if (success()) body.success = success();
    if (maxSteps.value !== '') body.max_steps = Number(maxSteps.value);
    if (dataset.value.trim()) body.dataset = dataset.value.trim();
    if (lines(allowed.value).length) body.allowed_packages = lines(allowed.value);
    const values = Object.fromEntries([...valueRows.children].map((row) => row.read()).filter(([name]) => name));
    if (Object.keys(values).length) body.values = values;
    if (!selection.size) throw new Error('Choose at least one running Android to teach on.');
    let data;
    try {
      data = await post(API, body);
    } catch (error) {
      // What the server refused is shown at the field as well as in the message.
      focusField(applyFieldErrors(error, formEl)[0]);
      throw error;
    }
    ok('The Pilot session started.');
    go(`pilot/${data.task.id}`);
    reload();
  };

  drawChips();
  drawCheck();
  // Grouped the way the operator thinks: the goal, where it runs, what runs it, and
  // how a run is judged. Fields that belong together (the app and its picker, the check
  // and its value) share a row, so that nothing sits alone in a half-empty grid.
  const section = (title, ...body) => h('fieldset', null, h('legend', null, title), body);
  const formEl = h('form', { class: 'stack pilot-form', onsubmit: (event) => { event.preventDefault(); return attempt(start); } },
    h('div', { class: 'field', dataset: { path: 'goal' } }, h('label', { for: (goal.id = 'pilot-goal') }, 'Goal, in plain words'), goal,
      h('div', { class: 'help' }, 'What should be reached in the app, as you would tell a person. One goal per session.')),
    section('Androids to teach on',
      h('div', { class: 'field' }, h('div', { class: 'toolbar' }, selectorBox(chosen, () => running)), chips,
        h('div', { class: 'help' }, `Each one asks the model once per step; ${config.ai.pilot.max_parallel} run at the same time, the rest queue.`))),
    section('App and model',
      h('div', { class: 'fields two' },
        field('App (package)', pkg, 'The app the goal is in. Type it, or pick from what is installed.', { path: 'package' }),
        field('Installed on the first selected Android', installed, 'Picking one fills in the package.'),
        field('Model', model, null, { path: 'model' }),
        field('Runs on each Android', runs, 'Several runs give training something to compare. Failed runs are discarded.', { path: 'runs' }))),
    section('Success check',
      h('div', { class: 'fields two' },
        field('What must be on screen at the end', checkKind),
        field('Value for the check', checkValue, ['Sent as: ', checkString], { path: 'success' }),
        h('p', { class: 'help wide' }, 'Choose something only the final screen has. Without a check of yours the Pilot can only accept an element the model itself names as evidence, which proves less; a run that nothing confirms ends "unverified".'))),
    h('details', null, h('summary', null, 'More: step limit, values to type, allowed apps, dataset name'),
      h('div', { class: 'fields' },
        field('Most actions per run', maxSteps, null, { path: 'max_steps' }),
        field('Other apps a run may pass through', allowed, 'Comma-separated packages. Landing anywhere else ends the run.', { path: 'allowed_packages' }),
        field('Dataset the recordings go to', dataset, 'Leave empty to name it after the goal.', { path: 'dataset' }),
        h('div', { class: 'field wide' }, h('span', { class: 'label-row' }, h('strong', null, 'Values the Pilot may type, by name')), valueRows,
          h('div', { class: 'row' }, button('Add a value', addValue, 'small', 'plus')),
          h('div', { class: 'help' }, 'The model is told the names, never the values, and a recording stores only the name. "secret" hides the value on this screen; values are kept in memory for this session only.')))),
    h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn primary' }, icon('pilot'), 'Start the Pilot')));
  // With one Android running there is nothing to choose: it is selected (after the
  // selector box exists, so that it shows the selection too).
  if (running.length === 1) chosen.setSelection([running[0].id]);
  return formEl;
}

// ---------------------------------------------------------------- one session
function sessionView(root, id, scope, instances) {
  const box = h('div', { class: 'stack' }, skeleton(5));
  const trainBox = h('div');
  const thumbs = thumbnails(scope, { width: 200 });
  const cards = new Map();                 // device id -> { el, img, body, shown }
  let session = null;
  let trained = null;
  const nameOf = (device) => (instances.find((i) => i.id === Number(device)) || {}).name || `Android ${device}`;
  const call = (path, body, done) => attempt(() => post(`${API}/sessions/${id}${path}`, body).then(() => load()), done);

  function deviceCard(key, device) {
    let card = cards.get(key);
    if (!card) {
      card = { img: thumbs.image(Number(key), `Screen of ${nameOf(key)}`), body: h('div', { class: 'stack tight grow' }), shown: '' };
      card.el = h('article', { class: 'pilot-device card', 'aria-label': `${nameOf(key)} in this session` }, h('div', { class: 'pilot-shot' }, card.img), card.body);
      cards.set(key, card);
    }
    const now = JSON.stringify(device);
    if (now === card.shown) return card.el;      // nothing new: leave buttons and focus alone
    card.shown = now;
    const waiting = device.waiting;
    const unverified = (device.results || []).filter((r) => r.state === 'unverified');
    const live = !device.finished && ACTIVE.has(session.state);
    replace(card.body,
      h('div', { class: 'card-head' },
        h('h3', null, h('a', { href: `#/android/${key}` }, nameOf(key)), h('span', { class: 'num muted small' }, `#${key}`)),
        stateBadge(device.state),
        h('span', { class: 'small muted' }, `run ${Math.min(device.run || 0, device.runs)} of ${device.runs}`),
        live && button('Stop this Android', () => call('/cancel', { instance: Number(key) }, `Stopping the Pilot on ${nameOf(key)}.`), 'small danger', 'stop')),
      h('p', null, device.steps ? `Step ${device.step} of at most ${device.steps}` : device.step ? `Step ${device.step}` : '',
        device.step_label ? [': ', h('strong', null, device.step_label)] : '',
        device.model_calls != null ? h('span', { class: 'small muted' }, ` · ${device.model_calls} model request(s)${device.rejected ? `, ${device.rejected} answer(s) not used` : ''}`) : ''),
      (device.error || device.message) && h('p', { class: device.error ? 'small fail' : 'small muted' }, device.error || device.message),
      waiting && h('div', { class: 'banner warn', role: 'alert' }, icon('alert'),
        h('div', { class: 'grow' }, h('strong', null, `Waiting for you: ${waiting.what || 'the run cannot continue by itself'}. `), waiting.reason || '',
          ' Open the Android, put it right, then choose.'),
        button('Resume', () => call(`/devices/${key}/resolve`, { action: 'resume' }, 'Resumed.'), 'small primary'),
        button('Skip', () => call(`/devices/${key}/resolve`, { action: 'skip' }, 'Skipped.'), 'small'),
        button('Abort', () => call(`/devices/${key}/resolve`, { action: 'abort' }, 'Aborted on that Android.'), 'small danger')),
      unverified.map((result) => h('div', { class: 'banner info' }, icon('help'),
        h('div', { class: 'grow' }, h('strong', null, 'An unverified run. '), 'The model said the goal was reached and nothing on the screen confirmed it. ',
          result.message || '', ' Its recording is set aside; confirm it only if you looked and it is right.'),
        button('Confirm this run', () => call('/confirm', { instance: Number(key), recording: result.recording }, 'Run confirmed; its recording now counts.'), 'small primary', 'check'))),
      (device.results || []).length > 0 && h('div', { class: 'row small' }, h('span', { class: 'muted' }, 'Runs so far:'),
        device.results.map((result, index) => h('span', { class: 'row' }, h('span', { class: 'num muted' }, `${index + 1}.`), stateBadge(result.state),
          result.elapsed != null ? h('span', { class: 'muted' }, duration(result.elapsed)) : null))),
      (device.actions || []).length > 0 && h('details', { open: live || Boolean(waiting) },
        h('summary', null, `${device.actions.length} action(s) of this run, with the model’s stated reason`),
        h('ol', { class: 'pilot-actions' }, device.actions.map((action) => h('li', { value: action.n },
          h('div', null, h('code', null, action.action), ' ', h('span', null, action.target || ''), action.outcome ? h('span', { class: 'muted' }, ` → ${action.outcome}`) : null),
          action.reason && h('div', { class: 'small muted reason' }, 'Model’s reason: ', action.reason))))));
    return card.el;
  }

  let trainShown = '';
  function trainingView(force = false) {
    const count = session.recordings.length;
    const summary = trained;
    const now = JSON.stringify([count, ACTIVE.has(session.state), Boolean(trained)]);
    if (now === trainShown && !force) return;        // unchanged: the Train button keeps its state and the focus
    trainShown = now;
    replace(trainBox, panel('Train a program', { icon: 'automation' },
      h('p', null, `${plural(count, 'verified recording')} in the dataset `, h('code', null, session.dataset), '. ',
        count >= 3 ? 'Three or more give a real consensus.' : 'With fewer than three, one wrong run cannot be outvoted: more runs make a better program.'),
      h('div', { class: 'row' },
        h('button', { type: 'button', class: 'btn primary', disabled: count === 0 || ACTIVE.has(session.state), dataset: { action: `pilot-train:${session.dataset}` }, onclick: () => attempt(async () => {
          const data = await post(`${API}/${encodeURIComponent(session.dataset)}/train`);
          const task = await finished(data.task);
          if (task.state !== 'done') throw new Error(task.error || `Training ended as ${task.state}.`);
          trained = { ...task.output.summary, program: task.output.program };
          trainingView(true);
        }) }, icon('automation'), `Train a program from ${plural(count, 'verified run')}`),
        ACTIVE.has(session.state) && h('span', { class: 'small muted' }, 'Available when the session has ended.'),
        count === 0 && !ACTIVE.has(session.state) && h('span', { class: 'small muted' }, 'No run was verified, so there is nothing to train. Run the goal again, or confirm an unverified run.')),
      summary && h('div', { class: 'stack tight pilot-summary' },
        h('div', { class: 'row' }, badge('ok', `${summary.agreed.length} step(s) made in every recording`),
          summary.disagreed.length ? badge('warn', `${summary.disagreed.length} made only in some`) : null,
          h('span', { class: 'small muted' }, `${summary.steps} steps from ${plural(summary.recordings, 'recording')}`)),
        h('ul', null, summary.text.map((line) => h('li', null, line))),
        summary.warnings.length > 0 && h('div', { class: 'banner warn' }, icon('alert'), h('div', { class: 'grow' }, h('strong', null, 'The trainer warns: '), summary.warnings.join(' '))),
        h('ol', { class: 'steps' }, summary.outline.map((step) => h('li', null, step.label, ' ', h('span', { class: 'small muted' }, `(${step.kind}; in ${step.seen_in} of ${summary.recordings})`)))),
        h('div', { class: 'row' },
          button('Run this program…', () => runDialog('program', session.dataset, (summary.program && summary.program.dynamic) || []), 'primary', 'play'),
          linkButton('Programs', '#/automation/programs', ''),
          h('span', { class: 'small muted' }, 'Replaying needs no model. The program starts the app but does not stop it first: stop the app before a replay if it may be on another screen.')))));
  }

  const headBox = h('div', { class: 'card-head' });
  const chipsBox = h('div', { class: 'limits' });
  const statusLine = h('p', { role: 'status' });
  const devicesBox = h('div', { class: 'pilot-devices' });
  let headShown = '';
  let built = false;

  async function load() {
    session = (await get(`${API}/sessions/${encodeURIComponent(id)}`)).session;
    const results = Object.values(session.devices).flatMap((device) => device.results || []);
    const verified = (session.counts.done || 0) + (session.counts.confirmed || 0);
    if (!built) {
      built = true;
      replace(box, h('div', { class: 'card pilot-top' }, headBox, chipsBox, statusLine), devicesBox, trainBox);
    }
    // The head (with its Cancel button) is redrawn only when the session changes state.
    const headNow = JSON.stringify([session.state, session.success]);
    if (headNow !== headShown) {
      headShown = headNow;
      replace(headBox, h('h2', null, icon('pilot'), session.goal), sessionBadge(session.state),
        ACTIVE.has(session.state) && h('button', { type: 'button', class: 'btn danger', dataset: { action: `pilot-cancel:${id}` }, onclick: () => call('/cancel', {}, 'Cancelling the session.') }, icon('stop'), 'Cancel the session'));
      replace(chipsBox,
        h('span', { class: 'info-chip' }, icon('apps'), h('span', { class: 'k' }, 'app'), h('code', { class: 'v' }, session.package)),
        h('span', { class: 'info-chip' }, icon('plug'), h('span', { class: 'k' }, 'model'), h('code', { class: 'v' }, session.model)),
        h('span', { class: 'info-chip' }, icon('check'), h('span', { class: 'k' }, 'success check'), h('span', { class: 'v' }, session.success ? session.success.describe : 'none yet')),
        session.success && h('span', { class: 'info-chip' }, h('span', { class: 'k' }, 'chosen by'), h('span', { class: 'v' }, session.success.source)));
    }
    replace(statusLine, h('strong', null, `Verified ${verified} of ${results.length} finished run(s)`),
      ` (${session.runs} per Android on ${plural(Object.keys(session.devices).length, 'Android')}). `,
      `${plural(session.recordings.length, 'recording')} collected for training; ${session.model_calls} model request(s) so far.`,
      session.unverified.length ? ` ${plural(session.unverified.length, 'unverified run')} set aside.` : '',
      (session.counts.failed || 0) ? ` ${session.counts.failed} failed and were discarded: that costs time, not correctness.` : '');
    for (const [key, device] of Object.entries(session.devices)) {
      const el = deviceCard(key, device);
      if (!el.isConnected) devicesBox.append(el);
    }
    trainingView();
    return session;
  }

  root.append(h('nav', { class: 'crumbs', 'aria-label': 'Breadcrumb' }, h('a', { href: '#/pilot' }, 'AI Pilot'), icon('chevron-right'), h('span', { class: 'num' }, id)), box);
  let turn = 0;
  let asked = false;
  return scope.every(1000, async () => {
    turn += 1;
    const quiet = session && !ACTIVE.has(session.state) && !Object.values(session.devices).some((device) => device.waiting);
    if (quiet && turn % 5) return;                          // ended: look again only now and then
    await load();
    if (!trained && !asked && !ACTIVE.has(session.state)) {
      // A dataset trained earlier shows its summary; asked once, a 404 means "not trained yet".
      asked = true;
      trained = await get(`${API}/${encodeURIComponent(session.dataset)}/summary`).then((data) => data.summary, () => null);
      if (trained) trainingView(true);
    }
  });
}

export async function render(root, params, scope) {
  const [instances, modelData, configData] = await Promise.all([
    loadInstances(), get('/api/ai/models').catch(() => ({ models: [] })), get('/api/config')]);
  const models = modelData.models;
  const intro = pageHead('AI Pilot', 'Teach with AI once, run everywhere without AI: a model works out how to reach a goal in an app, and the runs that really got there are trained into a program that replays with no model.',
    linkButton('Models', '#/access/models', '', 'plug'), linkButton('Programs', '#/automation/programs', '', 'automation'));
  if (params[0]) {
    root.append(intro);
    await sessionView(root, params[0], scope, instances);
    return;
  }

  const table = dataTable({
    label: 'Pilot sessions',
    key: (s) => s.id,
    columns: [
      { title: 'Goal', cell: (s) => h('a', { href: `#/pilot/${s.id}` }, s.goal) },
      { title: 'App', cell: (s) => h('code', null, s.package) },
      { title: 'Androids', class: 'num', cell: (s) => Object.keys(s.devices).length },
      { title: 'State', cell: (s) => sessionBadge(s.state) },
      { title: 'Verified runs', cell: (s) => `${(s.counts.done || 0) + (s.counts.confirmed || 0)} of ${Object.values(s.counts).reduce((a, b) => a + b, 0)}` },
      { title: 'Started', cell: (s) => when(s.created) },
    ],
    empty: emptyState('pilot', 'No Pilot sessions yet', 'State a goal above and let a model try it on one or two Androids. Sessions are kept until the daemon restarts; their verified recordings stay in the dataset.'),
  });
  const loadSessions = async () => table.update((await get(`${API}/sessions`)).sessions);

  root.append(intro,
    h('div', { class: 'stack' },
      h('div', { class: 'banner info', role: 'note' }, icon('info'),
        h('div', { class: 'grow' }, h('strong', null, 'What to expect. '),
          'A small local model reaches a short goal only some of the time: in the farm’s own measurements about 6 runs in 10 were verified on a five-action goal, and single sessions ranged from none to nearly all. ',
          'A run counts only when a check holds on the real screen; failed runs are discarded, so a weak model costs time, not correctness. The trained program needs no model to replay.')),
      models.length === 0
        ? emptyState('plug', 'No model is configured', 'The Pilot needs a language model to work out the steps once. Add a profile for a model server you run; the program it teaches then runs without one.',
          linkButton('Set up a model', '#/access/models', 'primary', 'plug'))
        : panel('Teach a goal', { icon: 'pilot' }, startForm(instances, models, configData.config, () => loadSessions().catch(() => {}))),
      panel('Sessions', { icon: 'clock', actions: button('Refresh', () => attempt(loadSessions), 'small', 'restart') }, table.el)));
  await scope.every(5000, loadSessions);
}
