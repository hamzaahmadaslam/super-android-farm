// Model profiles, the Assistant and the Pilot: a scripted stand-in, never a model.
//
// The demo is not connected to any language model and makes no connection. So that the
// Assistant and AI Pilot pages can be tried, this file plays a script instead of a model:
//
//   - one seeded profile, "demo-script", whose Test reports that it is a scripted stand-in;
//   - an Assistant whose turns are picked by a small keyword matcher and carried out on
//     the demo's own pretend Androids, through the same approvals inbox as any assistant;
//   - a Pilot whose runs are fixed lists of actions played on the drawn device screens.
//
// Every conversation opens with a notice that says so, and every answer starts with
// "Scripted demo answer". Events have the shapes superfarm/ai/assistant.py writes and
// sessions the shapes superfarm/ai/pilot.py reports, because the real pages draw them.
//
// Nothing runs on a timer. A turn or a run is data (what comes next and when it is due) in
// db.ai, and onTick() applies whatever is due, so a reload or a long pause loses nothing.

import tools from '../generated/tools.js';
import { accepted, created, handle, need, route, truthy } from '../backend.js';
import * as device from '../device.js';
import { ACTIVE, ApiError, bad, cancelTask, capacity, conflict, doctorChecks, findTask, ids, notFound, onTick, record, registerEffect, select, submit, summary } from '../farm.js';
import { db, emit, hex, isoLocal, now, pad, round, slug as makeSlug } from '../store.js';

const A = '/api/ai';
const SCRIPT = 'demo-script';
const PACKAGE = /^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$/;
const MAX_EVENTS = 400;
const LABEL = 'Scripted demo answer. ';

const store = () => {
  if (!db.ai) db.ai = { conversations: [], sessions: [], held: {}, seq: 0 };
  return db.ai;
};
const oneLine = (value, limit) => String(value ?? '').replace(/\s+/g, ' ').trim().slice(0, limit);
const auto = () => db.automation;
const sortNumbers = (list) => [...list].sort((a, b) => a - b);
const invalid = (field, message) => new ApiError(422, 'validation_error', `${field}: ${message}`, { detail: [{ field, message }] });

// ------------------------------------------------------------------ profiles
function models() {
  const ai = db.config.ai;
  const names = Object.keys(ai.models || {}).sort();
  const chosen = ai.default_model && ai.models[ai.default_model] ? ai.default_model : names.length === 1 ? names[0] : '';
  return names.map((name) => {
    const profile = ai.models[name];
    return {
      name, provider: name === SCRIPT ? 'scripted stand-in' : profile.provider, base_url: profile.base_url, model: profile.model, vision: profile.vision, tools: profile.tools, timeout: profile.timeout,
      default: name === chosen, implicit: false, api_key_secret: profile.api_key_secret, key_set: profile.api_key_secret ? false : null,
    };
  });
}

// The profile a request names, or the default; the refusals are the real ones.
function pickModel(requested) {
  const list = models();
  if (!list.length) throw new ApiError(400, 'model_not_configured', 'no model is configured: add a profile under ai.models (see docs/AI.md)');
  const name = requested || (list.find((entry) => entry.default) || {}).name || '';
  if (!name) throw new ApiError(400, 'model_not_configured', 'several model profiles exist and none is the default: name one, or set ai.default_model');
  if (!db.config.ai.models[name]) throw new ApiError(400, 'model_not_configured', `no model profile named '${String(name).slice(0, 32)}'`);
  return name;
}

route('GET', `${A}/models`, () => ({ models: models(), default: db.config.ai.default_model }));
route('POST', `${A}/models/{name}/test`, ({ params }) => {
  const model = models().find((entry) => entry.name === params.name);
  if (!model) throw new ApiError(400, 'model_not_configured', `no model profile named '${params.name.slice(0, 32)}'`);
  if (model.name === SCRIPT) {
    const check = (id, ms, detail) => ({ id, ok: true, kind: '', ms, detail });
    return {
      probe: {
        name: model.name, provider: model.provider, base_url: model.base_url, model: model.model, ok: true, tools: true, vision: null, available: [model.model], seconds: 0,
        checks: [
          check('reachable', 2, 'Nothing was contacted: demo-script is a scripted stand-in that lives in this page. There is no server behind it.'),
          check('model', 1, 'There is no model. A keyword matcher picks one of a few fixed scripts.'),
          check('reply', 1, 'It answers with fixed sentences and the demo\'s own numbers, not with generated text.'),
          check('tools', 3, 'The script "calls" the farm\'s tools by keyword and the demo carries them out; no function-calling model is involved.'),
        ],
      },
    };
  }
  return {
    probe: {
      ok: false, provider: model.provider, base_url: model.base_url, model: model.model, seconds: 0, available: [],
      checks: [{ id: 'reach', ok: false, kind: 'unreachable', ms: 0, detail: 'The demo never opens a connection, so this server was not contacted. On a real farm this check calls the address above.' }],
    },
  };
});

// -------------------------------------------------------------- the farm's words
const TOOL = Object.fromEntries(tools.map((tool) => [tool.name, tool]));

// The operator's policy applied to one tool: the same rules as the real decision (approvals.js).
function decide(tool) {
  const ai = db.config.ai;
  if (ai.deny.includes(tool.name)) return ['deny', 'this tool is switched off for AI assistants on this farm (ai.deny)'];
  if (tool.class === 'read') return ['allow', 'reading needs no approval'];
  if (ai.policy === 'read-only') return ['deny', "the farm's AI policy is read-only (ai.policy): assistants may look, not change"];
  if (ai.policy === 'allow') return ['allow', 'ai.policy is allow'];
  if (ai.policy !== 'ask') return ['deny', `unknown AI policy '${ai.policy}'`];
  if (ai.auto_approve.includes(tool.name)) return ['allow', 'pre-approved by the operator (ai.auto_approve)'];
  if (tool.class === 'destructive' || ai.ask_for.includes(tool.class)) return ['ask', 'the operator decides'];
  return ['allow', `ai.ask_for does not include ${tool.class} actions`];
}

// "Android 3", "3 Androids: Android 1, 2, 3": ids only, as the real approval line words them.
function who(list) {
  const sorted = sortNumbers(list);
  const tail = sorted.length < 2 ? '' : `: Android ${sorted.slice(0, 12).join(', ')}${sorted.length > 12 ? ` and ${sorted.length - 12} more` : ''}`;
  return { noun: sorted.length === 1 ? `Android ${sorted[0]}` : `${sorted.length} Androids`, tail, one: sorted.length === 1 };
}
const LINES = {
  start_androids: (w) => `Start ${w.noun}${w.tail}`,
  stop_androids: (w) => `Stop ${w.noun}${w.tail}`,
  restart_androids: (w) => `Restart ${w.noun}${w.tail}`,
  delete_androids: (w) => `Delete ${w.noun} and ${w.one ? 'its' : 'their'} data${w.tail}`,
};

