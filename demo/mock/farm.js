// The rules of the pretend farm: what an Android's status looks like, how a start turns
// into "starting", "running" and "booted", how tasks progress, and how full the invented
// host is. The routes under routes/ translate requests into calls to this module, the way
// the real routes translate them into calls to the farm.
//
// Nothing is driven by timers that would be lost on a reload. Every piece of pending work
// is a list of steps with the time each one is due, stored in the state; advance() applies
// the steps whose time has come. It runs before every request and a few times a second.

import estimates from './generated/capacity.js';
import { backup, DEVICES as devices, GPUS, HOME, HOST, HOST_PUBLIC_IP } from './seed.js';
import { db, emit, hash, nextId, now, round, save } from './store.js';

// ------------------------------------------------------------------ errors
// The real API's error envelope: {"error": {"code", "message", "detail"?}} and a status.
export class ApiError extends Error {
  constructor(status, code, message, extra = {}) {
    super(message);
    this.status = status;
    this.body = { error: { code, message, ...extra } };
  }
}
export const bad = (message, detail) => new ApiError(400, 'bad_request', message, detail ? { detail } : {});
export const notFound = (message) => new ApiError(404, 'not_found', message);
export const conflict = (message, code = 'conflict') => new ApiError(409, code, message);
export const invalid = (field, message) => new ApiError(422, 'validation_error', `${field}: ${message}`, { detail: [{ field, message }] });

// --------------------------------------------------------------- instances
export const UP = new Set(['starting', 'running', 'booted']);
const BOOT_SECONDS = { software: 11, intel: 17, nvidia: 11 };          // measured on the test machine, to the second

export function record(id) {
  const n = Number(id);
  const found = Number.isInteger(n) ? db.instances[n] : null;
  if (!found) {
    if (!/^\d{1,4}$/.test(String(id))) throw bad(`bad Android id ${JSON.stringify(String(id).slice(0, 20))}`);
    throw notFound(`there is no Android ${n}`);
  }
  return found;
}

export const ids = () => Object.keys(db.instances).map(Number).sort((a, b) => a - b);
export const displayName = (spec) => spec.name || `Android ${spec.id}`;

// The same grammar as the real selector: all | running | stopped | tag:x | 5 | 1-10, joined
// with commas; a leading "!" removes.
export function select(selector) {
  const picked = new Set();
  const all = ids();
  for (const raw of String(selector).split(',').map((part) => part.trim()).filter(Boolean)) {
    const negate = raw.startsWith('!');
    const part = negate ? raw.slice(1) : raw;
    let match;
    if (part === 'all') match = () => true;
    else if (part === 'running') match = (id) => UP.has(db.instances[id].phase);
    else if (part === 'stopped') match = (id) => !UP.has(db.instances[id].phase);
    else if (part.startsWith('tag:')) match = (id) => db.instances[id].spec.tags.includes(part.slice(4).toLowerCase());
    else if (/^\d+-\d+$/.test(part)) {
      const [a, b] = part.split('-').map(Number);
      match = (id) => id >= Math.min(a, b) && id <= Math.max(a, b);
    } else if (/^\d+$/.test(part)) match = (id) => id === Number(part);
    else throw bad(`bad selector part '${raw}'`);
    for (const id of all) if (match(id)) picked[negate ? 'delete' : 'add'](id);
  }
  return [...picked].sort((a, b) => a - b);
}

// ids or a selector, as the input, window and camera routes take them.
export function targets(body, { allowEmpty = false } = {}) {
  let out;
  if (Array.isArray(body.ids)) out = body.ids.map((id) => record(id).spec.id);
  else if (typeof body.selector === 'string' && body.selector) out = select(body.selector);
  else if (allowEmpty) return [];
  else throw new ApiError(422, 'validation_error', 'give `ids` or `selector`');
  if (!out.length && !allowEmpty) throw bad('no Android matches');
  return out;
}

