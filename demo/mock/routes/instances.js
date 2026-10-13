// Androids: create, change, start and stop, clone, delete, logs.

import meta from '../generated/meta.js';
import { accepted, created, route, truthy, whole } from '../backend.js';
import {
  addBackup, ApiError, bad, busyTargets, conflict, displayName, ids, lifecycle, netStatus, record, refuseOverCapacity, remove, resolved, select, summary, UP,
} from '../farm.js';
import { MAX_ANDROIDS, newSpec } from '../seed.js';
import { clone, db, now, round } from '../store.js';

const BOOTS = new Set(['start', 'restart', 'recreate']);
const GPU_MODES = ['auto', 'balanced', 'software', 'igpu', 'dgpu', 'intel', 'amd', 'nvidia'];
const NET_MODES = ['direct', 'vpn', 'gateway', 'proxy', 'none'];

// The checks worth making on a hand-typed setting; the real daemon validates the whole
// model. Errors name the field, so the form marks the right input.
function validate(spec) {
  const errors = [];
  const say = (field, message) => errors.push({ field, message });
  const int = (field, value, low, high) => {
    if (value == null) return;
    if (!Number.isInteger(value)) say(field, 'Input should be a valid integer');
    else if (value < low || value > high) say(field, `Input should be between ${low} and ${high}`);
  };
  if (typeof spec.name !== 'string' || spec.name.length > 60) say('name', 'String should have at most 60 characters');
  if (!Array.isArray(spec.tags)) say('tags', 'Input should be a valid list');
  else {
    spec.tags = [...new Set(spec.tags.map((tag) => String(tag).trim().toLowerCase()).filter(Boolean))];
    const wrong = spec.tags.find((tag) => !/^[a-z0-9][a-z0-9_.-]{0,31}$/.test(tag));
    if (wrong !== undefined) say('tags', `bad tag '${wrong}': use a-z, 0-9, _ . - (max 32)`);
  }
  if (spec.android_version != null && !db.config.android.images[spec.android_version]) say('android_version', `Android ${spec.android_version} has no image configured (android.images)`);
  if (spec.gpu_mode != null && !GPU_MODES.includes(spec.gpu_mode)) say('gpu_mode', `Input should be ${GPU_MODES.map((m) => `'${m}'`).join(', ')}`);
  int('width', spec.width, 120, 4096); int('height', spec.height, 120, 4096); int('dpi', spec.dpi, 72, 800); int('fps', spec.fps, 1, 120);
  const net = spec.network || {};
  if (net.mode != null && !NET_MODES.includes(net.mode)) say('network.mode', `Input should be ${NET_MODES.map((m) => `'${m}'`).join(', ')}`);
  if (net.gateway && !db.config.network.gateways[net.gateway]) say('network.gateway', `no gateway named '${net.gateway}'`);
  const res = spec.resources || {};
  if (res.memory_mb != null && (typeof res.memory_mb !== 'number' || res.memory_mb < 256)) say('resources.memory_mb', 'Input should be greater than or equal to 256');
  if (res.cpus != null && (typeof res.cpus !== 'number' || res.cpus <= 0)) say('resources.cpus', 'Input should be greater than 0');
  if (errors.length) throw new ApiError(400, 'bad_request', `${errors[0].field}: ${errors[0].message}${errors.length > 1 ? ` (and ${errors.length - 1} more)` : ''}`, { detail: errors });
  return spec;
}

// A mapping merges key by key, a list replaces the saved list, null puts the default back.
function applyPatch(target, patch, defaults) {
  const out = { ...target };
  for (const [key, value] of Object.entries(patch)) {
    if (key === 'id' || key === 'created') continue;
    const fallback = defaults ? defaults[key] : undefined;
    if (value === null) {
      if (fallback === undefined) delete out[key];
      else out[key] = clone(fallback);
    } else if (typeof value === 'object' && !Array.isArray(value) && out[key] && typeof out[key] === 'object' && !Array.isArray(out[key])) {
      out[key] = applyPatch(out[key], value, fallback && typeof fallback === 'object' ? fallback : undefined);
    } else {
      out[key] = value;
    }
  }
  return out;
}