// The text of farm_status, from the demo's own numbers (superfarm/ai/tools.py writes it the same way).
function farmStatus() {
  const rows = ids().map((id) => summary(db.instances[id]));
  const counts = {};
  for (const row of rows) counts[row.state] = (counts[row.state] || 0) + 1;
  const cap = capacity();
  const estimate = cap.estimate || {};
  const alerts = doctorChecks().filter((check) => check.status === 'warn' || check.status === 'fail');
  const byState = Object.keys(counts).sort().map((state) => `${counts[state]} ${state}`).join(', ') || 'none yet';
  const text = [
    `${rows.length} Android(s): ${byState}.`,
    `Capacity: ${cap.running} running of about ${estimate.max_instances} estimated (limited by ${estimate.binding}); room for ${cap.headroom} more.`
      + (cap.over ? ` OVER CAPACITY: ${cap.reasons.slice(0, 3).join('; ')}` : '') + (cap.enforced ? ' The estimate is enforced: starts over it are refused unless forced.' : ' The estimate is advisory.'),
    ...alerts.map((alert) => `${alert.status.toUpperCase()} ${alert.title}: ${oneLine(alert.detail, 240)}`),
  ];
  if (!alerts.length) text.push('No host alerts.');
  return {
    text: text.join('\n'),
    facts: {
      total: rows.length, byState, failed: rows.filter((row) => row.state === 'error').map((row) => row.id), running: cap.running, max: estimate.max_instances,
      binding: estimate.binding, room: cap.headroom, over: cap.over, alerts: alerts.map((alert) => alert.title),
    },
  };
}

function statusAnswer(f) {
  return [
    `${LABEL}Built from the demo's own numbers: ${f.total} Androids exist (${f.byState}).`,
    `${f.running} ${f.running === 1 ? 'is' : 'are'} running; this machine is estimated to hold about ${f.max} at once (limited by ${f.binding}), so there is room for ${f.room} more${f.over ? ', but it is over its estimate now' : ''}.`,
    f.failed.length ? `Failed to start: ${f.failed.map((id) => `Android ${id}`).join(', ')}.` : 'None is in an error state.',
    f.alerts.length ? `Host checks: ${f.alerts.length} to look at (${f.alerts.join('; ')}).` : 'The host checks show nothing to fix.',
  ].join(' ');
}

// One line, written by the farm, saying what really ran in a turn (superfarm/ai/assistant.py _record).
function farmRecord(actions) {
  if (!actions.length) return 'Farm record: no action that changes anything ran in this turn.';
  const done = actions.filter((a) => a.ok);
  const failed = actions.filter((a) => !a.ok);
  const parts = [];
  if (done.length) parts.push(`${done.length} action(s) ran: ${done.slice(0, 6).map((a) => a.summary || a.tool).join('; ')}`);
  if (failed.length) parts.push(`${failed.length} did not run or failed: ${failed.slice(0, 6).map((a) => `${a.summary || a.tool} (${a.status})`).join('; ')}`);
  return `Farm record: ${parts.join('. ')}.`;
}

function taskText(task) {
  const results = Object.entries(task.results).sort((a, b) => Number(a[0]) - Number(b[0]));
  const failed = results.filter(([, r]) => !r.ok).length;
  const title = `Task ${task.id} (${task.title})`;
  const head = task.state === 'done' ? `${title} finished: ${results.length - failed} ok, ${failed} failed.` : `${title} ended as ${task.state}: ${results.length - failed} ok, ${failed} failed.`;
  return {
    ok: task.state === 'done' && failed === 0,
    text: [head, ...results.map(([id, r]) => `Android ${id}: ${r.ok ? 'ok' : 'FAILED'}${r.detail ? ` - ${oneLine(r.detail, 300)}` : ''}`)].join('\n'),
  };
}

// ----------------------------------------------------------------- assistant
const NOTICE = 'This is the demo assistant, and it is scripted: no model is running, and the demo is not connected to any language model, whichever profile is chosen. Nothing you type leaves this browser tab. '
  + 'It matches keywords in your message and works on the demo\'s own pretend Androids; anything that changes them still waits for your Yes or No.';
const HELP = `${LABEL}I am a scripted stand-in, not a language model, so I only follow a few phrases. Try: "how many Androids are running and how many can this machine run?", "stop android 1", `
  + '"restart tag:eu", "start all stopped", "create 2 androids", or "delete android 6".';
const FILLER = /^(?:(?:please|pls|kindly|now|just|then|could you|can you|would you|will you|go ahead and),?\s+)+/;
const STATUS = /\b(status|capacity|how many|room|overview|running|failed|error|healthy|host|doctor|set up|machine|fit)\b/;
const NUMBER_WORDS = { a: 1, an: 1, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10 };
const MAX_CREATE = 10;

function findConversation(id) {
  const found = /^[a-f0-9]{12}$/.test(String(id)) ? store().conversations.find((c) => c.id === id) : null;
  if (!found) throw notFound('no such conversation');
  return found;
}

function publicConversation(c) {
  return {
    id: c.id, title: c.title, model: c.model, state: c.state, error: c.error, created: c.created, updated: c.updated, task: c.task || null, events: c.count,
    pending_approvals: [...c.pending], running_tool: c.running_tool, usage: { ...c.usage },
  };
}

function note(conv, type, fields, at) {
  conv.count += 1;
  const event = { n: conv.count, at: round(at, 2), type, ...fields };
  conv.events.push(event);
  if (conv.events.length > MAX_EVENTS) conv.events.shift();
  conv.updated = event.at;
  return event;
}

// What the script reads out of the Androids named in a message: names, tag:x, 1-3, "all",
// "stopped", "running", or numbers. `missing` holds numbers that name no Android.
function parseTargets(rest) {
  let text = ` ${rest.toLowerCase()} `;
  const picked = new Set();
  const missing = [];
  const named = Object.values(db.instances).map((rec) => [rec.spec.name.toLowerCase(), rec.spec.id]).filter(([name]) => name).sort((a, b) => b[0].length - a[0].length);
  for (const [name, id] of named) {
    if (text.includes(name)) { picked.add(id); text = text.replace(name, ' '); }
  }
  for (const m of text.matchAll(/tag:([a-z0-9_-]+)/g)) for (const id of select(`tag:${m[1]}`)) picked.add(id);
  text = text.replace(/tag:[a-z0-9_-]+/g, ' ');
  for (const m of text.matchAll(/(\d+)\s*(?:-|to|through)\s*(\d+)/g)) {
    const [low, high] = [Number(m[1]), Number(m[2])].sort((a, b) => a - b);
    for (const id of ids()) if (id >= low && id <= high) picked.add(id);
  }
  text = text.replace(/(\d+)\s*(?:-|to|through)\s*(\d+)/g, ' ');
  const state = /\bstopped\b/.test(text) ? 'stopped' : /\b(running|booted)\b/.test(text) ? 'running' : '';
  if (state) for (const id of select(state)) picked.add(id);
  else if (/\b(all|every|everything)\b/.test(text)) for (const id of ids()) picked.add(id);
  for (const m of text.matchAll(/\b(\d+)\b/g)) {
    const id = Number(m[1]);
    if (db.instances[id]) picked.add(id); else missing.push(id);
  }
  return { ids: sortNumbers(picked), missing, state };
}