// An Android's settings with the farm defaults filled in.
export function resolved(rec) {
  const c = db.config;
  const s = rec.spec;
  const preset = devices[s.device.profile] || {};
  const net = s.network || {};
  const mode = net.mode || c.network.mode;
  return {
    android_version: s.android_version || c.android.default_version,
    width: s.width || c.android.width, height: s.height || c.android.height, dpi: s.dpi || c.android.dpi, fps: s.fps || c.android.fps,
    gpu_mode: s.gpu_mode || c.gpu.mode, gpu_node: s.gpu_node || '',
    memory_mb: (s.resources && s.resources.memory_mb) || c.resources.memory_mb,
    net_mode: mode,
    vpn_profile: mode === 'vpn' ? (net.vpn_profile || c.network.vpn.profile || (db.profiles[0] || {}).name || '') : '',
    gateway: mode === 'gateway' ? (net.gateway ?? c.network.gateway) || '' : '',
    proxy: mode === 'proxy' ? (net.proxy ?? c.network.proxy) || '' : '',
    killswitch: net.killswitch ?? c.network.killswitch,
    model: preset.label || s.device.profile,
  };
}

// Which rendering an Android would get if it started now, and why not when it cannot.
export function rendering(requested) {
  const [intel, nvidia] = GPUS;
  const card = (gpu, kind) => ({ node: gpu.node, kind, label: gpu.name });
  switch (requested) {
    case 'software': return { mode: 'software', node: '', kind: '', label: '' };
    case 'igpu': case 'intel': return { mode: 'intel', ...card(intel, 'integrated') };
    case 'auto': case 'dgpu': case 'nvidia': return { mode: 'nvidia', ...card(nvidia, 'discrete') };
    case 'amd': return { error: "gpu mode 'amd' needs an AMD GPU with a Mesa driver, and this machine has none" };
    default: return { error: `unknown gpu mode '${requested}'` };
  }
}

const imageOf = (version) => {
  const entry = db.config.android.images[version];
  return (entry && entry.tag) || `superfarm/android:${version}`;
};

export function summary(rec, t = now()) {
  const s = rec.spec;
  const r = resolved(rec);
  const prefix = db.config.docker.name_prefix;
  const containers = rec.phase === 'missing' ? 'missing' : UP.has(rec.phase) ? 'running' : 'exited';
  const status = containers === 'running' ? `Up ${Math.max(1, Math.round((t - (rec.lastStart || t)) / 60))} minutes` : containers === 'exited' ? 'Exited (0)' : '';
  const port = db.config.network.adb_port_base + s.id;
  const used = rec.mode ? rendering(rec.mode) : {};
  return {
    id: s.id, name: displayName(s), tags: [...s.tags], notes: s.notes, state: rec.phase, busy: rec.activity,
    containers: {
      android: { name: `${prefix}-android-${s.id}`, state: containers, status },
      net: { name: `${prefix}-net-${s.id}`, state: containers, status },
    },
    adb_port: port, serial: `${db.config.network.adb_bind}:${port}`,
    data_volume: s.data_volume || `${prefix}-data-${s.id}`, adopted: Boolean(s.data_volume), autostart: s.autostart,
    device_profile: s.device.profile, uptime: null, last_error: rec.lastError, last_start: rec.lastStart,
    warnings: rec.warnings || [], data_android_version: rec.dataVersion || '',
    android_version: r.android_version, image: s.image || imageOf(r.android_version),
    screen: { width: r.width, height: r.height, dpi: r.dpi, fps: r.fps },
    gpu: { requested: r.gpu_mode, mode: used.mode || '', node: used.node || '', kind: used.kind || '', label: used.label || '' },
    net: { mode: r.net_mode, profile: r.vpn_profile, gateway: r.gateway, proxy: r.proxy, killswitch: r.killswitch },
  };
}

