// AI assistants: the approvals inbox, the policy, and a simulated assistant.
//
// No assistant is connected to the demo. So that the inbox has something to answer, a
// pretend one files a request now and then: never more than two wait at once, the caller
// is named "(simulated)", and the line shown is the one the real tool writes for those
// arguments (generated/approvals.js). Approving really does the thing, to the pretend farm.

import samples from '../generated/approvals.js';
import tools from '../generated/tools.js';
import { route, whole } from '../backend.js';
import * as device from '../device.js';
import { bad, conflict, lifecycle, notFound, onTick, submit, UP } from '../farm.js';
import { HOME } from '../seed.js';
import { db, emit, hex, now } from '../store.js';

const CALLER = 'mcp:demo-assistant (simulated)';
const SESSION = 'demo-session-2';
const MAX_PENDING = 2;
const FIRST_AFTER = 25;                // seconds after signing in
const PAUSE = [70, 130];               // seconds between requests

// The operator's policy applied to one tool: the same rules as the real decision.
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

const pending = () => db.approvals.filter((item) => item.state === 'pending');

function find(id) {
  if (!/^[a-f0-9]{8}$/.test(String(id))) throw bad('bad approval id');
  const found = db.approvals.find((item) => item.id === id);
  if (!found) throw notFound('no such approval request');
  return found;
}

const outcome = (item) => (item.state === 'expired' ? `expired (${item.reason || 'nobody answered in time'})` : `${item.state} by ${item.decided_by || 'someone'}`);

function settle(item, state, by, reason = '') {
  Object.assign(item, { state, decided_by: by, decided_at: now(), reason });
  db.assistant.nextAt = now() + PAUSE[0] + Math.random() * (PAUSE[1] - PAUSE[0]);
  emit({ type: 'approval', approvals: [item] });
}

// What the simulated assistant had asked for, done to the pretend farm.
function carryOut(item) {
  const who = item.targets.filter((id) => db.instances[id]);
  if (!who.length) return;
  try {
    switch (item.tool) {
      case 'restart_androids': lifecycle('restart', who); break;
      case 'stop_androids': lifecycle('stop', who); break;
      case 'delete_androids': lifecycle('delete', who); break;
      case 'create_backup': submit('backup.manual', `back up ${who.length} Android(s)`, who.map((id, index) => ({ after: 2.5 + index * 2.4, id, effect: ['backup', id] })), { targets: who, output: { pruned: [] } }); break;
      case 'type_text': for (const id of who) if (db.instances[id].phase === 'booted') device.type(db.instances[id], item.arguments.text); break;
      case 'clear_app_data': for (const id of who) { const state = device.ui(db.instances[id]); delete state.lists[item.arguments.package]; state.rev += 1; } break;
      default: break;
    }
  } catch { /* the farm refused (an Android is busy): the approval stands, nothing ran */ }
}

route('GET', '/api/approvals', ({ query }) => {
  const limit = whole(query.limit ?? 100, 1, 500, 'limit');
  const list = query.state ? db.approvals.filter((item) => item.state === query.state) : db.approvals;
  return { approvals: [...list].sort((a, b) => b.created - a.created).slice(0, limit), pending: pending().length, now: now() };
});
route('GET', '/api/approvals/{id}', ({ params }) => ({ approval: find(params.id) }));
route('POST', '/api/approvals/{id}/approve', ({ params, body }) => {
  const item = find(params.id);
  if (item.state === 'approved') return { approval: item };
  if (item.state !== 'pending') throw conflict(`this request can no longer be approved: it was already ${outcome(item)}`);
  item.remembered = Boolean(body.remember && item.can_remember);
  settle(item, 'approved', `${body.via || 'panel'} (demo visitor)`);
  carryOut(item);
  return { approval: item };
});
route('POST', '/api/approvals/{id}/deny', ({ params, body }) => {
  const item = find(params.id);
  if (item.state === 'denied') return { approval: item };
  if (item.state !== 'pending') throw conflict(`this request can no longer be denied: it was already ${outcome(item)}`);
  settle(item, 'denied', `${body.via || 'panel'} (demo visitor)`, String(body.reason || '').replace(/\s+/g, ' ').slice(0, 500));
  return { approval: item };
});