// The turn a message starts: what the script will do, as plain data.
function understand(raw) {
  const low = oneLine(raw, 400).toLowerCase().replace(FILLER, '');
  const answer = (text) => ({ kind: 'answer', answer: `${LABEL}${text}` });
  const verbs = [
    ['delete_androids', /^(?:delete|remove|destroy|erase)\b(.*)$/], ['restart_androids', /^(?:restart|reboot)\b(.*)$/],
    ['stop_androids', /^(?:stop|shut ?down|power off|halt)\b(.*)$/], ['start_androids', /^(?:start|boot|power on|turn on)\b(.*)$/],
  ];
  const make = /^(?:create|add|make|new)\b(.*)$/.exec(low);
  if (make && /\bandroids?\b/.test(make[1])) {
    const word = /\b(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\b/.exec(make[1]);
    const count = word ? (NUMBER_WORDS[word[1]] ?? Number(word[1])) : 1;
    if (count < 1 || count > MAX_CREATE) return answer(`The scripted assistant creates between 1 and ${MAX_CREATE} Androids at a time. Ask for a number in that range.`);
    return { kind: 'change', tool: 'create_androids', ids: [], args: { count }, summary: count === 1 ? 'Create 1 new Android' : `Create ${count} new Androids` };
  }
  for (const [tool, pattern] of verbs) {
    const m = pattern.exec(low);
    if (!m) continue;
    const found = parseTargets(m[1]);
    if (found.missing.length) return answer(`There is no Android ${found.missing[0]}. Name one that exists (see the Androids page), a range like 1-3, "all", or a tag like tag:eu.`);
    if (!found.ids.length) {
      return answer(found.state ? `No Android is ${found.state} right now, so there is nothing to do.`
        : 'Which Android? Name it, for example "stop android 1", a range like 1-3, "all", "stopped", or a tag like tag:eu.');
    }
    return { kind: 'change', tool, ids: found.ids, args: { selector: found.ids.join(',') }, summary: LINES[tool](who(found.ids)) };
  }
  if (STATUS.test(low)) return { kind: 'status', tool: 'farm_status', ids: [], args: {}, summary: TOOL.farm_status.title };
  return { kind: 'other' };
}

function startTurn(text, t) {
  const turn = { ...understand(text), call: `call_${hex(6)}`, phase: 'say', due: t + 0.8, actions: [], approval: '', task: '', approved: false, result: null };
  if (turn.kind === 'answer' || turn.kind === 'other') turn.phase = 'final';
  return turn;
}

function modelCall(conv, extra = 0) {
  const u = conv.usage;
  u.model_calls += 1;
  u.tokens_in += 640 + 85 * conv.turns + 70 * u.tool_calls + Math.round(extra * 0.2);
  u.tokens_out += 28 + ((u.model_calls * 17) % 41) + Math.round(extra * 0.1);
}

const approvalOf = (turn) => db.approvals.find((item) => item.id === turn.approval);
const sameIds = (a, b) => a.length === b.length && sortNumbers(a).every((value, index) => value === sortNumbers(b)[index]);
const BULK = { start_androids: 'start', stop_androids: 'stop', restart_androids: 'restart', delete_androids: 'delete' };
// The tools the approvals page carries out by itself the moment they are approved (approvals.js).
const CARRIED = new Set(['stop_androids', 'restart_androids', 'delete_androids']);

function endTurn(conv, turn, text, ended = '') {
  const at = turn.due;
  if (ended) note(conv, 'notice', { text: ended }, at);
  note(conv, 'final', { text, ended, actions: turn.actions, record: farmRecord(turn.actions) }, at);
  conv.state = 'idle';
  conv.running_tool = null;
  conv.pending = [];
  conv.turns += 1;
  const task = findTask(conv.task);
  if (task && ACTIVE.has(task.state)) {
    Object.assign(task, { state: 'done', finished: now(), hold: false, message: ended || 'answered', output: { conversation: conv.id, answer: text, ended, actions: turn.actions, usage: { ...conv.usage } } });
  }
  conv.task = '';
  conv.turn = null;
}

function failure(turn, status, text) {
  turn.result = { ok: false, status, text };
  turn.phase = 'result';
}

const PHASE = {
  say(conv, turn) {
    note(conv, 'assistant', {
      text: turn.kind === 'status' ? 'Scripted step: I will read the farm\'s status first.'
        : turn.tool === 'create_androids' ? 'Scripted step: I will ask the farm to create the Androids you named.' : `Scripted step: I will ask the farm to do this: ${turn.summary}.`,
    }, turn.due);
    modelCall(conv, 40);
    turn.phase = 'call';
    turn.due += 0.8;
  },
  call(conv, turn) {
    note(conv, 'tool_call', { call: turn.call, tool: turn.tool, klass: TOOL[turn.tool].class, summary: turn.summary, arguments: turn.args }, turn.due);
    conv.running_tool = { tool: turn.tool, summary: turn.summary, since: turn.due };
    turn.phase = TOOL[turn.tool].class === 'read' ? 'read' : 'gate';
    turn.due += 0.8;
  },
  read(conv, turn) {
    const status = farmStatus();
    turn.facts = status.facts;
    turn.result = { ok: true, status: 'ok', text: status.text };
    turn.phase = 'result';
  },
  gate(conv, turn) {
    const tool = TOOL[turn.tool];
    const [decision, why] = decide(tool);
    if (decision === 'deny') { failure(turn, 'not_allowed', `Not allowed: ${why}.`); return; }
    if (decision === 'allow') { turn.phase = 'run'; return; }
    const t = now();
    const item = {
      id: hex(8), created: t, expires: t + db.config.ai.approval_timeout_seconds, caller: `assistant:${conv.model} (scripted)`, session: conv.id, tool: turn.tool, klass: tool.class,
      summary: turn.summary, arguments: turn.args, targets: turn.ids, can_remember: tool.class === 'change', state: 'pending', decided_by: '', decided_at: 0, reason: '', remembered: false,
    };
    db.approvals.unshift(item);
    db.approvals.length = Math.min(db.approvals.length, 60);
    emit({ type: 'approval', approvals: [item] });
    note(conv, 'approval', { call: turn.call, approval_id: item.id, state: 'pending', summary: item.summary, expires: item.expires }, turn.due);
    conv.pending = [item.id];
    conv.state = 'waiting_approval';
    turn.approval = item.id;
    turn.phase = 'await';
  },
  await(conv, turn, t) {
    const item = approvalOf(turn);
    turn.due = Math.max(turn.due, t) + 0.4;
    conv.pending = [];
    conv.state = 'running';
    if (item && item.state === 'approved') { turn.approved = true; turn.phase = 'run'; return; }
    if (item && item.state === 'denied') failure(turn, 'declined', `The operator declined this request.${item.reason ? ` Reason given: ${item.reason.replace(/[.\s]+$/, '')}.` : ''} Do not try it again unless asked.`);
    else failure(turn, 'no_answer', 'Nobody said Yes in time, so the request expired and nothing ran.');
  },
  run(conv, turn, t) {
    if (turn.tool === 'create_androids') {
      const made = handle('POST', '/api/instances', { count: turn.args.count });
      if (made.status !== 201) { failure(turn, 'refused', made.body.error.message); return; }
      const list = made.body.instances.map((spec) => spec.id);
      const selector = list.length > 1 ? `${list[0]}-${list[list.length - 1]}` : String(list[0]);
      turn.result = { ok: true, status: 'ok', text: `Created ${list.length} Android(s): id ${list.join(', ')}. They are not running yet: start them with start_androids using selector "${selector}".` };
      turn.phase = 'result';
      return;
    }
    const verb = BULK[turn.tool];
    if (turn.approved && CARRIED.has(turn.tool)) {
      // approvals.js has already asked the farm when the request was approved; find that task.
      const item = approvalOf(turn);
      const found = db.tasks.find((task) => task.kind === `instance.${verb}` && task.created >= ((item && item.decided_at) || t) - 2 && sameIds(task.targets, turn.ids));
      if (!found) { failure(turn, 'refused', 'The farm did not take the request (an Android was busy). Nothing changed.'); return; }
      turn.task = found.id;
    } else {
      const sent = handle('POST', '/api/instances/bulk', { action: verb, selector: turn.args.selector });
      if (sent.status !== 202) { failure(turn, 'refused', sent.body.error.message); return; }
      turn.task = sent.body.task.id;
    }
    turn.phase = 'follow';
  },
  follow(conv, turn, t) {
    turn.due = Math.max(turn.due, t);
    const task = findTask(turn.task);
    const view = task ? taskText(task) : { ok: false, text: 'The task is no longer on record.' };
    turn.result = { ok: view.ok, status: view.ok ? 'ok' : 'refused', text: view.text };
    turn.phase = 'result';
  },
  result(conv, turn) {
    const { ok, status, text } = turn.result;
    note(conv, 'tool_result', { call: turn.call, tool: turn.tool, ok, status, text, untrusted: false, images: 0, approval_id: turn.approval || null }, turn.due);
    conv.running_tool = null;
    conv.usage.tool_calls += 1;
    if (TOOL[turn.tool].class !== 'read') turn.actions.push({ tool: turn.tool, klass: TOOL[turn.tool].class, summary: turn.summary, ok, status });
    modelCall(conv, text.length);
    turn.phase = 'final';
    turn.due += 0.9;
  },
  final(conv, turn) {
    let text = HELP;
    if (turn.kind === 'answer') text = turn.answer;
    else if (turn.kind === 'status') text = statusAnswer(turn.facts);
    else if (turn.kind === 'change') {
      const r = turn.result;
      const said = {
        ok: `the request "${turn.summary}" was carried out, and the farm reports success. The line below is the farm's own record of what ran.`,
        declined: `you declined "${turn.summary}", so I did nothing and will not try again unless you ask. Nothing changed.`,
        no_answer: `nobody answered "${turn.summary}" in time, so nothing ran.`,
        not_allowed: `the farm's policy does not allow "${turn.summary}". ${r.text} Nothing changed.`,
      };
      const line = r.ok ? said.ok : said[r.status] || `the farm refused "${turn.summary}": ${r.text} Nothing changed.`;
      text = `${LABEL}${line[0].toUpperCase()}${line.slice(1)}`;
    }
    if (turn.kind === 'answer' || turn.kind === 'other') modelCall(conv, text.length);
    endTurn(conv, turn, text);
  },
};

function ready(turn, t) {
  if (turn.phase === 'await') { const item = approvalOf(turn); return !item || item.state !== 'pending'; }
  if (turn.phase === 'follow') { const task = findTask(turn.task); return !task || !ACTIVE.has(task.state); }
  return turn.due <= t;
}

// A turn that the operator stops (the button, the task drawer, or deleting the conversation).
function stopTurn(conv, t, { silent = false } = {}) {
  const turn = conv.turn;
  if (!turn) return;
  const item = turn.approval ? approvalOf(turn) : null;
  if (item && item.state === 'pending') {
    Object.assign(item, { state: 'expired', decided_by: 'cancelled', decided_at: t, reason: 'the conversation was cancelled' });
    emit({ type: 'approval', approvals: [item] });
  }
  turn.due = Math.max(turn.due, t);
  if (silent) {
    const task = findTask(conv.task);
    if (task && ACTIVE.has(task.state)) Object.assign(task, { state: 'done', finished: t, hold: false, message: 'cancelled' });
    return;
  }
  if (turn.phase === 'result') PHASE.result(conv, turn);
  else if (turn.tool && !turn.result && turn.phase !== 'say' && turn.phase !== 'call') {
    const text = 'Cancelled before the farm finished it.';
    note(conv, 'tool_result', { call: turn.call, tool: turn.tool, ok: false, status: 'cancelled', text, untrusted: false, images: 0, approval_id: turn.approval || null }, turn.due);
    if (TOOL[turn.tool].class !== 'read') turn.actions.push({ tool: turn.tool, klass: TOOL[turn.tool].class, summary: turn.summary, ok: false, status: 'cancelled' });
  }
  endTurn(conv, turn, '', 'cancelled');
}

function tickConversations(t) {
  let changed = false;
  for (const conv of store().conversations) {
    if (!conv.turn) continue;
    const task = findTask(conv.task);
    if (task && task.state === 'cancelled') { stopTurn(conv, t); changed = true; continue; }
    for (let guard = 0; guard < 30 && conv.turn && ready(conv.turn, t); guard++) {
      PHASE[conv.turn.phase](conv, conv.turn, t);
      changed = true;
    }
  }
  return changed;
}

route('GET', `${A}/conversations`, () => ({ conversations: store().conversations.map(publicConversation) }));
route('POST', `${A}/conversations`, ({ body }) => {
  const model = pickModel(typeof body.model === 'string' ? body.model : '');
  const t = now();
  const conv = {
    id: hex(12), title: '', model, state: 'idle', error: '', created: t, updated: t, task: '', count: 0, pending: [], running_tool: null,
    usage: { model_calls: 0, tool_calls: 0, tokens_in: 0, tokens_out: 0 }, events: [], turns: 0, turn: null,
  };
  note(conv, 'notice', { text: NOTICE }, t);
  const list = store().conversations;
  list.unshift(conv);
  const keep = db.config.ai.assistant.keep_conversations;
  for (let at = list.length - 1; at > 0 && list.length > keep; at--) if (list[at].state === 'idle') list.splice(at, 1);
  return created({ conversation: publicConversation(conv) });
});
// The Assistant page asks for "the events after N" from two places at once right after it sends a message
// (the send itself and its polling loop), and draws what each answer holds. A real daemon answers in a few
// milliseconds, so the two rarely overlap; the demo's answers take longer, and the same event would be drawn
// twice. So a request that repeats the `after` of one answered a moment ago is given only what is newer than
// what that one was sent.
const lately = new Map();               // conversation id -> { after, upto, at }
route('GET', `${A}/conversations/{id}`, ({ params, query }) => {
  const conv = findConversation(params.id);
  const after = Math.max(0, Math.floor(Number(query.after) || 0));
  const t = now();
  const mark = lately.get(conv.id);
  const from = mark && mark.after === after && t - mark.at < 0.4 && mark.upto > after ? mark.upto : after;
  if (!mark || from === after || mark.after !== after) lately.set(conv.id, { after, upto: conv.count, at: t });
  else mark.upto = conv.count;
  return { conversation: { ...publicConversation(conv), events: conv.events.filter((event) => event.n > from), last: conv.count } };
});
route('POST', `${A}/conversations/{id}/messages`, ({ params, body }) => {
  const conv = findConversation(params.id);
  const text = need(body, 'text', { max: 8000 });
  const profile = db.config.ai.models[conv.model];
  if (!profile) throw new ApiError(400, 'model_not_configured', `no model profile named '${conv.model.slice(0, 32)}'`);
  if (!profile.tools) throw bad(`the model profile '${conv.model}' has \`tools\` off, and the Assistant works only through tool calls. Use a profile whose model supports tool calling.`);
  if (conv.state !== 'idle') throw conflict('this conversation is still answering the previous message; wait for it or cancel it');
  const t = now();
  conv.state = 'running';
  conv.error = '';
  if (!conv.title) conv.title = oneLine(text, 60);
  note(conv, 'user', { text }, t);
  const task = submit('ai-assistant', `assistant: ${oneLine(text, 60)}`, [], {});
  task.hold = true;
  task.message = 'asking the model';
  conv.task = task.id;
  conv.turn = startTurn(text, t);
  return accepted(task);
});
route('POST', `${A}/conversations/{id}/cancel`, ({ params }) => {
  const conv = findConversation(params.id);
  if (conv.state === 'idle') return { ok: true, cancelled: false };
  stopTurn(conv, now());
  return { ok: true, cancelled: true };
});
route('DELETE', `${A}/conversations/{id}`, ({ params }) => {
  const conv = findConversation(params.id);
  stopTurn(conv, now(), { silent: true });
  store().conversations = store().conversations.filter((c) => c !== conv);
  lately.delete(conv.id);
  return { ok: true };
});

// --------------------------------------------------------------------- pilot
// A session is a task (hold keeps it running until every Android has finished) plus, per
// Android, a queue of steps: { delay, op } means "op happens `delay` seconds after the one
// before it". An Android that waits for a person has `blocked` set, and its queue waits.
const ACTION_GAP = 1.3;
const RUN_GAP = 1.2;
const FIRST_START = 1.2;               // seconds before the first Android leaves "queued"
const RATE_DIALOG = 'Enjoying this app? Rate it';
const kept = new Map();                // session id -> { name: value }: named values stay in memory only, as on a real farm

const findSession = (id) => {
  const found = store().sessions.find((s) => s.id === id);
  if (!found) throw notFound(`no pilot session '${String(id).slice(0, 40)}'`);
  return found;
};
const taskOf = (s) => findTask(s.id);
const isActive = (s) => { const task = taskOf(s); return Boolean(task && ACTIVE.has(task.state)); };
const instanceSerial = (id) => `${db.config.network.adb_bind}:${db.config.network.adb_port_base + id}`;

function parseSuccess(raw) {
  if (raw == null || raw === '') return null;
  let text = raw;
  if (typeof raw === 'object' && !Array.isArray(raw)) {
    const key = ['text', 'id', 'desc', 'regex', 'package', 'activity'].find((name) => typeof raw[name] === 'string' && raw[name]);
    text = key ? `${key}:${raw[key]}` : '';
  }
  const m = typeof text === 'string' ? /^(text|id|desc|regex|package|activity):([\s\S]*)$/.exec(text.trim()) : null;
  const value = m ? m[2].trim() : '';
  if (!m || !value) throw bad('a success check needs an element (text:, id:, desc:, regex:), a package: or an activity:');
  const kind = m[1];
  if (kind === 'package' && !PACKAGE.test(value)) throw bad(`bad package name '${value.slice(0, 60)}': expected something like com.example.app`);
  return {
    selector: kind === 'activity' ? null : { [kind]: value }, activity: kind === 'activity' ? value : '', source: 'operator',
    describe: kind === 'activity' ? `the activity ${value} is in front` : `an element with ${kind}='${value}' is on screen`,
  };
}

function sentence(body, name, low, high) {
  const value = body[name];
  if (typeof value !== 'string' || !value.trim()) throw invalid(name, 'Field required');
  if (value.length < low) throw invalid(name, `String should have at least ${low} characters`);
  if (value.length > high) throw invalid(name, `String should have at most ${high} characters`);
  return value;
}

// The dataset the recordings go to: named after the goal unless the request names one.
function pilotDataset(requested, goal, pkg) {
  const slug = requested || makeSlug(goal);
  if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(slug)) throw bad('the dataset name needs lower-case letters, digits and - (at most 64)');
  const existing = auto().datasets.find((d) => d.slug === slug);
  if (existing) {
    if (existing.package && existing.package !== pkg) throw bad(`the dataset '${slug}' belongs to ${existing.package}, not ${pkg}; name another dataset for this goal`);
    if (!existing.package) existing.package = pkg;
    return slug;
  }
  const stamp = isoLocal(now());
  auto().datasets.push({ slug, name: requested ? slug : oneLine(goal, 120), description: goal, package: pkg, created: stamp, updated: stamp, recordings: [] });
  return slug;
}