// Tunnel state, kill switch and addresses of one Android (GET /api/instances/{id}/net).
export function netStatus(rec, { refresh = false } = {}) {
  const r = resolved(rec);
  const up = UP.has(rec.phase);
  const base = {
    mode: r.net_mode, provider: '', profile: '', state: 'stopped', ready: false, since: 0, started: 0, tunnel_ip: '', tunnel_dev: '',
    restarts: rec.restarts || 0, detail: '', fix: '', killswitch: { enabled: r.killswitch, active: false, guarded: false, problems: [] },
    servers: 0, stalled: false, public_ip: null,
  };
  if (!up) return { ...base, state: rec.phase === 'missing' ? 'missing' : 'stopped', detail: rec.phase === 'missing' ? 'the network container does not exist; start the Android' : '' };
  if (refresh) rec.publicChecked = true;
  const since = Math.round(rec.netSince || rec.lastStart || 0);
  const guard = { enabled: r.killswitch, active: r.killswitch, guarded: r.killswitch, problems: [] };
  const seen = (address) => (rec.publicChecked ? address : null);
  if (r.net_mode === 'none') return { ...base, state: 'isolated', ready: true, since, started: since, killswitch: { ...guard, active: true, guarded: true } };
  if (r.net_mode === 'direct') return { ...base, state: 'direct', ready: true, since, started: since, killswitch: { ...guard, active: false, guarded: false, enabled: false }, public_ip: seen(HOST_PUBLIC_IP) };
  if (r.net_mode === 'proxy') return { ...base, state: 'direct', ready: true, since, started: since, detail: `through the proxy ${r.proxy || '(none set)'}`, killswitch: { enabled: false, active: false, guarded: false, problems: [] }, public_ip: seen(`203.0.113.${60 + (rec.spec.id % 60)}`) };
  if (r.net_mode === 'gateway') {
    const gateway = db.gateways[r.gateway];
    const ok = Boolean(gateway && gateway.up);
    return {
      ...base, provider: 'gateway', profile: r.gateway, state: ok ? 'connected' : 'reconnecting', ready: ok, since, started: since,
      tunnel_ip: `192.0.2.${128 + (rec.spec.id % 120)}`, tunnel_dev: 'eth0', killswitch: guard,
      detail: ok ? '' : `the shared gateway ${r.gateway || '(none)'} is down; nothing leaves this Android until it is back`,
      public_ip: ok ? seen(exitAddress(db.config.network.gateways[r.gateway].profile)) : null,
    };
  }
  // vpn: this Android's own tunnel
  const profile = db.profiles.find((p) => p.name === r.vpn_profile);
  if (!profile) return { ...base, provider: 'openvpn', profile: r.vpn_profile, state: 'error', killswitch: guard, detail: `VPN profile ${r.vpn_profile || '(none)'} does not exist`, fix: 'import the profile on the Network page, or choose another one in this Android’s settings' };
  if (profile.needs_credentials && !db.credentials.includes(db.config.network.vpn.credentials)) {
    return { ...base, provider: profile.provider, profile: profile.name, state: 'auth_failed', killswitch: guard, servers: profile.servers.length,
      detail: 'the server refused the username or password', fix: `store credentials named "${db.config.network.vpn.credentials}" on the Network page` };
  }
  return {
    ...base, provider: profile.provider, profile: profile.name, state: 'connected', ready: true, since, started: since,
    tunnel_ip: `198.51.100.${120 + (rec.spec.id % 100)}`, tunnel_dev: profile.provider === 'wireguard' ? 'wg0' : 'tun0', killswitch: guard,
    servers: profile.servers.length, public_ip: seen(exitAddress(profile.name)),
  };
}

// Each VPN profile exits from its own invented public address.
export const exitAddress = (profileName) => `203.0.113.${20 + Math.floor(hash(profileName) * 200)}`;

// ---------------------------------------------------------------- capacity
// The estimate is the real estimator's answer for the invented host (generated/capacity.js,
// one per farm-wide rendering mode). What moves here is how much of it is in use.
export function capacity() {
  const perf = db.config.performance;
  const running = ids().filter((id) => UP.has(db.instances[id].phase)).length;
  const estimate = { ...(estimates[db.config.gpu.mode] || estimates.auto), running_seen: running };
  const max = estimate.max_instances;
  const used = ids().reduce((sum, id) => sum + (UP.has(db.instances[id].phase) ? memoryOf(id) : 0), 0);
  const available = Math.max(0, Math.round(HOST.ram_mb - 2600 - used));
  const reasons = [];
  if (Number.isInteger(max) && running >= max) reasons.push(`${running} Android(s) are running and this machine is estimated to hold about ${max} (limited by ${estimate.binding || 'its resources'})`);
  if (perf.memory_guard_mb > 0 && available < perf.memory_guard_mb) reasons.push(`${available} MB of RAM is available; below performance.memory_guard_mb (${perf.memory_guard_mb} MB)`);
  return {
    estimate, running, headroom: Number.isInteger(max) ? Math.max(0, max - running) : 0, over: reasons.length > 0, reasons, memory_available_mb: available,
    enforced: perf.enforce_capacity, advice: [...(estimate.advice || [])],
  };
}