route('GET', '/api/mcp/info', () => {
  const ai = db.config.ai;
  const command = '/home/demo/.local/bin/sf';
  return {
    policy: ai.policy, ask_for: [...ai.ask_for].sort(), auto_approve: [...ai.auto_approve].sort(), deny: [...ai.deny].sort(),
    approval_timeout_seconds: ai.approval_timeout_seconds, max_screenshot_px: ai.max_screenshot_px, forbidden_words: db.config.automation.forbidden_field_words,
    enabled: ai.mcp.enabled, http_port: ai.mcp.http_port, elicitation: ai.mcp.elicitation, sdk_version: 'not installed in the demo', pending: pending().length,
    tools: tools.map((tool) => { const [decision, reason] = decide(tool); return { name: tool.name, title: tool.title, class: tool.class, decision, reason, description: tool.description }; }),
    client_config: {
      stdio: { mcpServers: { superfarm: { command, args: ['--home', HOME, 'mcp'] } } },
      http: { mcpServers: { superfarm: { url: `http://127.0.0.1:${ai.mcp.http_port}/mcp`, headers: { Authorization: 'Bearer <the token printed by: sf token>' } } } },
      http_start: `${command} --home ${HOME} mcp --http`,
    },
  };
});

// ------------------------------------------------------ the simulated assistant
function pickTargets(sample) {
  const all = Object.values(db.instances);
  const booted = all.filter((rec) => rec.phase === 'booted').map((rec) => rec.spec.id);
  const two = sample.arguments.selector.includes(',');
  let pool = booted;
  if (sample.tool === 'delete_androids') pool = all.filter((rec) => !UP.has(rec.phase)).map((rec) => rec.spec.id);
  if (sample.tool === 'create_backup') pool = all.filter((rec) => rec.dataVersion).map((rec) => rec.spec.id);
  if (sample.tool === 'clear_app_data') pool = booted.filter((id) => db.instances[id].apps[sample.arguments.package]);
  if (pool.length < (two ? 2 : 1)) return null;
  const start = Math.floor(Math.random() * pool.length);
  return two ? [pool[start], pool[(start + 1) % pool.length]].sort((a, b) => a - b) : [pool[start]];
}

function file(t) {
  for (let tries = 0; tries < samples.length; tries++) {
    const sample = samples[db.assistant.round % samples.length];
    db.assistant.round += 1;
    const tool = tools.find((entry) => entry.name === sample.tool);
    if (!tool || decide(tool)[0] !== 'ask') continue;         // the policy answers this one by itself: nothing to ask
    const who = pickTargets(sample);
    if (!who) continue;
    const swap = (text) => String(text).replace(/901/g, who[0]).replace(/902/g, who[1] ?? who[0]);
    const item = {
      id: hex(8), created: t, expires: t + db.config.ai.approval_timeout_seconds, caller: CALLER, session: SESSION, tool: sample.tool, klass: sample.klass,
      summary: swap(who.length > 1 ? sample.two : sample.one), arguments: JSON.parse(swap(JSON.stringify(sample.arguments))), targets: who,
      can_remember: sample.can_remember, state: 'pending', decided_by: '', decided_at: 0, reason: '', remembered: false,
    };
    db.approvals.unshift(item);
    db.approvals.length = Math.min(db.approvals.length, 60);
    emit({ type: 'approval', approvals: [item] });
    return true;
  }
  return false;
}

onTick((t) => {
  let changed = false;
  for (const item of pending()) {
    if (item.expires <= t) { settle(item, 'expired', 'timeout', 'nobody answered in time'); changed = true; }
  }
  if (!db.session.signedIn) return changed;
  if (!db.assistant.nextAt) { db.assistant.nextAt = t + FIRST_AFTER; return true; }
  if (t >= db.assistant.nextAt && pending().length < MAX_PENDING) {
    db.assistant.nextAt = t + PAUSE[0] + Math.random() * (PAUSE[1] - PAUSE[0]);
    file(t);
    changed = true;
  }
  return changed;
});