// What the goal says to type: a stored value's name, a quoted text, or "called X".
function textFor(goal, values) {
  const names = values && typeof values === 'object' ? Object.keys(values) : [];
  if (names.length) return { named: names[0], text: '' };
  const quoted = /["“'‘]([^"”'’]{1,60})["”'’]/.exec(goal) || /\b(?:called|named|titled)\s+(.{1,60}?)\s*$/i.exec(goal);
  return { named: '', text: quoted ? quoted[1].trim() : 'Pilot note' };
}

const tapStep = (id, label, reason, outcome) => ({
  action: 'tap', target: `'${label}'`, reason, outcome, play: { k: 'tap', id }, rec: { kind: 'tap', describe: `tap '${label}'`, text: label, target: id },
});

// The fixed list of model actions of one run: 5 to 7, differing a little from run to run,
// so that training has something to agree and disagree on.
function scriptFor(s, index) {
  const wait = (reason) => ({ action: 'wait', target: '1.0s', reason, outcome: 'nothing changed', play: { k: 'wait' }, rec: null });
  const scroll = (reason) => ({ action: 'scroll', target: 'down', reason, outcome: 'nothing changed: that is the end, there is nothing further that way', play: { k: 'swipe' }, rec: null });
  if (s.package === 'example.settings') {
    return [
      wait('The app has just opened; give its first screen a moment to load.'),
      scroll('Looking for the entry the goal is about, further down the list.'),
      tapStep('about', 'About this device', 'This entry is the closest match to the goal.', 'a new screen'),
      { action: 'key', target: 'back', reason: 'Going back to the list to confirm where the app returns to.', outcome: 'back on a screen seen before', play: { k: 'key', key: 'BACK' }, rec: { kind: 'key', describe: 'press back', key: 'BACK' } },
      wait('Let the screen settle before checking the result.'),
    ];
  }
  const title = tapStep('title', 'title field', 'The cursor has to be in the title field before text can be typed.', 'nothing changed');
  title.target = 'the title field';
  return [
    wait('The app has just opened; give its first screen a moment to load.'),
    tapStep('new_note', 'New note', 'The goal needs a note, and this button creates one.', 'a new screen'),
    index % 3 !== 1 ? title : null,
    {
      action: 'type', target: s.named ? `the title field, with the stored value "${s.named}"` : `'${s.text}' into the title field`, reason: 'The goal names the text that belongs in the title.', outcome: 'a new screen',
      play: { k: 'type' }, rec: { kind: 'type', describe: s.named ? `type {${s.named}}` : `type '${s.text}'`, text: s.named ? '' : s.text, name: s.named || null, target: 'title' },
    },
    tapStep('save', 'Save', 'Typed text is only kept once it is saved.', 'a new screen'),
    index % 3 === 2 ? scroll('Checking whether the saved note shows further down the list.') : null,
    wait('Let the list update before checking the result.'),
  ].filter(Boolean);
}