// Figures that move a little on every look, around a value that is stable per Android.
const wobble = (key, t, period = 9) => Math.sin(t / period + hash(key) * 6.283);
export const memoryOf = (id) => {
  const rec = db.instances[id];
  const apps = Object.keys(rec.apps || {}).length;
  return 880 + Math.round(hash(`mem${id}`) * 160) + apps * 22 + (rec.mode === 'nvidia' ? 300 : 0);
};
export function cpuOf(id, t) {
  const rec = db.instances[id];
  if (rec.phase !== 'booted') return rec.phase === 'starting' || rec.phase === 'running' ? 140 + 30 * wobble(`boot${id}`, t, 2) : 0;
  const base = rec.mode === 'software' ? 78 : rec.mode === 'intel' ? 13 : 15;
  return Math.max(1, base * (0.55 + hash(`cpu${id}`) * 0.5) + base * 0.25 * wobble(`cpu${id}`, t));
}

export function metrics(t = now()) {
  const running = ids().filter((id) => UP.has(db.instances[id].phase));
  const perInstance = {};
  let cpu = 0;
  let mem = 0;
  for (const id of running) {
    const one = { cpu: round(cpuOf(id, t)), mem_mb: round(memoryOf(id) + 12 * wobble(`m${id}`, t, 30)), mem_limit_mb: resolved(db.instances[id]).memory_mb, pids: 70 + Math.round(hash(`pid${id}`) * 40) };
    perInstance[id] = one;
    cpu += one.cpu;
    mem += one.mem_mb;
  }
  const threads = HOST.cpu.threads;
  const hostCpu = Math.min(100, round(2.5 + cpu / threads + 0.8 * wobble('host', t, 5)));
  const used = Math.round(2600 + mem);
  const onCard = running.filter((id) => db.instances[id].mode === 'nvidia').length;
  const onIntel = running.filter((id) => db.instances[id].mode === 'intel').length;
  return {
    time: t,
    host: {
      cpu_percent: hostCpu, cpu_count: threads, load: [round(hostCpu * threads / 100 + 0.3, 2), round(hostCpu * threads / 110 + 0.4, 2), round(hostCpu * threads / 125 + 0.5, 2)],
      memory: { total_mb: HOST.ram_mb, available_mb: HOST.ram_mb - used, used_mb: used, swap_total_mb: HOST.swap_mb, swap_free_mb: HOST.swap_mb, used_percent: round((100 * used) / HOST.ram_mb) },
      temps: [{ sensor: 'cpu', label: 'Package', celsius: round(38 + hostCpu * 0.35 + wobble('temp', t, 20)) }],
    },
    gpus: [
      { index: 0, vendor: 'nvidia', driver: 'nvidia', name: GPUS[1].name, util_percent: round(Math.min(100, 2 + onCard * 6 + 2 * wobble('gpu0', t, 4))), vram_used_mb: 420 + onCard * 360,
        vram_total_mb: GPUS[1].vram_mb, temp_c: round(41 + onCard * 2.2), power_w: round(28 + onCard * 9 + 3 * wobble('pw', t, 6), 2) },
      { index: 1, vendor: 'intel', driver: 'i915', name: GPUS[0].name, node: GPUS[0].node, util_percent: null, vram_used_mb: null, vram_total_mb: null, temp_c: null, power_w: null,
        freq_mhz: onIntel ? 900 + Math.round(200 * wobble('fq', t, 3)) : 0, freq_max_mhz: 1650 },
    ],
    instances: perInstance,
    totals: { defined: ids().length, running: running.length, cpu_percent: round(cpu), mem_mb: round(mem) },
  };
}