route('GET', '/api/instances', () => ({ instances: ids().map((id) => summary(db.instances[id])) }));

route('POST', '/api/instances', ({ body }) => {
  const count = body.count == null ? 1 : body.count;
  if (!Number.isInteger(count) || count < 1 || count > 500) throw new ApiError(422, 'validation_error', 'count: Input should be between 1 and 500', { detail: [{ field: 'count', message: 'Input should be between 1 and 500' }] });
  if (ids().length + count > MAX_ANDROIDS) throw bad(`the demo holds at most ${MAX_ANDROIDS} Androids, so that it fits in one browser tab; a real farm has no such ceiling`);
  const template = { ...(body.template || {}) };
  delete template.id;
  delete template.created;
  delete template.use_profile_screen;
  const device = template.device || {};
  delete template.device;
  const name = typeof body.name === 'string' ? body.name.trim() : '';
  const made = [];
  let next = 1;
  for (let n = 0; n < count; n++) {
    while (db.instances[next]) next += 1;
    const spec = newSpec(next, now(), { name: name ? (count > 1 ? `${name} ${n + 1}` : name) : '', profile: device.profile && device.profile !== 'random' ? device.profile : undefined });
    const merged = validate(applyPatch(spec, { ...template, device: { ...device, profile: spec.device.profile } }, meta.spec));
    db.instances[next] = {
      spec: merged, phase: 'missing', activity: '', lastError: '', lastStart: null, mode: '', dataVersion: '', apps: {}, values: {}, netSince: 0, restarts: 0,
      publicChecked: false, device: { locale: 'en-US', timezone: '', dark_mode: false, animations: false, font_scale: 1, proxy: '' },
    };
    made.push(merged);
  }
  return created({ instances: made });
});

route('POST', '/api/instances/bulk', ({ body }) => {
  const actions = ['start', 'stop', 'restart', 'recreate', 'delete'];
  if (!actions.includes(body.action)) throw new ApiError(422, 'validation_error', `action: Input should be ${actions.map((a) => `'${a}'`).join(', ')}`);
  if (typeof body.selector !== 'string' || !body.selector) throw new ApiError(422, 'validation_error', 'selector: Field required');
  const who = select(body.selector);
  if (BOOTS.has(body.action)) refuseOverCapacity(who, Boolean(body.force));
  return accepted(lifecycle(body.action, who, { force: Boolean(body.force) }));
});

route('GET', '/api/instances/{id}', ({ params }) => {
  const rec = record(params.id);
  const t = now();
  const out = summary(rec, t);
  if (UP.has(rec.phase)) {
    out.uptime = round(Math.max(0, t - (rec.lastStart || t)));
    const live = netStatus(rec);
    out.net = { ...out.net, ...Object.fromEntries(Object.entries(live).filter(([key]) => key !== 'mode')) };
  }
  return { ...out, spec: rec.spec };
});

route('PATCH', '/api/instances/{id}', ({ params, body }) => {
  const rec = record(params.id);
  const patch = body.patch;
  if (!patch || typeof patch !== 'object' || Array.isArray(patch)) throw bad('patch must be a mapping of fields to new values');
  if ('id' in patch && Number(patch.id) !== rec.spec.id) throw bad("an Android's id cannot be changed; clone it instead");
  if ('device' in patch && patch.device === null) throw bad("an Android's device identity cannot be removed; change its fields, or set device.profile to give it another model");
  const next = validate(applyPatch(rec.spec, patch, meta.spec));
  const before = resolved(rec).android_version;
  const after = next.android_version || db.config.android.default_version;
  if (rec.dataVersion && after !== before && after !== rec.dataVersion && !body.force) {
    throw new ApiError(409, 'needs_force', `Android ${rec.spec.id} has data written by Android ${rec.dataVersion}; starting it with Android ${after} can make that data unreadable. Use force to change the version anyway (a backup is taken first)`);
  }
  rec.spec = next;
  return { instance: next, note: 'saved; a running Android picks the change up at its next restart' };
});

