// Health, sign-in, the invented host, settings, images and tasks.

import configData from '../generated/config.js';
import meta from '../generated/meta.js';
import schema from '../generated/schema.js';
import { accepted, route, truthy, whole } from '../backend.js';
import {
  ACTIVE, ApiError, bad, cancelTask, capacity, conflict, doctorChecks, findTask, metrics, notFound, publicTask, registerEffect, setVersion,
  submit, summarize, system,
} from '../farm.js';
import { DEVICES, GPUS, HOME } from '../seed.js';
import { clone, db, hex, mergePatch, now } from '../store.js';

setVersion(meta.version);

// ------------------------------------------------------------ health / auth
route('GET', '/api/health', () => ({ ok: true, version: meta.version }));

// The demo has no token. Whatever is typed is accepted, compared with nothing, kept
// nowhere and sent nowhere; "signed in" is one flag in this tab's state.
route('POST', '/api/auth/login', () => {
  db.session = { signedIn: true, csrf: hex(32), expires: now() + db.config.server.session_hours * 3600 };
  return { ok: true, auth_required: true, csrf: db.session.csrf, expires: db.session.expires };
});
route('POST', '/api/auth/logout', () => {
  db.session = { signedIn: false };
  return { ok: true };
});
route('GET', '/api/auth/me', () => ({ authenticated: true, method: 'session', auth_required: true, csrf: db.session.csrf, expires: db.session.expires }));

// ------------------------------------------------------------------- system
route('GET', '/api/system', () => system());
route('GET', '/api/system/capacity', () => capacity());
route('GET', '/api/system/doctor', ({ query }) => {
  if (truthy(query.fresh)) db.doctorRan = now();
  const checks = doctorChecks();
  return { checks, summary: summarize(checks) };
});
route('GET', '/api/system/metrics', () => metrics());
route('GET', '/api/device-profiles', () => ({ profiles: DEVICES }));

route('GET', '/api/system/gpus', () => {
  const [intel, card] = GPUS;
  const option = (value, label, available = true, reason = '', node = '') => ({ value, label, available, reason, node });
  return {
    gpus: GPUS,
    options: [
      option('auto', `Automatic (now: ${card.name} (discrete, relay))`),
      option('software', 'CPU only (software rendering)'),
      option('igpu', `Integrated GPU: ${intel.name}`),
      option('dgpu', `Discrete GPU: ${card.name}`),
      option('intel', `Intel GPU (Mesa): ${intel.name}`),
      option('amd', 'AMD GPU (Mesa)', false, "gpu mode 'amd' needs an AMD GPU with a Mesa driver, and this machine has none"),
      option('nvidia', `NVIDIA GPU (relay): ${card.name}`),
      option('intel', `${intel.name} (integrated, Mesa) on renderD128`, true, '', intel.node),
      option('nvidia', `${card.name} (discrete, relay) on renderD129`, true, '', card.node),
    ],
    relay: {
      runtime: { runtime_dir: `${HOME}/overlays/venus`, relay_dir: `${HOME}/overlays/venus-relay`, files: { 'gpu_config.sh': true, 'vulkan.virtio.so': true, 'gralloc.venus.so': true },
        relay: { binary: true, helper: true, library: '' }, icd: true, icd_path: db.config.gpu.nvidia.icd, missing: [], runtime_ready: true, relay_ready: true, ready: true, built_for: 'Android 13' },
      relays: [{ node: card.node, running: Object.values(db.instances).some((rec) => rec.mode === 'nvidia'), pid: 4242, socket: '/run/superfarm/venus/renderD129' }],
    },
  };
});

// ------------------------------------------------------------------- config
route('GET', '/api/config', () => ({ config: db.config }));
route('GET', '/api/config/schema', () => schema);

const ANDROID_RESTART = ['android.', 'resources.', 'gpu.', 'network.', 'device.', 'media.camera.', 'docker.log_', 'docker.name_prefix',
  'performance.restart_policy', 'performance.trim_', 'performance.disable_animations'];
const DAEMON_RESTART = ['server.host', 'server.port', 'server.tls_cert', 'server.tls_key', 'server.allowed_origins', 'server.trusted_proxies', 'logging.'];

function flatten(value, prefix, out) {
  if (value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length) {
    for (const [key, inner] of Object.entries(value)) flatten(inner, prefix ? `${prefix}.${key}` : key, out);
  } else {
    out[prefix] = JSON.stringify(value);
  }
  return out;
}

const at = (object, dotted) => dotted.split('.').reduce((node, key) => (node == null ? undefined : node[key]), object);