// ------------------------------------------------------------------- tasks
// A task is its public fields plus `plan`: steps of the form
//   { at, id?, ok?, detail?, effect?, silent? }
// `id` names the Android the step reports on; a `silent` step changes state without being
// a result (an Android going from "starting" to "running"). Effects are plain data so that
// the whole task survives a reload; EFFECTS turns them into changes.
export const ACTIVE = new Set(['queued', 'running']);
const EXCLUSIVE = new Set(['instance.start', 'instance.stop', 'instance.restart', 'instance.recreate', 'instance.delete', 'backup.manual', 'backup.restore']);
const KEEP_TASKS = 60;
// How a finished task words its count: "3 of 3 succeeded", "2 of 2 backed up".
const WORDS = { 'backup.manual': 'backed up', automation: 'finished' };
const effects = {};
export function registerEffect(name, fn) { effects[name] = fn; }

export function publicTask(task) {
  const { plan, done, ...rest } = task;
  const failed = Object.values(task.results).filter((r) => !r.ok).length;
  return { ...rest, failed };
}

export function submit(kind, title, plan, { targets: who = [], output = null, word = WORDS[kind] || 'succeeded', exclusive = EXCLUSIVE.has(kind) } = {}) {
  const t = now();
  if (exclusive && who.length) {
    const busy = busyTargets();
    const taken = who.filter((id) => busy[id]);
    if (taken.length) {
      const holder = busy[taken[0]];
      const which = taken.length === 1 ? `Android ${taken[0]} is` : `Androids ${taken.slice(0, 10).join(', ')} are`;
      throw new ApiError(409, 'busy', `${which} busy: task ${holder.id} (${holder.title}) is still working on ${taken.length === 1 ? 'it' : 'them'}. Wait for it to finish, or cancel it`,
        { detail: { task: holder.id, ids: taken } });
    }
  }
  const counted = plan.filter((step) => step.id != null && !step.silent);
  const task = {
    id: `t${nextId('task')}`, kind, title, targets: who, state: 'running', current: 0, total: counted.length, message: counted.length ? `0/${counted.length}` : '',
    error: '', results: {}, output: null, created: t, started: t, finished: 0,
    plan: plan.map((step) => ({ ...step, at: t + (step.after || 0) })).sort((a, b) => a.at - b.at), done: { output, word, exclusive },
  };
  db.tasks.unshift(task);
  db.tasks.length = Math.min(db.tasks.length, KEEP_TASKS);
  return task;
}

// Android id -> the active exclusive task that still has work to do on it.
export function busyTargets() {
  const out = {};
  for (const task of db.tasks) {
    if (!ACTIVE.has(task.state) || !(task.done && task.done.exclusive)) continue;
    for (const step of task.plan) if (step.id != null && !step.silent && out[step.id] === undefined) out[step.id] = task;
  }
  return out;
}

export const findTask = (id) => db.tasks.find((task) => task.id === id);

export function cancelTask(task) {
  if (!ACTIVE.has(task.state)) return false;
  const left = task.plan;
  task.plan = [];
  task.state = 'cancelled';
  task.finished = now();
  task.message = 'cancelled';
  // An Android that was on its way up goes back down; anything else is left as it is.
  for (const step of left) {
    const rec = step.id != null && db.instances[step.id];
    if (!rec) continue;
    if (step.effect && step.effect[0] === 'booted') { rec.phase = 'stopped'; rec.mode = ''; }
    rec.activity = '';
  }
  if (effects.taskCancelled) effects.taskCancelled(task);
  return true;
}

// Apply every step that is due. Returns true when anything changed.
export function advance(t = now()) {
  let changed = false;
  for (const task of db.tasks) {
    if (!ACTIVE.has(task.state)) continue;
    while (task.plan.length && task.plan[0].at <= t) {
      const step = task.plan.shift();
      let outcome = { ok: step.ok !== false, detail: step.detail || '' };
      if (step.effect) {
        const [name, ...args] = step.effect;
        try {
          const said = effects[name](...args, step, task);
          if (said && typeof said === 'object') outcome = { ...outcome, ...said };
        } catch (error) {
          outcome = { ok: false, detail: error.message };
        }
      }
      if (step.id != null && !step.silent) {
        task.results[step.id] = { ok: outcome.ok, detail: outcome.detail };
        task.current += 1;
        task.message = `${task.current}/${task.total}`;
      } else if (step.say) {
        task.message = step.say;
      }
      if (outcome.output) task.done.output = { ...(task.done.output || {}), ...outcome.output };
      if (!outcome.ok && step.id == null) { task.state = 'failed'; task.error = outcome.detail; task.plan = []; }
      changed = true;
    }
    if (!task.plan.length && !task.hold && ACTIVE.has(task.state)) {
      const results = Object.values(task.results);
      const good = results.filter((r) => r.ok).length;
      task.state = 'done';
      task.finished = t;
      task.message = results.length ? `${good} of ${results.length} ${task.done.word}` : (task.message || 'done');
      task.output = results.length ? { ok: good, failed: results.length - good, ...(task.done.output || {}) } : task.done.output;
      changed = true;
    }
  }
  for (const hook of tickers) changed = hook(t) || changed;
  if (changed) { save(); emit({ type: 'changed' }); }
  return changed;
}

