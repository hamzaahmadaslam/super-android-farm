// Automation: datasets and recordings, training, programs, flows, values, runs, schedules.
//
// Where the real farm reads an app's screen and replays steps on it, the demo plays the
// steps on its drawn screens (device.js). Bundled example flows are the real ones, with
// the real validator's summary of each (generated/flows.js). A flow typed by a visitor is
// checked by a simplified checker written here, and the demo says so when it does.

import flowData from '../generated/flows.js';
import { accepted, created, route, truthy, whole } from '../backend.js';
import * as device from '../device.js';
import { ACTIVE, ApiError, bad, conflict, findTask, notFound, onTick, record, registerEffect, select, submit } from '../farm.js';
import { db, emit, hex, isoLocal, now, pad, round, slug as makeSlug } from '../store.js';

const A = '/api/automation';
const auto = () => db.automation;
const ok = () => ({ ok: true });
const SLUG = /^[a-z0-9][a-z0-9-]{0,63}$/;
const FLOW_NAME = /^(examples\/)?[a-z0-9][a-z0-9-]{0,63}$/;
const VALUE_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
const forbidden = () => db.config.automation.forbidden_field_words;
const looksSecret = (name) => forbidden().some((word) => String(name).toLowerCase().includes(word));

// ----------------------------------------------------------------- datasets
function datasetSummary(d) {
  return {
    slug: d.slug, name: d.name, description: d.description, package: d.package, created: d.created, updated: d.updated, recordings: d.recordings.length,
    steps: d.recordings.reduce((sum, r) => sum + r.steps, 0), trained: auto().programs.some((p) => p.slug === d.slug),
  };
}
function dataset(slug) {
  const found = auto().datasets.find((d) => d.slug === slug);
  if (!found) throw notFound(`no dataset '${String(slug).slice(0, 64)}'`);
  return found;
}

route('GET', `${A}/datasets`, () => ({ datasets: auto().datasets.map(datasetSummary) }));
route('POST', `${A}/datasets`, ({ body }) => {
  const name = typeof body.name === 'string' ? body.name.trim() : '';
  if (!name || name.length > 120) throw new ApiError(422, 'validation_error', 'name: Field required', { detail: [{ field: 'name', message: 'Field required' }] });
  const slug = makeSlug(name);
  if (!SLUG.test(slug)) throw bad('the name needs at least one letter or digit');
  if (auto().datasets.some((d) => d.slug === slug)) throw conflict(`a dataset named '${slug}' already exists`);
  const stamp = isoLocal(now());
  const made = { slug, name, description: body.description || '', package: body.package || '', created: stamp, updated: stamp, recordings: [] };
  auto().datasets.push(made);
  return created({ dataset: datasetSummary(made) });
});
route('GET', `${A}/datasets/{slug}`, ({ params }) => {
  const d = dataset(params.slug);
  return { dataset: { ...datasetSummary(d), recordings_list: d.recordings.map(({ ops, ...rest }) => rest) } };
});
route('PATCH', `${A}/datasets/{slug}`, ({ params, body }) => {
  const d = dataset(params.slug);
  for (const field of ['name', 'description', 'package']) if (typeof body[field] === 'string') d[field] = body[field];
  d.updated = isoLocal(now());
  return { dataset: datasetSummary(d) };
});
route('DELETE', `${A}/datasets/{slug}`, ({ params }) => {
  dataset(params.slug);
  auto().datasets = auto().datasets.filter((d) => d.slug !== params.slug);
  return ok();
});
route('GET', `${A}/datasets/{slug}/recordings/{rid}`, ({ params }) => {
  const found = dataset(params.slug).recordings.find((r) => r.id === params.rid);
  if (!found) throw notFound(`no recording '${params.rid.slice(0, 40)}' in '${params.slug}'`);
  const { ops, preview, ...rest } = found;
  return { recording: { ...rest, package: dataset(params.slug).package, screen: [device.W, device.H], steps: (ops || []).map((op, index) => ({ id: `s${pad(index, 4)}`, kind: op.kind })), describe: preview } };
});
route('DELETE', `${A}/datasets/{slug}/recordings/{rid}`, ({ params }) => {
  const d = dataset(params.slug);
  if (!d.recordings.some((r) => r.id === params.rid)) throw notFound('no such recording');
  d.recordings = d.recordings.filter((r) => r.id !== params.rid);
  return ok();
});

// ---------------------------------------------------------------- recording
function session(id) {
  const rec = record(id);
  const found = auto().recording[rec.spec.id];
  if (!found) throw conflict(`Android ${rec.spec.id} is not being recorded; start a recording first`);
  return { rec, found };
}
function sessionStatus(s, t = now()) {
  return {
    recording: s.recording, dataset: s.dataset, instance: s.instance, device: s.device, active: true, steps: s.ops.length, seconds: round(t - s.started, 2),
    screen: [device.W, device.H], last_problem: s.problem || '', preview: s.ops.slice(-8).map((op) => op.describe),
  };
}
function addStep(s, op) {
  const step = { id: `s${pad(s.ops.length, 4)}`, at: round(now() - s.started, 2), ...op, read_problem: '' };
  s.ops.push(step);
  return step;
}
const point = (body) => {
  if (typeof body.x !== 'number' || typeof body.y !== 'number') throw new ApiError(422, 'validation_error', 'x: Input should be a valid number');
  return [body.x, body.y];
};
const describeElement = (hit) => (hit ? { id: hit.id, text: hit.text || '', cls: hit.cls, bounds: hit.bounds, clickable: true, editable: Boolean(hit.editable) } : null);