// The checks a JSON schema written by the real settings model can express: types, choices,
// ranges and lengths. The real daemon validates with that model itself, so a few
// cross-field rules it has are not applied here.
function check(value, rule, path, errors) {
  if (rule.$ref) rule = schema.$defs[rule.$ref.split('/').pop()];
  if (rule.anyOf) {
    const attempts = rule.anyOf.map((option) => { const found = []; check(value, option, path, found); return found; });
    if (!attempts.some((found) => found.length === 0)) errors.push(...attempts.sort((a, b) => a.length - b.length)[0]);
    return;
  }
  const say = (message) => errors.push({ field: path, message });
  if (rule.enum && !rule.enum.includes(value)) return say(`Input should be ${rule.enum.map((v) => `'${v}'`).join(', ')}`);
  if (rule.const !== undefined && value !== rule.const) return say(`Input should be '${rule.const}'`);
  switch (rule.type) {
    case 'integer': if (!Number.isInteger(value)) return say('Input should be a valid integer'); break;
    case 'number': if (typeof value !== 'number' || !Number.isFinite(value)) return say('Input should be a valid number'); break;
    case 'boolean': if (typeof value !== 'boolean') return say('Input should be a valid boolean'); break;
    case 'string': if (typeof value !== 'string') return say('Input should be a valid string'); break;
    case 'null': if (value !== null) return say('Input should be None'); break;
    case 'array':
      if (!Array.isArray(value)) return say('Input should be a valid list');
      if (rule.items) value.forEach((item, index) => check(item, rule.items, `${path}.${index}`, errors));
      break;
    case 'object':
      if (!value || typeof value !== 'object' || Array.isArray(value)) return say('Input should be a valid dictionary');
      for (const [key, inner] of Object.entries(value)) {
        const known = rule.properties && rule.properties[key];
        if (known) check(inner, known, path ? `${path}.${key}` : key, errors);
        else if (rule.additionalProperties && typeof rule.additionalProperties === 'object') check(inner, rule.additionalProperties, `${path}.${key}`, errors);
        else if (rule.additionalProperties === false) errors.push({ field: path ? `${path}.${key}` : key, message: 'Extra inputs are not permitted' });
      }
      break;
    default: break;
  }
  if (typeof value === 'number') {
    if (rule.minimum !== undefined && value < rule.minimum) say(`Input should be greater than or equal to ${rule.minimum}`);
    if (rule.maximum !== undefined && value > rule.maximum) say(`Input should be less than or equal to ${rule.maximum}`);
    if (rule.exclusiveMinimum !== undefined && value <= rule.exclusiveMinimum) say(`Input should be greater than ${rule.exclusiveMinimum}`);
    if (rule.exclusiveMaximum !== undefined && value >= rule.exclusiveMaximum) say(`Input should be less than ${rule.exclusiveMaximum}`);
  }
  if (typeof value === 'string') {
    if (rule.maxLength !== undefined && value.length > rule.maxLength) say(`String should have at most ${rule.maxLength} characters`);
    if (rule.minLength !== undefined && value.length < rule.minLength) say(`String should have at least ${rule.minLength} characters`);
    if (rule.pattern && !new RegExp(rule.pattern).test(value)) say(`String should match pattern '${rule.pattern}'`);
  }
  return undefined;
}

// Removing a setting puts it back to its default, as the real merge patch does.
function withDefaults(value, fallback) {
  if (!fallback || typeof fallback !== 'object' || Array.isArray(fallback)) return value === undefined ? clone(fallback) : value;
  const out = value && typeof value === 'object' && !Array.isArray(value) ? { ...value } : {};
  for (const [key, inner] of Object.entries(fallback)) out[key] = withDefaults(out[key], inner);
  return out;
}

export function changeConfig(patch) {
  if (!patch || typeof patch !== 'object' || Array.isArray(patch)) throw bad('a config patch is a mapping of sections to new values');
  const next = withDefaults(mergePatch(db.config, patch), { ...configData.defaults, network: { ...configData.defaults.network, gateways: undefined } });
  if (!next.network.gateways) next.network.gateways = {};
  const refused = configData.immutable.filter((dotted) => JSON.stringify(at(next, dotted)) !== JSON.stringify(at(db.config, dotted)));
  if (refused.length) {
    throw new ApiError(403, 'immutable_setting', `${refused.join(', ')} cannot be changed through the API: it decides which program the daemon runs. `
      + 'Change it in config.yaml or with `sf config set`, then restart the daemon');
  }
  const errors = [];
  check(next, schema, '', errors);
  if (errors.length) {
    throw new ApiError(400, 'bad_request', `${errors[0].field}: ${errors[0].message}${errors.length > 1 ? ` (and ${errors.length - 1} more)` : ''}`, { detail: errors });
  }
  const before = flatten(db.config, '', {});
  const after = flatten(next, '', {});
  const changed = [...new Set([...Object.keys(before), ...Object.keys(after)])].filter((key) => before[key] !== after[key]).sort();
  db.config = next;
  return {
    config: next, changed,
    restart_android: changed.filter((key) => ANDROID_RESTART.some((prefix) => key.startsWith(prefix))),
    restart_daemon: changed.filter((key) => DAEMON_RESTART.some((prefix) => key.startsWith(prefix))),
  };
}