// Other modules (automation runs, the simulated assistant) advance with the same clock.
const tickers = [];
export function onTick(fn) { tickers.push(fn); }

// --------------------------------------------------------------- lifecycle
registerEffect('activity', (id, word) => {
  const rec = db.instances[id];
  if (rec) rec.activity = word;
});

registerEffect('phase', (id, phase) => {
  const rec = db.instances[id];
  if (!rec) return;
  rec.phase = phase;
});

registerEffect('booted', (id, seconds, mode) => {
  const rec = db.instances[id];
  if (!rec) throw new Error('the Android was deleted meanwhile');
  const r = resolved(rec);
  rec.phase = 'booted';
  rec.activity = '';
  rec.lastError = '';
  rec.netSince = now();
  rec.publicChecked = false;
  rec.dataVersion = rec.dataVersion || r.android_version;
  if (!db.images.built.includes(r.android_version)) db.images.built.push(r.android_version);
  if (!db.images.pulled.includes(r.android_version)) db.images.pulled.push(r.android_version);
  return { detail: `booted in ${seconds} s (${mode})` };
});

registerEffect('stopped', (id) => {
  const rec = db.instances[id];
  if (!rec) return { ok: true, detail: 'already gone' };
  const was = rec.phase;
  rec.phase = rec.phase === 'missing' ? 'missing' : 'stopped';
  rec.activity = '';
  rec.mode = '';
  if (effects.deviceStopped) effects.deviceStopped(id);
  return { detail: was === 'missing' || was === 'stopped' ? 'was not running' : 'stopped' };
});

registerEffect('fail', (id, message) => {
  const rec = db.instances[id];
  if (rec) { rec.phase = 'error'; rec.activity = ''; rec.lastError = message; rec.mode = ''; }
  return { ok: false, detail: message };
});

registerEffect('deleted', (id, keepData) => {
  if (!db.instances[id]) return { ok: false, detail: `there is no Android ${id}` };
  const saved = db.config.backup.auto_before_risky && !keepData ? addBackup(id, 'before delete') : null;
  remove(id);
  return { detail: keepData ? 'deleted; its data volume was kept' : saved ? `deleted; backed up first as ${saved.name}` : 'deleted' };
});

// A backup entry for one Android, as the Backups page lists it. Sizes are invented.
export function addBackup(id, reason) {
  const rec = db.instances[id];
  if (!rec || !rec.dataVersion) return null;
  const size = Math.round((0.9 + hash(`size${id}`) * 0.7 + Object.keys(rec.apps).length * 0.06) * 1024 ** 3);
  let entry = backup('android', id, displayName(rec.spec), now(), size, reason, { android_version: rec.dataVersion, android_id: rec.spec.device.android_id });
  // Two backups of one Android in the same second get a suffix, as on a real farm.
  for (let n = 2; db.backups.some((b) => b.name === entry.name); n++) entry = { ...entry, name: `${entry.name.replace(/-\d+$/, '')}-${n}` };
  db.backups.unshift(entry);
  db.backups.length = Math.min(db.backups.length, 40);
  return entry;
}

export function remove(id) {
  delete db.instances[id];
  if (db.listening && db.listening.id === id) db.listening = null;
  db.windows = db.windows.filter((n) => n !== id);
  for (const feed of db.camera.feeds) feed.ids = feed.ids.filter((n) => n !== id);
  db.camera.feeds = db.camera.feeds.filter((feed) => feed.ids.length);
  if (effects.deviceStopped) effects.deviceStopped(id);
}