function newRun(s, dev, index, at) {
  const queue = [{ delay: index === 0 ? 0.4 : RUN_GAP, op: { k: 'begin' } }];
  scriptFor(s, index).forEach((step, at_) => {
    if (at_ === 2 && s.stuck === dev.id && index === 0) queue.push({ delay: 1.0, op: { k: 'stuck' } });
    queue.push({ delay: ACTION_GAP, op: { k: 'act', step } });
  });
  queue.push({ delay: 1.0, op: { k: 'end' } });
  dev.queue = queue;
  dev.nextAt = at + queue[0].delay;
  dev.cur = { index, started: dev.nextAt, step: 0, step_label: '', message: '', error: '', actions: [], ops: [], model_calls: 0, tokens_in: 0, tokens_out: 0, recording: '' };
}

function failRun(s, dev, at, error) {
  dev.cur.error = error;
  closeRun(s, dev, 'failed', at);
}

// The run on `dev` is over, however it ended: record it, then go on to the next run or finish the Android.
function closeRun(s, dev, state, at) {
  const cur = dev.cur;
  const verified = state === 'done';
  const good = verified || state === 'unverified';
  const result = {
    label: `#${dev.id}${s.runs > 1 ? ` run ${cur.index + 1}` : ''}`, serial: instanceSerial(dev.id), state, step: cur.step, steps: s.max_steps, step_label: cur.step_label,
    message: cur.message, error: cur.error, waiting: null, done: cur.actions.length, skipped: 0, recovered: 0, issues: [], dry_run: false, elapsed: round(Math.max(0, at - cur.started), 1), log: [],
    goal: s.goal, package: s.package, recording: good ? cur.recording : '', actions: cur.actions, model_calls: cur.model_calls, rejected: 0,
    tokens_in: cur.tokens_in, tokens_out: cur.tokens_out, outcome: good ? state === 'done' ? 'verified' : 'unverified' : '',
    verified_by: verified ? (s.success && s.success.source === 'operator' ? 'operator' : 'model') : '', recorded_steps: cur.ops.length,
  };
  if (good) {
    const recording = {
      id: cur.recording, created: isoLocal(cur.started), device: instanceSerial(dev.id), instance: dev.id, steps: cur.ops.length, taps: cur.ops.filter((op) => op.kind === 'tap').length,
      seconds: result.elapsed, note: 'taught by the scripted demo Pilot', dynamic: s.named ? [s.named] : [], preview: cur.ops.slice(0, 8).map((op) => op.describe), ops: cur.ops,
    };
    const d = auto().datasets.find((entry) => entry.slug === s.dataset);
    if (verified) {
      if (d) { d.recordings.push(recording); d.updated = isoLocal(now()); }
    } else {
      (store().held[s.dataset] = store().held[s.dataset] || []).push(recording);
    }
  }
  dev.results.push(result);
  dev.cur = null;
  dev.queue = [];
  dev.waiting = null;
  dev.blocked = false;
  dev.state = state;
  if (dev.results.length < s.runs && !dev.cancelled) {
    dev.state = 'running';
    newRun(s, dev, dev.results.length, at);
  } else {
    s.freedAt = at;
    finishDevice(s, dev);
  }
}