route('GET', `${A}/record`, () => ({ recordings: Object.values(auto().recording).map((s) => sessionStatus(s)) }));
route('POST', `${A}/record/start`, ({ body }) => {
  const rec = record(body.id);
  const d = dataset(body.dataset);
  if (rec.phase !== 'booted') throw conflict(`Android ${rec.spec.id} is not running; recording needs a running device`);
  if (auto().recording[rec.spec.id]) throw conflict(`Android ${rec.spec.id} is already being recorded`);
  if (Object.values(auto().recording).length >= 2) throw conflict('the demo records on two Androids at a time');
  const t = now();
  const clock = new Date(t * 1000);
  const s = {
    recording: `r${pad(d.recordings.length + 1, 3)}-${pad(clock.getHours())}${pad(clock.getMinutes())}${pad(clock.getSeconds())}`, dataset: d.slug, instance: rec.spec.id,
    device: `${db.config.network.adb_bind}:${db.config.network.adb_port_base + rec.spec.id}`, started: t, ops: [], problem: '',
  };
  auto().recording[rec.spec.id] = s;
  return sessionStatus(s, t);
});
route('GET', `${A}/record/{id}`, ({ params }) => sessionStatus(session(params.id).found));

route('POST', `${A}/record/{id}/tap`, ({ params, body }) => {
  const { rec, found } = session(params.id);
  const [x, y] = point(body);
  const hit = device.tap(rec, x, y);
  found.problem = hit ? '' : 'that tap landed on no named element; the program would have to tap the same spot blindly';
  return addStep(found, { kind: 'tap', device: [Math.round(x * device.W), Math.round(y * device.H)], element: describeElement(hit), text: hit ? hit.text : '',
    describe: hit ? `tap '${hit.text || hit.id}'` : `tap at (${Math.round(x * device.W)}, ${Math.round(y * device.H)})` });
});
route('POST', `${A}/record/{id}/long-press`, ({ params, body }) => {
  const { rec, found } = session(params.id);
  const [x, y] = point(body);
  const hit = device.tap(rec, x, y);
  return addStep(found, { kind: 'long_press', element: describeElement(hit), text: hit ? hit.text : '', describe: hit ? `long-press '${hit.text || hit.id}'` : 'long-press' });
});
route('POST', `${A}/record/{id}/swipe`, ({ params, body }) => {
  const { rec, found } = session(params.id);
  device.swipe(rec, body.x1, body.y1, body.x2, body.y2);
  const direction = Math.abs(body.x2 - body.x1) > Math.abs(body.y2 - body.y1) ? (body.x2 > body.x1 ? 'right' : 'left') : (body.y2 > body.y1 ? 'down' : 'up');
  return addStep(found, { kind: 'swipe', direction, describe: `swipe ${direction}` });
});
route('POST', `${A}/record/{id}/text`, ({ params, body }) => {
  const { rec, found } = session(params.id);
  if (body.name) {
    if (!VALUE_NAME.test(body.name)) throw bad(`bad value name '${String(body.name).slice(0, 40)}'`);
    device.type(rec, `{${body.name}}`);
    return addStep(found, { kind: 'type', name: body.name, describe: `type {${body.name}}` });
  }
  if (typeof body.value !== 'string' || !body.value) throw bad('give `value` (text typed now) or `name` (a value looked up when the program runs)');
  device.type(rec, body.value);
  return addStep(found, { kind: 'type', text: body.value, describe: `type '${body.value.slice(0, 40)}'` });
});
route('POST', `${A}/record/{id}/key`, ({ params, body }) => {
  const { rec, found } = session(params.id);
  const name = String(body.name || '').toUpperCase();
  if (!name) throw new ApiError(422, 'validation_error', 'name: Field required');
  device.key(rec, name);
  return addStep(found, { kind: 'key', key: name.toLowerCase(), describe: `press ${name.toLowerCase()}` });
});
route('POST', `${A}/record/{id}/launch`, ({ params, body }) => {
  const { rec, found } = session(params.id);
  if (!device.launch(rec, body.package)) throw conflict(`${String(body.package).slice(0, 80)} is not installed on Android ${rec.spec.id}`);
  return addStep(found, { kind: 'launch', package: body.package, describe: `launch ${body.package}` });
});
route('POST', `${A}/record/{id}/wait`, ({ params, body }) => {
  const { found } = session(params.id);
  const seconds = Number(body.seconds);
  if (!(seconds > 0 && seconds <= 600)) throw new ApiError(422, 'validation_error', 'seconds: Input should be greater than 0');
  return addStep(found, { kind: 'wait', seconds, describe: `wait ${seconds}s` });
});
route('POST', `${A}/record/{id}/assert-visible`, ({ params, body }) => {
  const { rec, found } = session(params.id);
  const [x, y] = point(body);
  const hit = device.elementAt(rec, x * device.W, y * device.H);
  if (!hit) throw conflict('there is no named element at that point to check for');
  return addStep(found, { kind: 'assert', text: hit.text, describe: `check '${hit.text || hit.id}' is visible` });
});
route('POST', `${A}/record/{id}/undo`, ({ params }) => ({ undone: Boolean(session(params.id).found.ops.pop()) }));
route('POST', `${A}/record/{id}/stop`, ({ params }) => {
  const { rec, found } = session(params.id);
  const t = now();
  delete auto().recording[rec.spec.id];
  if (!found.ops.length) return { ...sessionStatus(found, t), active: false, taps: 0, named_taps: 0, summary: 'nothing was recorded, so nothing was saved' };
  const taps = found.ops.filter((op) => op.kind === 'tap');
  const named = taps.filter((op) => op.element).length;
  const d = dataset(found.dataset);
  d.recordings.push({
    id: found.recording, created: isoLocal(found.started), device: found.device, instance: found.instance, steps: found.ops.length, taps: taps.length,
    seconds: round(t - found.started, 1), note: '', dynamic: found.ops.filter((op) => op.name).map((op) => op.name), preview: found.ops.slice(0, 8).map((op) => op.describe),
    ops: found.ops.map(({ kind, describe, text, name, package: pkg, key: which, element }) => ({ kind, describe, text, name, package: pkg, key: which, target: element ? element.id : '' })),
  });
  d.updated = isoLocal(t);
  return { ...sessionStatus(found, t), active: false, taps: taps.length, named_taps: named,
    summary: `saved ${found.recording}: ${found.ops.length} step(s), ${named} of ${taps.length} tap(s) landed on a named element` };
});
route('POST', `${A}/record/{id}/cancel`, ({ params }) => {
  const { rec } = session(params.id);
  delete auto().recording[rec.spec.id];
  return { cancelled: true };
});