route('DELETE', '/api/instances/{id}', ({ params, query }) => {
  const rec = record(params.id);
  const id = rec.spec.id;
  const holder = busyTargets()[id];
  if (holder) throw new ApiError(409, 'busy', `Android ${id} is busy: task ${holder.id} (${holder.title}) is still working on it. Wait for it to finish, or cancel it`, { detail: { task: holder.id, ids: [id] } });
  const keep = truthy(query.keep_data);
  const volume = summary(rec).data_volume;
  const hadData = Boolean(rec.dataVersion);
  const saved = db.config.backup.auto_before_risky && !keep ? addBackup(id, 'before delete') : null;
  remove(id);
  return { deleted: id, id, data: !hadData ? 'none' : keep ? 'kept' : 'removed', volume, backup: saved ? saved.dir : '', warnings: [] };
});

route('POST', '/api/instances/{id}/clone', ({ params, body }) => {
  const rec = record(params.id);
  if (body.with_data && UP.has(rec.phase)) throw conflict(`Android ${rec.spec.id} is running; stop it before its data is copied`);
  if (ids().length + 1 > MAX_ANDROIDS) throw bad(`the demo holds at most ${MAX_ANDROIDS} Androids`);
  let next = 1;
  while (db.instances[next]) next += 1;
  const fresh = newSpec(next, now(), { name: `${displayName(rec.spec)} (copy)`, profile: rec.spec.device.profile });
  const spec = { ...clone(rec.spec), id: next, created: fresh.created, name: fresh.name, device: { ...clone(rec.spec.device), android_id: fresh.device.android_id, serial: fresh.device.serial }, data_volume: null };
  db.instances[next] = {
    spec, phase: body.with_data ? 'stopped' : 'missing', activity: '', lastError: '', lastStart: null, mode: '', dataVersion: body.with_data ? rec.dataVersion : '',
    apps: body.with_data ? clone(rec.apps) : {}, values: {}, netSince: 0, restarts: 0, publicChecked: false, device: clone(rec.device),
  };
  return created({ instance: spec });
});

route('GET', '/api/instances/{id}/logs', ({ params, query }) => {
  const rec = record(params.id);
  const tail = whole(query.tail ?? 200, 1, 5000, 'tail');
  const r = resolved(rec);
  if (rec.phase === 'missing') return { id: rec.spec.id, android: '', net: '' };
  const android = [
    `init: starting Android ${r.android_version} (${r.width}x${r.height}, ${r.dpi} dpi)`,
    `init: rendering: ${rec.mode || 'not started'}`, 'init: binder ready', 'init: zygote started', 'init: boot completed',
  ];
  const net = [`net: mode ${r.net_mode}`, r.killswitch ? 'net: kill switch rules installed (IPv4 and IPv6)' : 'net: kill switch off',
    r.net_mode === 'vpn' ? `net: tunnel up with ${r.vpn_profile}` : r.net_mode === 'gateway' ? `net: default route through gateway ${r.gateway}` : 'net: ready', 'net: ready=1'];
  const note = '(demo: these lines are invented)';
  return { id: rec.spec.id, android: [note, ...android].slice(-tail).join('\n') + '\n', net: [note, ...net].slice(-tail).join('\n') + '\n' };
});

route('POST', '/api/instances/{id}/{action}', ({ params, body }) => {
  const rec = record(params.id);
  if (!['start', 'stop', 'restart', 'recreate'].includes(params.action)) throw new ApiError(404, 'not_found', 'no such API route');
  const force = Boolean(body.force);
  if (BOOTS.has(params.action)) refuseOverCapacity([rec.spec.id], force);
  return accepted(lifecycle(params.action, [rec.spec.id], { force }));
});

route('GET', '/api/instances/{id}/net', ({ params, query }) => netStatus(record(params.id), { refresh: truthy(query.refresh) }));