// The steps that bring one Android up, `offset` seconds from now.
function bootSteps(id, offset, force) {
  const rec = db.instances[id];
  const r = resolved(rec);
  const picked = rendering(r.gpu_mode);
  if (picked.error) return [{ after: offset + 0.6, id, effect: ['fail', id, picked.error] }];
  if (r.net_mode === 'vpn' && !db.profiles.some((p) => p.name === r.vpn_profile)) {
    return [{ after: offset + 0.6, id, effect: ['fail', id, `VPN profile ${r.vpn_profile || '(none set)'} does not exist; import it on the Network page`] }];
  }
  const seconds = round(BOOT_SECONDS[picked.mode] + hash(`boot${id}${rec.lastStart}`) * 1.2);
  rec.phase = 'starting';
  rec.activity = 'starting';
  rec.lastStart = now() + offset;
  rec.mode = picked.mode;
  rec.warnings = force ? ['started over the capacity estimate (forced)'] : [];
  return [
    { after: offset + seconds * 0.4, id, silent: true, effect: ['phase', id, 'running'] },
    { after: offset + seconds, id, effect: ['booted', id, seconds, picked.mode] },
  ];
}

const TITLES = { start: 'start', stop: 'stop', restart: 'restart', recreate: 'recreate', delete: 'delete' };

// start | stop | restart | recreate | delete on a list of Androids: one task, one result each.
export function lifecycle(action, who, { force = false, keepData = false } = {}) {
  if (!who.length) throw bad('the selector matches no Android');
  const perf = db.config.performance;
  const plan = [];
  let slot = 0;
  // Collected first and scheduled after submit() has had the chance to refuse the request.
  const build = [];
  for (const id of who) {
    const rec = db.instances[id];
    const up = UP.has(rec.phase);
    if (action === 'stop') build.push(() => (up ? [{ after: 1.2 + 0.25 * slot++, id, effect: ['stopped', id] }] : [{ after: 0.2, id, detail: 'was not running' }]));
    else if (action === 'delete') build.push(() => [{ after: (up ? 1.6 : 0.5) + 0.2 * slot++, id, effect: ['deleted', id, keepData] }]);
    else if (action === 'start' && up) build.push(() => [{ after: 0.2, id, detail: rec.phase === 'booted' ? 'already running' : 'already starting' }]);
    else {
      build.push(() => {
        const turn = slot++;
        const wave = Math.floor(turn / Math.max(1, perf.start_concurrency));
        const offset = (turn % Math.max(1, perf.start_concurrency)) * perf.start_stagger_seconds + wave * 12 + (up ? 1.5 : 0);
        if (up) { rec.phase = 'starting'; rec.activity = action === 'recreate' ? 'recreating' : 'restarting'; }
        return bootSteps(id, offset, force);
      });
    }
  }
  const task = submit(`instance.${action}`, `${TITLES[action]} ${who.length} Android(s)`, [], { targets: who });
  for (const make of build) plan.push(...make());
  const t = task.created;
  task.plan = plan.map((step) => ({ ...step, at: t + step.after })).sort((a, b) => a.at - b.at);
  task.total = plan.filter((step) => !step.silent).length;
  task.message = `0/${task.total}`;
  for (const id of who) {
    const rec = db.instances[id];
    if (rec && !rec.activity && (action === 'stop' || action === 'delete') && UP.has(rec.phase)) rec.activity = action === 'stop' ? 'stopping' : 'deleting';
  }
  return task;
}

// The same refusal as the real route: only when the estimate is enforced, and not forced.
export function refuseOverCapacity(who, force) {
  if (force || !db.config.performance.enforce_capacity) return;
  const starting = who.filter((id) => !UP.has(db.instances[id].phase)).length;
  if (!starting) return;
  const cap = capacity();
  const reasons = [...cap.reasons];
  if (!cap.over && starting > cap.headroom) reasons.push(`${starting} Androids were asked to start and about ${cap.headroom} more fit (${cap.running} running, estimate ${cap.estimate.max_instances})`);
  if (!reasons.length) return;
  const message = `not starting: over capacity (${reasons.join('; ')}). Stop some Androids, or start anyway with force; set performance.enforce_capacity to false to only be warned`;
  throw new ApiError(409, 'over_capacity', message, { detail: message, capacity: cap });
}