function finishDevice(s, dev) {
  dev.finished = true;
  const task = taskOf(s);
  if (!task) return;
  const good = dev.results.filter((r) => r.state === 'done').length;
  const last = dev.results[dev.results.length - 1] || {};
  let detail = `${good} of ${s.runs} run(s) verified`;
  if (good < s.runs) detail += `; last: ${last.state || dev.state}: ${last.message || last.error || dev.message}`;
  task.results[dev.id] = { ok: good === s.runs, detail: oneLine(detail, 300) };
}

function clearDialog(id) {
  const rec = db.instances[id];
  if (rec && rec.ui && rec.ui.dialog) { rec.ui.dialog = ''; rec.ui.rev += 1; }
}

function playStep(s, dev, rec, step) {
  const p = step.play;
  if (p.k === 'tap') {
    const hit = device.elements(rec).find((e) => e.id === p.id);
    if (!hit) return `there is no '${p.id}' on the screen of Android ${dev.id} (is a pop-up in front?)`;
    device.tap(rec, (hit.bounds[0] + hit.bounds[2]) / 2 / device.W, (hit.bounds[1] + hit.bounds[3]) / 2 / device.H);
  } else if (p.k === 'type') {
    device.type(rec, s.named ? ((kept.get(s.id) || {})[s.named] ?? `{${s.named}}`) : s.text);
  } else if (p.k === 'key') {
    device.key(rec, p.key);
  } else if (p.k === 'swipe') {
    device.swipe(rec, 0.5, 0.7, 0.5, 0.35);
  }
  return '';
}

// One request to the "model" and what it cost, in the counters a real run reports.
function modelRequest(cur, dev) {
  const seed = (cur.actions.length + 1) * 7 + dev.id;
  cur.model_calls += 1;
  cur.tokens_in += 900 + 120 * cur.actions.length + (seed % 53);
  cur.tokens_out += 34 + (seed % 23);
}

function applyOp(s, dev, op, at) {
  const rec = db.instances[dev.id];
  const cur = dev.cur;
  const up = Boolean(rec) && rec.phase === 'booted';
  if (!up) { failRun(s, dev, at, `Android ${dev.id} is not running`); return; }
  if (op.k === 'begin') {
    cur.started = at;
    cur.recording = `r${pad(++store().seq, 3)}-${isoLocal(at).slice(11).replace(/:/g, '')}`;
    dev.state = 'running';
    if (!device.launch(rec, s.package)) { failRun(s, dev, at, `${s.package} is not installed on Android ${dev.id}`); return; }
    cur.step_label = `launch ${s.package}`;
    cur.ops.push({ kind: 'launch', describe: `launch ${s.package}`, text: '', name: null, package: s.package, key: '', target: '' });
  } else if (op.k === 'stuck') {
    const ui = device.ui(rec);
    ui.dialog = RATE_DIALOG;
    ui.rev += 1;
    const reason = `a pop-up ("${RATE_DIALOG}") is in front, and the scripted Pilot has no action for it`;
    dev.waiting = { what: 'choose the next action', reason, step: cur.actions.length, since: round(at, 3) };
    dev.state = 'needs_attention';
    cur.message = reason;
    dev.blocked = true;
  } else if (op.k === 'act') {
    const problem = playStep(s, dev, rec, op.step);
    if (problem) { failRun(s, dev, at, problem); return; }
    modelRequest(cur, dev);
    cur.actions.push({ n: cur.actions.length + 1, action: op.step.action, target: op.step.target, reason: op.step.reason, outcome: op.step.outcome, at: round(at - cur.started, 1) });
    cur.step = cur.actions.length;
    cur.step_label = `${op.step.action} ${op.step.target}`;
    if (op.step.rec) cur.ops.push({ text: '', name: null, package: '', key: '', target: '', ...op.step.rec });
  } else if (op.k === 'end') {
    modelRequest(cur, dev);
    if (s.unverifiedAt && s.unverifiedAt.id === dev.id && s.unverifiedAt.index === cur.index) {
      cur.message = 'scripted demo run: this one is set to end unverified, so that Confirm can be tried.';
      closeRun(s, dev, 'unverified', at);
      return;
    }
    if (!s.success) s.success = { selector: { text: 'New note' }, activity: '', source: 'model', describe: "an element with text='New note' is on screen" };
    const text = (s.success.selector && s.success.selector.text) || 'New note';
    cur.ops.push({ kind: 'assert', describe: `check '${text}' is visible`, text, name: null, package: '', key: '', target: '' });
    cur.message = `goal reached: ${s.success.describe} (scripted run: counted as verified)`;
    closeRun(s, dev, 'done', at);
  }
}