route('PATCH', '/api/config', ({ body }) => changeConfig(body.patch));

// ------------------------------------------------------------------- images
const BUILT_FOR = ['13'];          // the camera driver and the NVIDIA runtime are built for Android 13 only
function images() {
  return Object.entries(db.config.android.images).sort((a, b) => Number(a[0]) - Number(b[0])).map(([version, entry]) => {
    const feature = (name) => (BUILT_FOR.includes(version)
      ? { supported: true, available: name === 'nvidia', unsupported: '', reason: name === 'nvidia' ? '' : 'not built yet; see native/camera/extract_hal.sh' }
      : { supported: false, available: false, unsupported: `not available on Android ${version}: built for Android 13 only`, reason: `not available on Android ${version}: built for Android 13 only` });
    const features = { camera: feature('camera'), nvidia: feature('nvidia') };
    return {
      version, base: entry.base, tag: entry.tag || `superfarm/android:${version}`, custom: Boolean(entry.tag), built: db.images.built.includes(version),
      base_present: db.images.pulled.includes(version), overlay_dir: `${HOME}/overlays/android-${version}`, overlays: [], stale: false, stale_reason: '',
      arm_translation: entry.arm_translation, overlays_available: { camera_hal: features.camera.available, nvidia_runtime: features.nvidia.available }, features,
    };
  });
}
route('GET', '/api/images', () => ({ images: images() }));

registerEffect('image', (version, what) => {
  if (what === 'pulled' && !db.images.pulled.includes(version)) db.images.pulled.push(version);
  if (what === 'built' && !db.images.built.includes(version)) db.images.built.push(version);
});

function imageTask(version, action) {
  if (!db.config.android.images[version]) throw notFound(`Android '${version.slice(0, 32)}' has no image configured (android.images)`);
  const pulled = db.images.pulled.includes(version);
  const plan = [];
  let after = 0.4;
  if (!pulled) {
    plan.push({ after, say: `pulling ${db.config.android.images[version].base} (pretend: nothing is downloaded)` });
    after += 5;
    plan.push({ after, effect: ['image', version, 'pulled'], say: 'pulled' });
  }
  if (action === 'build' || !db.images.built.includes(version)) {
    plan.push({ after: after + 0.3, say: `building superfarm/android:${version}` });
    after += 4;
  }
  plan.push({ after: after + 0.2, effect: ['image', version, 'built'], say: 'ready' });
  return submit(action === 'build' ? 'image-build' : 'image-ensure', `${action === 'build' ? 'Build' : 'Prepare'} the Android ${version} image`, plan);
}
route('POST', '/api/images/{version}/ensure', ({ params }) => accepted(imageTask(params.version, 'ensure')));
route('POST', '/api/images/{version}/build', ({ params }) => accepted(imageTask(params.version, 'build')));

// -------------------------------------------------------------------- tasks
route('GET', '/api/tasks', ({ query }) => {
  const limit = whole(query.limit ?? 50, 1, 500, 'limit');
  const list = truthy(query.active) ? db.tasks.filter((task) => ACTIVE.has(task.state)) : db.tasks;
  return { tasks: list.slice(0, limit).map(publicTask) };
});
route('GET', '/api/tasks/{task_id}', ({ params }) => {
  const task = findTask(params.task_id);
  if (!task) throw notFound(`no task '${params.task_id.slice(0, 40)}' (finished tasks are kept for a while, then forgotten)`);
  return { task: publicTask(task) };
});
route('POST', '/api/tasks/{task_id}/cancel', ({ params }) => {
  const task = findTask(params.task_id);
  if (!task) throw notFound(`no task '${params.task_id.slice(0, 40)}'`);
  if (!cancelTask(task) && task.state !== 'cancelled') throw conflict(`task ${task.id} has already finished (${task.state})`);
  return { task: publicTask(task) };
});