// A fleet action that is the same small job on every selected Android (install an app,
// set the time zone, run a command): one step each, a short moment apart.
export function fleet(kind, title, who, work, { each = 0.5, first = 0.7, needsBoot = true, output } = {}) {
  if (!who.length) throw bad('the selector matches no Android');
  const plan = who.map((id, index) => {
    const rec = db.instances[id];
    if (needsBoot && rec.phase !== 'booted') return { after: 0.3, id, ok: false, detail: `Android ${id} is not running` };
    return { after: first + each * index, id, ...work(id, rec) };
  });
  return submit(kind, title, plan, { targets: who, output });
}

export const system = () => {
  const checks = doctorChecks();
  return {
    version: versionOf(), farm_name: db.config.farm_name, home: HOME, host: HOST, gpus: GPUS, capacity: capacity(),
    doctor: summarize(checks),
    components: Object.fromEntries(['docker', 'store', 'tasks', 'instances', 'images', 'backups', 'library', 'apps', 'files', 'device', 'shell', 'screen', 'input',
      'windows', 'camera', 'listener', 'automation', 'approvals'].map((name) => [name, { available: true, reason: '' }])
      .concat([['h264', { available: false, reason: 'the demo draws its screens in the page; there is no device stream to relay' }]])),
    // Said directly, so the panel shows the picture stream and never opens a video socket.
    h264: { enabled: false, available: false, reason: 'not part of the demo' },
  };
};

let version = '0';
export function setVersion(value) { version = value; }
const versionOf = () => version;

// ------------------------------------------------------------------ doctor
// A short invented check list in the real one's shape, with two warnings to look at.
export function doctorChecks() {
  const c = db.config;
  const ok = (id, title, detail) => ({ id, title, status: 'ok', detail, fix: '' });
  return [
    ok('cpu.arch', '64-bit x86 CPU', 'x86_64'), ok('cpu.count', 'CPU threads', `${HOST.cpu.threads} threads`), ok('kernel.version', 'Kernel version', HOST.kernel),
    ok('kernel.binder', 'Android binder', 'binderfs (binder_linux: loaded)'), ok('kernel.tun', 'TUN device (VPN)', 'tun (builtin)'),
    ok('kernel.netfilter', 'Netfilter (NAT, published ports, kill switch)', 'loaded'), ok('docker.running', 'Docker daemon', 'running'),
    ok('docker.access', 'Docker usable by this user', 'yes'),
    { id: 'limits.inotify', title: 'inotify limits', status: 'warn', detail: 'max_user_instances is 128; many Androids need more', fix: 'sudo sf host apply' },
    ok('mem.ram', 'Memory', `${Math.round(HOST.ram_mb / 1024)} GB`),
    { id: 'mem.swap', title: 'Swap / zram', status: c.performance.zram.enabled ? 'ok' : 'warn', detail: c.performance.zram.enabled ? 'zram is on' : 'no compressed swap; idle Androids could share memory better with it',
      fix: c.performance.zram.enabled ? '' : 'sf config set performance.zram.enabled true\nsudo sf host apply' },
    ok('disk.space', 'Disk space', '412 GB free'), ok('gpu.detected', 'GPUs', GPUS.map((g) => g.name).join(', ')), ok('gpu.access', 'Render node permissions', 'readable and writable'),
    ok('nvidia.runtime', 'NVIDIA runtime for Android (Venus)', 'installed'), ok('tools.adb', 'adb', 'found'), ok('tools.scrcpy', 'scrcpy', 'found'),
    ok('net.adb_bind', 'ADB exposure', `bound to ${c.network.adb_bind}`), ok('net.server_host', 'Panel exposure', `bound to ${c.server.host}`),
    { id: 'camera.module', title: 'Virtual camera module (v4l2loopback)', status: 'skip', detail: 'media.camera.enabled is off', fix: '' },
  ];
}

export function summarize(checks) {
  const counts = { ok: 0, warn: 0, fail: 0, skip: 0 };
  for (const check of checks) counts[check.status] += 1;
  return { counts, ready: counts.fail === 0, worst: counts.fail ? 'fail' : counts.warn ? 'warn' : 'ok' };
}