function tickPilot(t) {
  let changed = false;
  for (const s of store().sessions) {
    const task = taskOf(s);
    if (!task) continue;
    if (!ACTIVE.has(task.state)) {
      if (task.state === 'cancelled' && s.order.some((id) => !s.devices[id].finished)) { cancelAll(s, t); changed = true; }
      continue;
    }
    const max = Math.max(1, db.config.ai.pilot.max_parallel);
    // An Android takes a free slot when its turn has come; one that finishes frees its slot for the next, in the same tick.
    for (let pass = 0, again = true; again && pass < 20; pass++) {
      again = false;
      let live = s.order.filter((id) => { const d = s.devices[id]; return !d.finished && d.state !== 'queued'; }).length;
      for (const [index, id] of s.order.entries()) {
        const dev = s.devices[id];
        const startAt = Math.max(s.created + FIRST_START + 0.4 * index, s.freedAt || 0);
        if (dev.state !== 'queued' || dev.finished || live >= max || t < startAt) continue;
        dev.state = 'running';
        newRun(s, dev, 0, startAt);
        live += 1;
        again = true;
        changed = true;
      }
      for (const id of s.order) {
        const dev = s.devices[id];
        const wasFinished = dev.finished;
        for (let guard = 0; guard < 200 && dev.cur && !dev.blocked && dev.nextAt <= t; guard++) {
          const item = dev.queue.shift();
          const at = dev.nextAt;
          applyOp(s, dev, item.op, at);
          if (dev.cur && !dev.blocked && dev.queue.length) dev.nextAt = at + dev.queue[0].delay;
          changed = true;
        }
        if (dev.finished && !wasFinished) again = true;
      }
    }
    const runs = s.order.reduce((sum, id) => sum + s.devices[id].results.length, 0);
    const total = s.order.length * s.runs;
    const message = `${runs}/${total} runs${s.order.some((id) => s.devices[id].waiting) ? `, ${s.order.filter((id) => s.devices[id].waiting).length} waiting for you` : ''}`;
    if (task.message !== message || task.current !== runs) { task.message = message; task.current = runs; changed = true; }
    if (s.order.every((id) => s.devices[id].finished)) {
      const results = s.order.flatMap((id) => s.devices[id].results);
      const verified = results.filter((r) => r.state === 'done').length;
      const unverified = results.filter((r) => r.state === 'unverified').length;
      Object.assign(task, {
        state: 'done', finished: t, hold: false, message: `${verified} of ${total} runs verified${unverified ? `, ${unverified} unverified` : ''}`,
        output: {
          session: s.id, dataset: s.dataset, runs: total, verified, unverified, failed: total - verified - unverified,
          recordings: results.filter((r) => r.state === 'done').map((r) => r.recording), model_calls: results.reduce((sum, r) => sum + r.model_calls, 0),
        },
      });
      changed = true;
    }
  }
  return changed;
}

function cancelDevice(s, dev, at) {
  if (dev.finished) return;
  dev.cancelled = true;
  clearDialog(dev.id);
  if (dev.cur) {
    dev.cur.message = 'cancelled';
    closeRun(s, dev, 'cancelled', at);
  } else {
    finishDevice(s, dev);
  }
  dev.state = 'cancelled';
  dev.message = 'cancelled before all its runs were made';
  dev.finished = true;
}
function cancelAll(s, at) {
  for (const id of s.order) cancelDevice(s, s.devices[id], at);
}

// ---- how a session looks from outside
const BRIEF = ['state', 'outcome', 'verified_by', 'recording', 'step', 'model_calls', 'rejected', 'elapsed', 'message', 'error', 'recorded_steps', 'tokens_in', 'tokens_out', 'actions'];
const brief = (result) => Object.fromEntries(BRIEF.map((key) => [key, result[key] ?? null]));

function deviceView(s, dev, t) {
  const cur = dev.cur;
  let base;
  if (cur) {
    base = {
      label: `#${dev.id}`, serial: instanceSerial(dev.id), state: dev.state, step: cur.step, steps: s.max_steps, step_label: cur.step_label, message: cur.message, error: cur.error, waiting: dev.waiting,
      done: cur.actions.length, skipped: 0, recovered: 0, issues: [], dry_run: false, elapsed: round(Math.max(0, t - cur.started), 1), log: [], goal: s.goal, package: s.package,
      recording: cur.recording, actions: cur.actions.slice(-60), model_calls: cur.model_calls, rejected: 0, tokens_in: cur.tokens_in, tokens_out: cur.tokens_out, outcome: '', verified_by: '', recorded_steps: cur.ops.length,
    };
  } else {
    base = dev.results[dev.results.length - 1] || { state: dev.state, message: dev.message, step: 0, steps: 0, step_label: '', error: '', waiting: null, actions: [] };
  }
  return { ...base, state: dev.cancelled ? 'cancelled' : base.state, id: dev.id, run: dev.results.length + (cur ? 1 : 0), runs: s.runs, finished: dev.finished, results: dev.results.map(brief) };
}

function sessionView(s) {
  const t = now();
  const task = taskOf(s);
  const devices = Object.fromEntries(s.order.map((id) => [String(id), deviceView(s, s.devices[id], t)]));
  const results = s.order.flatMap((id) => s.devices[id].results);
  const counts = {};
  for (const r of results) counts[r.state] = (counts[r.state] || 0) + 1;
  return {
    id: s.id, goal: s.goal, package: s.package, dataset: s.dataset, model: s.model, success: s.success, runs: s.runs, max_steps: s.max_steps, allowed_packages: s.allowed, created: s.created,
    state: task ? task.state : 'done', counts, devices, waiting: s.order.filter((id) => s.devices[id].waiting),
    recordings: results.filter((r) => r.state === 'done' || r.state === 'confirmed').map((r) => r.recording),
    unverified: s.order.flatMap((id) => s.devices[id].results.filter((r) => r.state === 'unverified').map((r) => ({ instance: id, recording: r.recording }))),
    model_calls: results.reduce((sum, r) => sum + r.model_calls, 0),
  };
}