// ----------------------------------------------------------------- training
registerEffect('train', (slug) => {
  const d = auto().datasets.find((entry) => entry.slug === slug);
  if (!d || !d.recordings.length) return { ok: false, detail: 'the dataset or its recordings were deleted meanwhile' };
  // The longest recording stands in for the consensus the real trainer builds.
  const base = [...d.recordings].sort((a, b) => b.steps - a.steps)[0];
  const ops = base.ops || base.preview.map((line) => ({ kind: line.split(' ')[0], describe: line }));
  const warnings = d.recordings.length < 3
    ? [d.recordings.length === 1 ? 'trained from a single recording: nothing could be cross-checked. Record the same goal at least three times for a real consensus.'
      : 'trained from two recordings: a third lets the trainer settle differences between them.'] : [];
  const program = {
    slug: d.slug, name: d.name, goal: d.description, package: d.package, created: isoLocal(now()), model: '', steps: ops.length, optional: 0,
    with_selector: ops.filter((op) => op.target || /'/.test(op.describe)).length, dynamic: [...new Set(d.recordings.flatMap((r) => r.dynamic || []))],
    from_recordings: d.recordings.map((r) => r.id), warnings, notes: ['demo: built from the longest recording, not by comparing them'],
    outline: ops.map((op, index) => ({ id: `p${pad(index, 3)}`, kind: op.kind, label: op.describe, selector: op.target ? { id: op.target } : {}, optional: false,
      dynamic: op.name || null, seen_in: d.recordings.length, shot: null, has_selector: Boolean(op.target) || op.kind !== 'tap' })),
    ops,
  };
  auto().programs = [...auto().programs.filter((p) => p.slug !== d.slug), program];
  const { outline, ops: _ops, ...summary } = program;
  return { output: summary };
});

route('POST', `${A}/datasets/{slug}/train`, ({ params }) => {
  const d = dataset(params.slug);
  if (!d.recordings.length) throw bad(`the dataset '${d.name}' has no recordings yet`);
  return accepted(submit('automation-train', `train ${d.name}`, [{ after: 0.8, say: 'building the consensus' }, { after: 3.2, effect: ['train', d.slug], say: 'trained' }]));
});

// ----------------------------------------------------------------- programs
const programSummary = ({ outline, ops, ...rest }) => rest;
function program(slug) {
  const found = auto().programs.find((p) => p.slug === slug);
  if (!found) throw notFound(`no trained program '${String(slug).slice(0, 64)}'`);
  return found;
}
route('GET', `${A}/programs`, () => ({ programs: auto().programs.map(programSummary) }));
route('GET', `${A}/programs/{slug}`, ({ params }) => { const { ops, ...rest } = program(params.slug); return { program: rest }; });
route('DELETE', `${A}/programs/{slug}`, ({ params }) => {
  program(params.slug);
  auto().programs = auto().programs.filter((p) => p.slug !== params.slug);
  return ok();
});

// -------------------------------------------------------------------- flows
// The simplified checker: it understands the layout of a flow file (top-level keys, the
// list under `steps:`) and knows the real step names, and nothing more. It does not check
// a step's arguments, nested steps, or the flows a flow calls.
export function checkFlow(text, name = '') {
  if (typeof text !== 'string' || !text.trim()) throw bad('the flow is empty');
  if (text.length > 512 * 1024) throw bad('the flow is larger than 512 KB');
  const lines = text.split('\n').map((line) => line.replace(/\s+$/, '')).filter((line) => line.trim() && !line.trim().startsWith('#'));
  const top = {};
  let key = '';
  for (const line of lines) {
    const m = /^([A-Za-z_]+):\s*(.*)$/.exec(line);
    if (m) { key = m[1]; top[key] = { value: m[2].replace(/\s+#.*$/, ''), block: [] }; } else if (/^\s/.test(line) || line.startsWith('- ')) {
      if (!key) throw bad('not valid YAML at line 1: a flow starts with a key such as `steps:`');
      top[key].block.push(line);
    } else throw bad(`not valid YAML: cannot read the line ${JSON.stringify(line.slice(0, 60))}`);
  }
  const known = ['name', 'description', 'package', 'variables', 'timeouts', 'overlays', 'never_tap', 'on_unknown', 'steps'];
  const unknown = Object.keys(top).filter((k) => !known.includes(k));
  if (unknown.length) throw bad(`unknown key(s): ${unknown.join(', ')}. A flow has: ${known.join(', ')}`);
  if (!top.steps) throw bad('steps: a flow needs at least one step');
  const unquote = (value) => value.trim().replace(/^(['"])(.*)\1$/, '$2');
  const variables = {};
  const pairs = top.variables ? (top.variables.value.startsWith('{') ? top.variables.value.replace(/^\{|\}$/g, '').split(',') : top.variables.block.map((line) => line.replace(/\s+#.*$/, ''))) : [];
  for (const pair of pairs.map((entry) => entry.trim()).filter(Boolean)) {
    const at = pair.indexOf(':');
    if (at < 1) throw bad(`variables: cannot read ${JSON.stringify(pair.slice(0, 40))}`);
    const variable = pair.slice(0, at).trim();
    const value = unquote(pair.slice(at + 1));
    if (looksSecret(variable)) throw bad(`variables: '${variable}' looks like a secret, and secrets are never written into a flow. Pass it when you start the run instead`);
    variables[variable] = value === '' || value === 'null' || value === '~' ? null : value;
  }
  const items = top.steps.block.filter((line) => /^\s*- /.test(line));
  if (!items.length) throw bad('steps: a flow needs at least one step');
  const indent = Math.min(...items.map((line) => line.search(/\S/)));
  const steps = [];
  top.steps.block.forEach((line) => {
    const at = line.search(/\S/);
    if (at === indent && line.trim().startsWith('- ')) {
      const m = /^-\s*([A-Za-z_]+)\s*:?\s*(.*)$/.exec(line.trim());
      const action = m ? m[1] : '';
      if (!flowData.actions.includes(action)) throw bad(`steps[${steps.length}]: no action found (saw ${action || line.trim().slice(2, 30)}). Actions: ${flowData.actions.join(', ')}`);
      steps.push({ kind: action, arg: m[2].replace(/\s+#.*$/, ''), label: '' });
    } else if (steps.length && /^\s*label:\s*/.test(line) && at === indent + 2) {
      steps[steps.length - 1].label = unquote(line.trim().slice(6));
    }
  });
  if (steps.length > 500) throw bad('a flow has at most 500 steps');
  const field = (arg, names) => {
    for (const one of names) {
      const m = new RegExp(`${one}:\\s*("([^"]*)"|'([^']*)'|[^,}]+)`).exec(arg);
      if (m) return unquote(m[1]);
    }
    return '';
  };
  const ops = steps.map((step) => {
    const inline = step.arg.startsWith('{') ? '' : unquote(step.arg);
    const text = field(step.arg, ['text']);
    const id = field(step.arg, ['id']);
    const target = text ? `text='${text}'` : id ? `id='${id}'` : '';
    let describe = step.kind;
    if (step.kind === 'launch' || step.kind === 'stop_app') describe = `${step.kind === 'launch' ? 'launch' : 'stop'} ${inline || field(step.arg, ['package']) || 'the app'}`;
    else if (step.kind === 'type') describe = `type '${text || inline}'`;
    else if (step.kind === 'key') describe = `press ${(inline || field(step.arg, ['name'])).toLowerCase()}`;
    else if (step.kind === 'sleep') describe = `sleep ${inline || field(step.arg, ['seconds'])}s`;
    else if (target) describe = `${step.kind} ${target}`;
    return { kind: step.kind, describe: step.label || describe, text: step.kind === 'type' ? (text || inline) : text, target: id, package: step.kind === 'launch' ? (inline || field(step.arg, ['package'])) : '',
      key: step.kind === 'key' ? (inline || field(step.arg, ['name'])) : '' };
  });
  const summary = {
    name: name || unquote((top.name || {}).value || '') || 'flow', description: unquote((top.description || {}).value || ''), package: unquote((top.package || {}).value || ''), variables,
    steps: ops.length, builtin: false, overlays: [], never_tap: 0, on_unknown: unquote((top.on_unknown || {}).value || '') || 'stop', outline: ops.map((op) => op.describe),
  };
  return { summary, ops };
}

const example = (name) => flowData.examples.find((entry) => entry.name === name);
const flowNames = () => [...Object.keys(auto().flows).sort(), ...flowData.examples.map((entry) => entry.name)];
function flowName(name) {
  if (!FLOW_NAME.test(name)) throw bad(`bad flow name '${String(name).slice(0, 80)}': use lower-case letters, digits and dashes (bundled examples are \`examples/<name>\`)`);
  return name;
}
function flowText(name) {
  flowName(name);
  if (name.startsWith('examples/')) {
    const found = example(name);
    if (!found) throw notFound(`no bundled example flow '${name}'`);
    return found.text;
  }
  if (!(name in auto().flows)) throw notFound(`no flow named '${name}'`);
  return auto().flows[name];
}
function flowSummary(name) {
  if (name.startsWith('examples/')) return example(name).summary;
  return checkFlow(auto().flows[name], name).summary;
}
const SIMPLIFIED = 'Validated by the demo’s simplified checker: it knows the layout of a flow and the real step names, not every rule of the real validator.';

route('GET', `${A}/flows`, () => ({
  flows: flowNames().map((name) => {
    try {
      return flowSummary(name);
    } catch (error) {
      return { name, description: '', package: '', variables: {}, steps: 0, builtin: false, overlays: [], never_tap: 0, on_unknown: 'stop', outline: [], error: error.message };
    }
  }),
}));
route('POST', `${A}/flows/validate`, ({ body }) => {
  if (typeof body.text === 'string') {
    emit({ type: 'note', text: SIMPLIFIED });
    return { ok: true, flow: checkFlow(body.text, body.name || '').summary };
  }
  if (body.name) { flowText(body.name); return { ok: true, flow: flowSummary(body.name) }; }
  throw bad('give `text` (YAML to check) or `name` (a saved flow)');
});
route('GET', `${A}/flows/{name*}`, ({ params }) => {
  const text = flowText(params.name);
  let summary = null;
  let error = '';
  try { summary = flowSummary(params.name); } catch (failure) { error = failure.message; }
  return { name: params.name, text, flow: summary, error, builtin: params.name.startsWith('examples/') };
});
route('PUT', `${A}/flows/{name*}`, ({ params, body }) => {
  flowName(params.name);
  if (params.name.startsWith('examples/')) throw bad('bundled examples are read-only; save a copy under a name of your own');
  const checked = checkFlow(body.text, params.name);
  if (Object.keys(auto().flows).length >= 30 && !(params.name in auto().flows)) throw bad('the demo keeps at most 30 flows');
  auto().flows[params.name] = body.text;
  emit({ type: 'note', text: SIMPLIFIED });
  return { flow: checked.summary };
});
route('DELETE', `${A}/flows/{name*}`, ({ params }) => {
  flowName(params.name);
  if (params.name.startsWith('examples/')) throw bad('bundled examples are read-only');
  if (!(params.name in auto().flows)) throw notFound('no such flow');
  delete auto().flows[params.name];
  return ok();
});

// ----------------------------------------------------------------- profiles
// Per-app roles and pop-up rules are not part of the demo; the list is simply empty.
route('GET', `${A}/profiles`, () => ({ profiles: [] }));

// ------------------------------------------------------------------- values
const valueName = (name) => {
  if (!VALUE_NAME.test(name)) throw bad(`bad value name '${String(name).slice(0, 40)}': use letters, digits and _`);
  if (looksSecret(name)) {
    throw bad(`'${name}' looks like a secret, and secrets are never stored as a value. Pass it when you start the run instead (run variables stay in memory only), `
      + `or pick a name that does not contain: ${forbidden().join(', ')}`);
  }
  return name;
};
const text = (body) => {
  if (typeof body.value !== 'string') throw new ApiError(422, 'validation_error', 'value: Field required', { detail: [{ field: 'value', message: 'Field required' }] });
  return body.value.slice(0, 2000);
};
const poolList = () => Object.entries(auto().values.pools).sort((a, b) => a[0].localeCompare(b[0])).map(([name, pool]) => ({ name, remaining: pool.lines.length, used: pool.used.length }));

route('GET', `${A}/values`, () => ({ shared: { values: auto().values.shared, hidden: [] }, pools: poolList() }));
route('PUT', `${A}/values/shared/{name}`, ({ params, body }) => { auto().values.shared[valueName(params.name)] = text(body); return { name: params.name, stored: true }; });
route('DELETE', `${A}/values/shared/{name}`, ({ params }) => {
  if (!(params.name in auto().values.shared)) throw notFound('no such value');
  delete auto().values.shared[params.name];
  return ok();
});
route('GET', `${A}/values/instances/{id}`, ({ params }) => ({ values: record(params.id).values || {}, hidden: [] }));
route('PUT', `${A}/values/instances/{id}/{name}`, ({ params, body }) => {
  const rec = record(params.id);
  rec.values = { ...(rec.values || {}), [valueName(params.name)]: text(body) };
  return { name: params.name, stored: true };
});
route('DELETE', `${A}/values/instances/{id}/{name}`, ({ params }) => {
  const rec = record(params.id);
  if (!rec.values || !(params.name in rec.values)) throw notFound('no such value');
  delete rec.values[params.name];
  return ok();
});
route('GET', `${A}/values/pools`, () => ({ pools: poolList() }));
route('GET', `${A}/values/pools/{name}`, ({ params, query }) => {
  const pool = auto().values.pools[params.name];
  if (!pool) throw notFound(`no pool named '${params.name.slice(0, 64)}'`);
  return { name: params.name, remaining: pool.lines.length, used: pool.used.slice(-whole(query.limit ?? 50, 1, 1000, 'limit')).reverse() };
});
route('POST', `${A}/values/pools/{name}`, ({ params, body }) => {
  valueName(params.name);
  if (!Array.isArray(body.lines) || !body.lines.length) throw new ApiError(422, 'validation_error', 'lines: Field required');
  const lines = body.lines.map((line) => String(line).trim().slice(0, 200)).filter(Boolean);
  const pool = auto().values.pools[params.name] || (auto().values.pools[params.name] = { lines: [], used: [] });
  if (pool.lines.length + lines.length > 500) throw bad('the demo keeps at most 500 lines in a pool');
  pool.lines.push(...lines);
  return { name: params.name, added: lines.length, remaining: pool.lines.length };
});
route('DELETE', `${A}/values/pools/{name}`, ({ params }) => {
  if (!auto().values.pools[params.name]) throw notFound('no such pool');
  delete auto().values.pools[params.name];
  return ok();
});

// --------------------------------------------------------------------- runs
// A run stores, per device, when it started and how long a step takes. What the device is
// doing at any moment follows from the clock, so a reload in the middle loses nothing.
const STEP_SECONDS = 1.6;
const RATE_DIALOG = 'Enjoying this app? Rate it';

function opsOf(kind, name) {
  if (kind === 'program') return program(name).ops || program(name).outline.map((step) => ({ kind: step.kind, describe: step.label, target: (step.selector || {}).id || '', text: (step.selector || {}).text || '' }));
  if (name.startsWith('examples/')) {
    const found = example(flowName(name));
    if (!found) throw notFound(`no bundled example flow '${name}'`);
    return found.summary.outline.map((line) => ({ kind: line.split(' ')[0], describe: line }));
  }
  flowText(name);
  return checkFlow(auto().flows[name], name).ops;
}

// A value for {name}: given for this run, then the Android's own, then shared, then a pool.
function valueFor(run, id, name) {
  const rec = db.instances[id];
  if (name in run.variables) return run.variables[name];
  if (rec && rec.values && name in rec.values) return rec.values[name];
  if (name in auto().values.shared) return auto().values.shared[name];
  const pool = auto().values.pools[name];
  if (pool && pool.lines.length) {
    const value = pool.lines.shift();
    pool.used.push({ value, at: now(), instance: id, run: run.id });
    return value;
  }
  return `{${name}}`;
}

function play(run, id, op) {
  const rec = db.instances[id];
  if (!rec || rec.phase !== 'booted' || run.dry_run) return;
  const fill = (value) => String(value || '').replace(/\{(\w+)\}/g, (_, name) => valueFor(run, id, name));
  if (op.kind === 'launch') device.launch(rec, fill(op.package) || fill((op.describe || '').replace(/^launch\s+/, '')));
  else if (op.kind === 'type') device.type(rec, fill(op.name ? `{${op.name}}` : op.text));
  else if (op.kind === 'key') device.key(rec, op.key || 'ENTER');
  else if (op.kind === 'back') device.key(rec, 'BACK');
  else if (op.kind === 'home') device.key(rec, 'HOME');
  else if (op.kind === 'tap' || op.kind === 'long_press') {
    const wanted = fill(op.text);
    const hit = device.elements(rec).find((e) => (op.target && e.id === op.target) || (wanted && e.text === wanted));
    if (hit) device.tap(rec, (hit.bounds[0] + hit.bounds[2]) / 2 / device.W, (hit.bounds[1] + hit.bounds[3]) / 2 / device.H);
  }
}

// One device of a run, as the API reports it.
function snapshot(run, id) {
  const d = run.devices[id];
  const { timeline, played, ...rest } = d;
  return rest;
}

function startRun(kind, name, who, { variables = {}, dryRun = false } = {}) {
  const ops = opsOf(kind, name);
  const t = now();
  const task = submit('automation', `${kind} ${name} on ${who.length} device(s)`, [], { targets: who });
  task.hold = true;
  task.total = who.length;
  task.message = `0/${who.length}`;
  // With two or more devices, the last one meets a pop-up the flow does not know.
  const ready = who.filter((id) => db.instances[id].phase === 'booted');
  const stuck = !dryRun && ready.length >= 2 && ops.length >= 2 ? ready[ready.length - 1] : null;
  const parallel = Math.max(1, db.config.automation.max_parallel);
  const run = { id: task.id, kind, name, dry_run: dryRun, created: round(t, 4), variables, ops, devices: {}, cancelled: false };
  ready.forEach((id, index) => { /* slots: `parallel` devices at a time */
    run.devices[id] = {
      id, label: `#${id}`, serial: `${db.config.network.adb_bind}:${db.config.network.adb_port_base + id}`, state: 'queued', step: 0, steps: ops.length, step_label: '',
      message: '', error: '', waiting: null, done: 0, skipped: 0, recovered: 0, issues: [], dry_run: dryRun, elapsed: 0, log: [],
      timeline: { start: t + 0.6 + Math.floor(index / parallel) * ops.length * STEP_SECONDS + (index % parallel) * 0.3, stuckAt: id === stuck ? Math.min(2, ops.length - 1) : -1, paused: 0, from: 0 }, played: 0,
    };
  });
  for (const id of who.filter((n) => !ready.includes(n))) {
    run.devices[id] = {
      id, label: `#${id}`, serial: '', state: 'failed', step: 0, steps: ops.length, step_label: '', message: '', error: `Android ${id} is not running`, waiting: null,
      done: 0, skipped: 0, recovered: 0, issues: [], dry_run: dryRun, elapsed: 0, log: [], timeline: null, played: 0,
    };
  }
  auto().runs.unshift(run);
  auto().runs.length = Math.min(auto().runs.length, 12);
  tickRuns(t);
  return task;
}

const FINAL = new Set(['done', 'failed', 'cancelled']);

function tickRuns(t) {
  let changed = false;
  for (const run of auto().runs) {
    const task = findTask(run.id);
    if (!task || !ACTIVE.has(task.state)) continue;
    for (const d of Object.values(run.devices)) {
      if (FINAL.has(d.state) || !d.timeline) continue;
      const line = d.timeline;
      if (t < line.start) continue;
      if (d.state === 'needs_attention') continue;
      const reached = Math.min(run.ops.length, line.from + Math.floor((t - line.start) / STEP_SECONDS));
      let next = d.played;
      while (next < reached) {
        if (next === line.stuckAt && !line.answered) {
          const rec = db.instances[d.id];
          if (rec) { device.ui(rec).dialog = RATE_DIALOG; device.ui(rec).rev += 1; }
          d.state = 'needs_attention';
          d.message = 'a dialog this flow does not know is in front';
          d.waiting = { what: `step ${next + 1}: ${run.ops[next].describe}`, reason: `a dialog this flow does not know is in front ("${RATE_DIALOG}")`, step: next, since: t };
          d.log.push(`needs attention: ${d.waiting.what}`);
          break;
        }
        play(run, d.id, run.ops[next]);
        d.log.push(run.ops[next].describe);
        next += 1;
      }
      if (next !== d.played || d.state === 'queued' || d.state === 'needs_attention') changed = true;
      d.played = next;
      d.done = next - d.skipped;
      d.step = Math.min(next, run.ops.length - 1);
      d.step_label = run.ops[d.step] ? run.ops[d.step].describe : '';
      d.elapsed = round(t - line.start);
      if (d.state !== 'needs_attention') d.state = next >= run.ops.length ? 'done' : 'running';
      if (d.state === 'done' && run.dry_run) d.message = 'practice run: nothing was sent';
      if (d.state === 'done' && d.skipped) d.message = `finished with ${d.skipped} skipped step(s)`;
    }
    const all = Object.values(run.devices);
    const finished = all.filter((d) => FINAL.has(d.state));
    const waiting = all.filter((d) => d.state === 'needs_attention').length;
    for (const d of finished) if (!task.results[d.id]) { task.results[d.id] = { ok: d.state === 'done', detail: d.state === 'done' ? (d.message || 'done') : (d.error || d.state) }; changed = true; }
    task.current = finished.length;
    task.message = `${finished.length}/${all.length}${waiting ? `, ${waiting} waiting for you` : ''}`;
    if (finished.length === all.length) {
      const good = finished.filter((d) => d.state === 'done').length;
      task.state = run.cancelled ? 'cancelled' : 'done';
      task.finished = t;
      task.hold = false;
      task.message = `${good} of ${all.length} finished`;
      task.output = { ok: good, failed: all.length - good, devices: Object.fromEntries(all.map((d) => [d.id, d.state])) };
      changed = true;
    }
  }
  return changed;
}
onTick(tickRuns);

// Cancelling the task from the task drawer ends its run as well.
registerEffect('taskCancelled', (task) => {
  const run = auto().runs.find((entry) => entry.id === task.id);
  if (!run) return;
  run.cancelled = true;
  for (const d of Object.values(run.devices)) if (!FINAL.has(d.state)) { d.state = 'cancelled'; d.message = 'cancelled'; d.waiting = null; clearDialog(d.id); }
});

function clearDialog(id) {
  const rec = db.instances[id];
  if (rec && rec.ui && rec.ui.dialog) { rec.ui.dialog = ''; rec.ui.rev += 1; }
}

function runStatus(run) {
  const task = findTask(run.id);
  const devices = Object.fromEntries(Object.keys(run.devices).map((id) => [id, snapshot(run, id)]));
  const counts = {};
  for (const d of Object.values(devices)) counts[d.state] = (counts[d.state] || 0) + 1;
  return {
    id: run.id, kind: run.kind, name: run.name, dry_run: run.dry_run, created: run.created, state: task ? task.state : (run.state || 'done'), counts, devices,
    waiting: Object.values(devices).filter((d) => d.waiting).map((d) => d.id),
  };
}
function findRun(id) {
  const run = auto().runs.find((entry) => entry.id === id);
  if (!run) throw notFound(`no automation run '${String(id).slice(0, 40)}'`);
  return run;
}

route('POST', `${A}/runs`, ({ body }) => {
  if (!['program', 'flow'].includes(body.kind)) throw new ApiError(422, 'validation_error', "kind: Input should be 'program' or 'flow'");
  if (typeof body.name !== 'string' || !body.name) throw new ApiError(422, 'validation_error', 'name: Field required');
  if (typeof body.selector !== 'string' || !body.selector) throw new ApiError(422, 'validation_error', 'selector: Field required');
  const who = select(body.selector);
  if (!who.length) throw bad('the selector matches no Android');
  if (who.length > 40) throw bad('the demo runs automation on at most 40 Androids at a time');
  const busy = auto().runs.filter((run) => { const task = findTask(run.id); return task && ACTIVE.has(task.state); }).flatMap((run) => Object.values(run.devices).filter((d) => !FINAL.has(d.state)).map((d) => d.id));
  const taken = who.filter((id) => busy.includes(id));
  if (taken.length) throw conflict(`Android ${taken[0]} is busy in another automation run; wait for it or cancel it`, 'busy');
  return accepted(startRun(body.kind, body.name, who, { variables: body.variables && typeof body.variables === 'object' ? body.variables : {}, dryRun: Boolean(body.dry_run) }));
});
route('GET', `${A}/runs`, ({ query }) => {
  const list = auto().runs.map(runStatus);
  return { runs: truthy(query.active) ? list.filter((run) => ACTIVE.has(run.state)) : list };
});
route('GET', `${A}/runs/{task_id}`, ({ params }) => ({ run: runStatus(findRun(params.task_id)) }));
route('POST', `${A}/runs/{task_id}/cancel`, ({ params, body }) => {
  const run = findRun(params.task_id);
  const task = findTask(run.id);
  if (!task || !ACTIVE.has(task.state)) throw notFound('no such run (or it has finished)');
  const only = body.id == null ? null : Number(body.id);
  for (const d of Object.values(run.devices)) {
    if ((only == null || d.id === only) && !FINAL.has(d.state)) { d.state = 'cancelled'; d.message = 'cancelled'; d.waiting = null; clearDialog(d.id); }
  }
  if (only == null) run.cancelled = true;
  tickRuns(now());
  return ok();
});
route('POST', `${A}/runs/{task_id}/resolve`, ({ params, body }) => {
  const run = findRun(params.task_id);
  const d = run.devices[Number(body.id)];
  if (!d || d.state !== 'needs_attention') throw notFound('no such device waiting for attention in that run');
  const action = body.action || 'resume';
  if (!['resume', 'skip', 'abort'].includes(action)) throw new ApiError(422, 'validation_error', "action: Input should be 'resume', 'skip' or 'abort'");
  const t = now();
  clearDialog(d.id);
  d.waiting = null;
  d.message = '';
  d.log.push(`operator chose: ${action}`);
  if (action === 'abort') {
    d.state = 'failed';
    d.error = `stopped by the operator at step ${d.played + 1}`;
  } else {
    if (action === 'skip') { d.skipped += 1; d.played += 1; d.issues.push(`skipped: ${run.ops[d.played - 1].describe}`); } else d.recovered += 1;
    d.timeline = { ...d.timeline, start: t, from: d.played, answered: true };
    d.state = 'running';
  }
  tickRuns(t);
  return ok();
});

// ---------------------------------------------------------------- schedules
function nextRun(schedule, t) {
  if (schedule.every) return t + schedule.every;
  const [minute, hour] = schedule.cron.split(/\s+/);
  const d = new Date(t * 1000);
  d.setSeconds(0, 0);
  if (/^\d+$/.test(minute) && /^\d+$/.test(hour)) {
    d.setHours(Number(hour), Number(minute));
    if (d.getTime() / 1000 <= t) d.setDate(d.getDate() + 1);
  } else if (/^\*\/\d+$/.test(minute)) {
    const step = Math.max(1, Number(minute.slice(2)));
    d.setMinutes(Math.floor(d.getMinutes() / step) * step + step);
  } else if (/^\d+$/.test(minute)) {
    d.setMinutes(Number(minute));
    if (d.getTime() / 1000 <= t) d.setHours(d.getHours() + 1);
  } else d.setMinutes(d.getMinutes() + 1);
  return d.getTime() / 1000;
}
function checkSchedule(s) {
  if (!['program', 'flow'].includes(s.kind)) throw new ApiError(422, 'validation_error', "kind: Input should be 'program' or 'flow'");
  for (const field of ['name', 'target', 'selector']) if (typeof s[field] !== 'string' || !s[field]) throw new ApiError(422, 'validation_error', `${field}: Field required`, { detail: [{ field, message: 'Field required' }] });
  if (Boolean(s.every) === Boolean(s.cron)) throw bad('a schedule runs `every` N seconds or on a `cron` line: give exactly one');
  if (s.every && (!Number.isInteger(s.every) || s.every < 60)) throw bad('`every` is at least 60 seconds');
  if (s.cron && s.cron.trim().split(/\s+/).length !== 5) throw bad('a cron line has five fields: minute hour day-of-month month day-of-week');
  if (Object.keys(s.variables || {}).some(looksSecret)) throw bad('a schedule is written to disk, so it cannot hold a secret-named value');
  select(s.selector);
  opsOf(s.kind, s.target);
}
route('GET', `${A}/schedules`, () => ({ schedules: auto().schedules }));
route('POST', `${A}/schedules`, ({ body }) => {
  const t = now();
  const made = {
    id: hex(12), name: body.name, kind: body.kind, target: body.target, selector: body.selector, every: body.every || 0, cron: (body.cron || '').trim(), variables: body.variables || {},
    dry_run: Boolean(body.dry_run), enabled: body.enabled !== false, created: t, next_run: 0, last_run: 0, last_task: '', last_result: '', runs: 0, skipped: 0,
  };
  checkSchedule(made);
  if (auto().schedules.length >= 20) throw bad('the demo keeps at most 20 schedules');
  made.next_run = nextRun(made, t);
  auto().schedules.push(made);
  return created({ schedule: made });
});
route('PATCH', `${A}/schedules/{schedule_id}`, ({ params, body }) => {
  const found = auto().schedules.find((s) => s.id === params.schedule_id);
  if (!found) throw notFound('no such schedule');
  const next = { ...found };
  for (const [key, value] of Object.entries(body)) if (value != null && key in found && !['id', 'created', 'runs'].includes(key)) next[key] = value;
  if (body.every) next.cron = '';
  if (body.cron) next.every = 0;
  checkSchedule(next);
  next.next_run = nextRun(next, now());
  Object.assign(found, next);
  return { schedule: found };
});
route('DELETE', `${A}/schedules/{schedule_id}`, ({ params }) => {
  if (!auto().schedules.some((s) => s.id === params.schedule_id)) throw notFound('no such schedule');
  auto().schedules = auto().schedules.filter((s) => s.id !== params.schedule_id);
  return ok();
});
// A schedule that comes due while the tab is open really starts its run.
onTick((t) => {
  let changed = false;
  for (const s of auto().schedules) {
    if (!s.enabled || !s.next_run || s.next_run > t) continue;
    s.next_run = nextRun(s, t);
    s.last_run = t;
    try {
      const who = select(s.selector).filter((id) => db.instances[id].phase === 'booted');
      if (!who.length) { s.skipped += 1; s.last_result = 'skipped: no Android of the selection was running'; } else {
        const task = startRun(s.kind, s.target, who.slice(0, 40), { variables: s.variables, dryRun: s.dry_run });
        s.last_task = task.id;
        s.last_result = `started on ${who.length} Android(s)`;
        s.runs += 1;
      }
    } catch (error) {
      s.last_result = error.message;
    }
    changed = true;
  }
  return changed;
});

// ------------------------------------------------------------ screen / llm
// One element of the drawn screen, with the fields the real UI reader reports.
function describe(rec, e, index) {
  const front = device.foreground(rec);
  return {
    resource_id: `${front.package}:id/${e.id}`, id: e.id, text: e.text || '', desc: '', cls: e.cls, full_class: `android.widget.${e.cls}`, package: front.package, index, depth: 1,
    path: `/FrameLayout[0]/${e.cls}[${index}]`, bounds: e.bounds, center: [Math.round((e.bounds[0] + e.bounds[2]) / 2), Math.round((e.bounds[1] + e.bounds[3]) / 2)],
    clickable: true, long_clickable: false, scrollable: false, checkable: e.cls === 'Switch', checked: Boolean(e.checked), selected: false, password: false,
    editable: Boolean(e.editable), focused: Boolean(e.focused), enabled: true,
  };
}
route('GET', `${A}/screen/{id}`, ({ params }) => {
  const rec = record(params.id);
  if (rec.phase !== 'booted') throw new ApiError(409, 'device_unavailable', `Android ${rec.spec.id} is not running`);
  const front = device.foreground(rec);
  const list = device.elements(rec);
  return {
    instance: rec.spec.id, package: front.package, activity: front.activity.split('.').slice(-1)[0], size: [device.W, device.H], nodes: list.map((e, index) => describe(rec, e, index)),
    elements: list.map((e, n) => ({ n, id: e.id, text: e.text || '', desc: '', cls: e.cls, package: front.package, bounds: e.bounds, clickable: true, editable: Boolean(e.editable),
      focused: Boolean(e.focused), selector: e.text ? { id: e.id, text: e.text } : { id: e.id } })),
  };
});
route('GET', `${A}/screen/{id}/element`, ({ params, query }) => {
  const rec = record(params.id);
  const x = Number(query.x);
  const y = Number(query.y);
  if (!Number.isFinite(x) || !Number.isFinite(y)) throw new ApiError(422, 'validation_error', 'x: Input should be a valid integer');
  const list = rec.phase === 'booted' ? device.elements(rec) : [];
  const hit = rec.phase === 'booted' ? device.elementAt(rec, x, y) : null;
  if (hit) return { element: describe(rec, hit, list.indexOf(list.find((e) => e.id === hit.id))) };
  return { element: { resource_id: '', id: '', text: '', desc: '', cls: '', path: '', index: 0, package: '', clickable: false, editable: false, scrollable: false, password: false, bounds: [0, 0, 0, 0] } };
});
route('GET', `${A}/llm`, () => ({ provider: 'none', ok: true, detail: 'no model configured; automation is deterministic' }));