route('POST', `${A}/pilot`, ({ body }) => {
  const goal = oneLine(sentence(body, 'goal', 3, 600), 600);
  const pkg = sentence(body, 'package', 3, 200).trim();
  sentence(body, 'selector', 1, 500);
  const runs = body.runs == null ? 1 : body.runs;
  if (!Number.isInteger(runs) || runs < 1 || runs > 10) throw invalid('runs', 'Input should be between 1 and 10');
  const steps = body.max_steps == null ? db.config.ai.pilot.max_steps : body.max_steps;
  if (!Number.isInteger(steps) || steps < 1 || steps > 200) throw invalid('max_steps', 'Input should be between 1 and 200');
  if (!PACKAGE.test(pkg)) throw bad(`bad package name '${pkg.slice(0, 60)}': expected something like com.example.app`);
  const success = parseSuccess(body.success);
  const extra = Array.isArray(body.allowed_packages) ? body.allowed_packages.filter((p) => PACKAGE.test(String(p))) : [];
  const allowed = [...new Set([...db.config.ai.pilot.allowed_packages, ...extra])].sort();
  const model = pickModel(typeof body.model === 'string' ? body.model : '');
  const order = select(body.selector);
  if (!order.length) throw bad(`the selector '${oneLine(body.selector, 80)}' matches no instance`);
  const dataset = pilotDataset(typeof body.dataset === 'string' && body.dataset ? body.dataset : '', goal, pkg);
  const typed = textFor(goal, body.values);

  const task = submit('ai-pilot', `pilot: ${oneLine(goal, 60)} on ${order.length} device(s)`, [], { targets: order });
  task.hold = true;
  task.total = order.length * runs;
  task.message = `0/${task.total} runs`;
  const booted = order.filter((id) => db.instances[id].phase === 'booted');
  const s = {
    id: task.id, goal, package: pkg, dataset, model, success, runs, max_steps: steps, allowed, created: task.created, text: typed.text, named: typed.named, order, devices: {}, freedAt: 0,
    // With 3 runs or more in all, the first Android's last run ends unverified; with 2 Androids or more, the second one stops once for a person.
    unverifiedAt: order.length * runs >= 3 ? { id: order[0], index: runs - 1 } : null, stuck: booted.length >= 2 ? booted[1] : 0,
  };
  for (const id of order) s.devices[id] = { id, state: 'queued', message: '', waiting: null, finished: false, cancelled: false, blocked: false, results: [], cur: null, queue: [], nextAt: 0 };
  if (typed.named) kept.set(s.id, { [typed.named]: String(body.values[typed.named] ?? '') });
  const list = store().sessions;
  list.unshift(s);
  for (let at = list.length - 1; at >= 30; at--) if (!isActive(list[at])) list.splice(at, 1);
  return accepted(task);
});
route('GET', `${A}/pilot/sessions`, ({ query }) => ({ sessions: store().sessions.filter((s) => !truthy(query.active) || isActive(s)).map(sessionView) }));
route('GET', `${A}/pilot/sessions/{id}`, ({ params }) => ({ session: sessionView(findSession(params.id)) }));
route('POST', `${A}/pilot/sessions/{id}/cancel`, ({ params, body }) => {
  const s = findSession(params.id);
  const t = now();
  if (body.instance == null) {
    const task = taskOf(s);
    if (task) cancelTask(task);
    cancelAll(s, t);
    return { ok: true, cancelled: true };
  }
  const dev = s.devices[Number(body.instance)];
  if (!dev || dev.finished) return { ok: true, cancelled: false };
  cancelDevice(s, dev, t);
  return { ok: true, cancelled: true };
});
route('POST', `${A}/pilot/sessions/{id}/devices/{instance}/resolve`, ({ params, body }) => {
  const s = findSession(params.id);
  const rec = record(params.instance);
  const action = body.action == null ? 'resume' : body.action;
  if (!['resume', 'skip', 'abort'].includes(action)) throw invalid('action', "Input should be 'resume', 'skip' or 'abort'");
  const dev = s.devices[rec.spec.id];
  if (!dev || !dev.waiting) throw conflict(`Android ${rec.spec.id} is not waiting for an answer in pilot session ${params.id.slice(0, 40)}`);
  const t = now();
  const what = dev.waiting.what;
  clearDialog(dev.id);
  dev.waiting = null;
  dev.cur.message = '';
  dev.state = 'running';
  if (action === 'resume') {
    dev.blocked = false;
    dev.nextAt = t + (dev.queue[0] ? dev.queue[0].delay : 0);
  } else {
    // Skip ends this Android's run and abort stops it; either way the run is discarded and the next run, if any, goes on.
    failRun(s, dev, t, action === 'skip' ? `ended by the operator at: ${what}` : `stopped by the operator at ${what}`);
  }
  return { ok: true };
});
route('POST', `${A}/pilot/sessions/{id}/confirm`, ({ params, body }) => {
  const s = findSession(params.id);
  if (!Number.isInteger(body.instance)) throw invalid('instance', 'Field required');
  const dev = s.devices[body.instance];
  if (!dev) throw notFound(`Android ${body.instance} is not part of pilot session ${s.id}`);
  const pending = dev.results.filter((r) => r.state === 'unverified' && (!body.recording || r.recording === body.recording));
  if (!pending.length) throw bad(`Android ${body.instance} has no unverified run to confirm in this session`);
  const result = pending[pending.length - 1];
  const held = store().held[s.dataset] || [];
  const found = held.find((r) => r.id === result.recording);
  const d = auto().datasets.find((entry) => entry.slug === s.dataset);
  if (!found || !d) throw notFound('the recording of that run is no longer there');
  store().held[s.dataset] = held.filter((r) => r !== found);
  d.recordings.push(found);
  d.updated = isoLocal(now());
  Object.assign(result, { state: 'confirmed', verified_by: 'operator (confirmed afterwards)' });
  return { confirmed: { instance: body.instance, recording: result.recording, state: 'confirmed' } };
});

// ---- training and what the recordings agreed on
// The ordinary trainer (automation.js) builds the program; this adds the Pilot's reading of it.
function pilotSummary(slug) {
  const program = auto().programs.find((p) => p.slug === slug);
  const d = auto().datasets.find((entry) => entry.slug === slug);
  if (!program || !d) return null;
  const total = (program.from_recordings || []).length;
  const outline = program.outline || [];
  const everywhere = outline.filter((step) => !step.optional);
  const partial = outline.filter((step) => step.optional);
  const instances = sortNumbers(new Set(d.recordings.map((r) => r.instance).filter((n) => n != null)));
  const waiting = (store().held[slug] || []).map((r) => r.id).sort();
  const lines = [
    `Built from ${total} recording(s) made on ${instances.length} Android(s)${instances.length ? ` (${instances.join(', ')})` : ''}.`,
    `${everywhere.length} of ${outline.length} step(s) were made in every recording.`,
    ...partial.map((step) => `Step ${step.id} (${step.label}) was made in ${step.seen_in} of ${total} recordings; at replay it is skipped when its element is not there.`),
  ];
  if (waiting.length) lines.push(`${waiting.length} unverified recording(s) were left out; confirm them to include them.`);
  if (total < 3) lines.push('With fewer than three recordings one wrong run cannot be outvoted: run the goal again.');
  return {
    dataset: slug, recordings: total, instances, steps: outline.length, agreed: everywhere.map((step) => step.id), disagreed: partial.map((step) => step.id),
    warnings: [...(program.warnings || [])], unverified: waiting, text: lines, outline: outline.map((step) => ({ id: step.id, kind: step.kind, label: step.label, seen_in: step.seen_in })),
  };
}

registerEffect('pilotSummary', (slug) => {
  const program = auto().programs.find((p) => p.slug === slug);
  const d = auto().datasets.find((entry) => entry.slug === slug);
  if (!program || !d) return { ok: false, detail: 'the dataset or its program was deleted meanwhile' };
  // How many recordings made each step decides which steps are optional at replay.
  const opsOf = (r) => r.ops || (r.preview || []).map((line) => ({ kind: line.split(' ')[0], describe: line }));
  const seen = (step) => d.recordings.filter((r) => opsOf(r).some((op) => op.kind === step.kind && op.describe === step.label)).length;
  for (const step of program.outline) {
    step.seen_in = Math.max(1, seen(step));
    step.optional = step.seen_in < d.recordings.length;
  }
  program.optional = program.outline.filter((step) => step.optional).length;
  program.notes = ['demo: built from the longest recording; how many of the others made each step is counted, the steps are not merged'];
  const { outline, ops, ...programSummary } = program;
  return { output: { program: programSummary, summary: pilotSummary(slug) } };
});

route('POST', `${A}/pilot/{dataset}/train`, ({ params }) => {
  const d = auto().datasets.find((entry) => entry.slug === params.dataset);
  if (!d) throw notFound(`no dataset '${String(params.dataset).slice(0, 64)}'`);
  if (!d.recordings.length) throw bad(`the dataset '${d.name}' has no recordings yet`);
  return accepted(submit('ai-pilot-train', `train ${d.slug}`, [
    { after: 0.6, say: 'building the consensus' },
    { after: 1.6, effect: ['train', d.slug], say: 'trained' },
    { after: 2.0, effect: ['pilotSummary', d.slug], say: 'summarising what the recordings agree on' },
  ]));
});
route('GET', `${A}/pilot/{dataset}/summary`, ({ params }) => ({ summary: pilotSummary(params.dataset) }));

// ----------------------------------------------------------------------- time
let ticking = false;
onTick((t) => {
  if (ticking) return false;          // handle() inside a turn calls advance() again; that one has nothing to add
  ticking = true;
  try {
    const a = tickConversations(t);
    const b = tickPilot(t);
    return a || b;
  } finally {
    ticking = false;
  }
});
