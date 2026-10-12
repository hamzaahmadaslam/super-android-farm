// What a visitor finds when the demo opens: a small invented farm that is already doing
// something. Every name, app, address and number below is made up.
//
// Addresses come only from the ranges reserved for documentation (RFC 5737), which are
// never routed on the internet:
//   192.0.2.0/24      the shared gateway's own network
//   198.51.100.0/24   VPN servers and tunnel addresses
//   203.0.113.0/24    public addresses
// The exception is 127.0.0.1, where the real panel shows each Android's ADB port.

import configData from './generated/config.js';
import flows from './generated/flows.js';
import host from './generated/host.js';
import meta from './generated/meta.js';
import { isoLocal, stamp } from './util.js';

// The invented machine: 32 threads, 64 GB of RAM, one integrated and one discrete GPU.
export const HOST = host.host;
export const HOME = host.home;
export const GPUS = host.gpus;
export const HOST_PUBLIC_IP = '203.0.113.5';
export const MAX_ANDROIDS = 300;          // the demo's own ceiling, so a tab's storage is never filled

// Device presets. The real farm ships presets of real phone models; the demo invents its
// own, in the same shape, so that no real product name appears in it.
export const DEVICES = Object.fromEntries([
  ['example-one', 'Example One', 'EX-100', 1080, 2400, 420], ['example-one-pro', 'Example One Pro', 'EX-110', 1440, 3120, 560],
  ['example-lite', 'Example Lite', 'EX-050', 720, 1600, 280], ['example-fold', 'Example Fold', 'EX-F20', 1812, 2176, 420],
  ['sample-s', 'Sample S', 'SM-S01', 1080, 2340, 440], ['sample-tab', 'Sample Tab', 'SM-T10', 1600, 2560, 320],
].map(([key, label, model, width, height, dpi]) => [key, {
  label, brand: key.split('-')[0], manufacturer: key.split('-')[0], model, device: model.toLowerCase().replace('-', ''), name: `${model.toLowerCase().replace('-', '')}xx`,
  screen: { width, height, dpi },
}]));
const devices = DEVICES;
const PROFILE_NAMES = Object.keys(devices);

const APPS = [
  { package: 'example.notes', label: 'Paper Notes', version_name: '2.4.1', version_code: 241, format: 'apk', size: 6_512_640, splits: [], abis: [] },
  { package: 'example.notes', label: 'Paper Notes', version_name: '2.3.0', version_code: 230, format: 'apk', size: 6_340_608, splits: [], abis: [] },
  { package: 'example.weather', label: 'Cloudline Weather', version_name: '1.9.0', version_code: 190, format: 'xapk', size: 48_234_496,
    splits: ['config.x86_64', 'config.arm64_v8a', 'config.xhdpi', 'config.en'], abis: ['x86_64', 'arm64-v8a'] },
  { package: 'example.tilegame', label: 'Tile Garden', version_name: '3.0.2', version_code: 302, format: 'apks', size: 91_226_112,
    splits: ['config.x86_64', 'config.mdpi'], abis: ['x86_64'] },
];

export function libraryEntry(app, added, original) {
  return {
    id: `${app.package}-${app.version_code}`, package: app.package, version_code: app.version_code, version_name: app.version_name,
    label: app.label, min_sdk: 26, target_sdk: 34, abis: app.abis, permissions: ['android.permission.INTERNET'],
    launcher: `${app.package}.MainActivity`, debuggable: false, split_required: app.splits.length > 0, format: app.format,
    original: original || `${app.package}-${app.version_name}.${app.format}`, added, size: app.size,
    sha256: 'demo'.padEnd(64, '0'), files: [{ name: 'base.apk', split: '', size: app.size }], splits: app.splits, obb: [], warnings: [],
  };
}

// One Android's saved settings (InstanceSpec), from the real model's defaults.
export function newSpec(id, created, { name = '', template = {}, profile } = {}) {
  const spec = { id, created, ...JSON.parse(JSON.stringify(meta.spec)) };
  const pick = profile || PROFILE_NAMES[(id * 7) % PROFILE_NAMES.length];
  const preset = devices[pick] || {};
  spec.name = name;
  spec.device = {
    ...spec.device, profile: pick,
    android_id: identity(`android-id-${id}-${created}`, 16, '0123456789abcdef'),
    serial: `R${identity(`serial-${id}-${created}`, 10, '0123456789ABCDEFGHJKLMNPQRSTUVWXYZ')}`,
    props: {
      'ro.product.brand': preset.brand || '', 'ro.product.manufacturer': preset.manufacturer || '',
      'ro.product.model': preset.model || '', 'ro.product.device': preset.device || '', 'ro.product.name': preset.name || '',
    },
  };
  for (const [key, value] of Object.entries(template)) {
    if (value && typeof value === 'object' && !Array.isArray(value) && spec[key] && typeof spec[key] === 'object') spec[key] = { ...spec[key], ...value };
    else spec[key] = value;
  }
  return spec;
}

function identity(text, length, alphabet) {
  let h = 2166136261;
  let out = '';
  for (let i = 0; out.length < length; i++) {
    for (const ch of `${text}:${i}`) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
    out += alphabet[(h >>> 0) % alphabet.length];
  }
  return out;
}

function instance(id, t, { name, tags = [], template = {}, running = false, mode = '', apps = {}, ago = 3600 }) {
  return {
    spec: newSpec(id, t - 86400 * 3 - id * 600, { name, template: { tags, ...template } }),
    phase: running ? 'booted' : 'stopped',        // missing | stopped | starting | running | booted | error
    activity: '', lastError: '', lastStart: running ? t - ago : null,
    mode: running ? mode : '', dataVersion: running || id < 7 ? '13' : '',
    apps, values: {}, netSince: running ? t - ago + 9 : 0, restarts: 0, publicChecked: running,
    device: { locale: 'en-US', timezone: 'Europe/Berlin', dark_mode: false, animations: false, font_scale: 1, proxy: '' },
  };
}

const installed = (list) => Object.fromEntries(list.map(([pkg, name, code]) => [pkg, { version: name, version_code: code, enabled: true }]));

export function seed(t) {
  const config = JSON.parse(JSON.stringify(configData.defaults));
  config.farm_name = 'Super Farm demo';
  config.network.gateways = { eu1: { provider: 'openvpn', profile: 'example-berlin.ovpn', credentials: 'default', subnet: '192.0.2.0/24' } };
  // The one model profile of the demo: a script, not a server. The provider is one the settings accept; the address is
  // not an address at all, and nothing is ever sent anywhere (routes/ai.js).
  config.ai.models = { 'demo-script': { provider: 'ollama', base_url: 'demo-script://no-server', model: 'scripted-stand-in', api_key_secret: '', tools: true, vision: false, timeout: 120, temperature: 0.1, num_ctx: 16384, max_tokens: 1024, headers: {}, extra: {} } };
  config.ai.default_model = 'demo-script';

  const instances = {
    1: instance(1, t, { name: 'Checkout tests 1', tags: ['eu', 'tests'], running: true, mode: 'nvidia', ago: 5400,
      apps: installed([['example.notes', '2.4.1', 241], ['example.weather', '1.9.0', 190]]) }),
    2: instance(2, t, { name: 'Checkout tests 2', tags: ['eu', 'tests'], running: true, mode: 'nvidia', ago: 5380,
      apps: installed([['example.notes', '2.3.0', 230]]) }),
    3: instance(3, t, { name: 'Berlin 1', tags: ['eu', 'vpn'], running: true, mode: 'nvidia', ago: 2700,
      template: { network: { mode: 'vpn', vpn_profile: 'example-berlin.ovpn' } },
      apps: installed([['example.notes', '2.4.1', 241], ['example.tilegame', '3.0.2', 302]]) }),
    4: instance(4, t, { name: 'Berlin 2', tags: ['eu', 'vpn'], running: true, mode: 'nvidia', ago: 2650,
      template: { network: { mode: 'gateway', gateway: 'eu1' } }, apps: installed([['example.weather', '1.9.0', 190]]) }),
    5: instance(5, t, { name: 'Tokyo 1', tags: ['asia', 'vpn'], running: true, mode: 'intel', ago: 900,
      template: { gpu_mode: 'igpu', network: { mode: 'vpn', vpn_profile: 'example-tokyo.ovpn' } }, apps: installed([['example.notes', '2.4.1', 241]]) }),
    6: instance(6, t, { name: 'Spare', tags: ['spare'], template: { gpu_mode: 'software' }, apps: installed([['example.notes', '2.3.0', 230]]) }),
    7: instance(7, t, { name: '', tags: [] }),
  };
  instances[7].phase = 'missing';

  const day = 86400;
  return {
    session: { signedIn: false },
    counters: { task: 3, run: 1 },
    config,
    instances,
    tasks: [
      doneTask('t3', 'backup.manual', 'back up 2 Android(s)', [1, 3], t - 7200, 41, { 1: 'backed up 1.4 GB in 21 s', 3: 'backed up 1.1 GB in 19 s' }, '2 of 2 backed up', { pruned: [] }),
      doneTask('t2', 'app-install', 'Install Paper Notes 2.4.1', [1, 3, 5], t - 9000, 14, { 1: 'installed', 3: 'installed', 5: 'installed' }, '3 of 3 succeeded'),
      doneTask('t1', 'instance.start', 'start 5 Android(s)', [1, 2, 3, 4, 5], t - 9300, 26,
        { 1: 'booted in 11.2 s (nvidia)', 2: 'booted in 11.4 s (nvidia)', 3: 'booted in 11.1 s (nvidia)', 4: 'booted in 11.6 s (nvidia)', 5: 'booted in 17.3 s (intel)' }, '5 of 5 succeeded'),
    ],
    profiles: [
      { name: 'example-berlin.ovpn', provider: 'openvpn', size: 2874, modified: t - 9 * day, valid: true, error: '',
        servers: ['198.51.100.10:1194/udp', '198.51.100.11:1194/udp'], removed: [], needs_credentials: true },
      { name: 'example-tokyo.ovpn', provider: 'openvpn', size: 2911, modified: t - 9 * day, valid: true, error: '',
        servers: ['198.51.100.77:443/tcp'], removed: ['up', 'down'], needs_credentials: true },
    ],
    credentials: ['default'],
    gateways: { eu1: { up: true, since: t - 2700 } },
    library: [libraryEntry(APPS[0], t - 4 * day), libraryEntry(APPS[1], t - 20 * day), libraryEntry(APPS[2], t - 6 * day), libraryEntry(APPS[3], t - 2 * day)],
    media: [{ id: 'street-walk.mp4', name: 'street-walk.mp4', kind: 'video', size: 18_350_080, modified: t - 5 * day }],
    camera: { feeds: [], muted: [] },
    listening: null,
    windows: [],
    images: { built: ['13'], pulled: ['13'] },
    doctorRan: t - 600,
    automation: seedAutomation(t),
    backups: [
      backup('farm', null, '', t - day, 18_432, ''),
      backup('android', 3, 'Berlin 1', t - 7200 + 20, 1_181_116_006, 'manual'),
      backup('android', 1, 'Checkout tests 1', t - 7200, 1_503_238_553, 'manual'),
    ],
    approvals: seedApprovals(t),
    assistant: { nextAt: 0, round: 0 },
    ai: { conversations: [], sessions: [], held: {}, seq: 0 },
    shell: {},
  };
}

function doneTask(id, kind, title, targets, created, seconds, details, message, extra = {}) {
  const results = Object.fromEntries(Object.entries(details).map(([key, detail]) => [key, { ok: true, detail }]));
  return {
    id, kind, title, targets, state: 'done', current: targets.length, total: targets.length, message, error: '', results,
    output: { ok: targets.length, failed: 0, ...extra }, created, started: created + 0.01, finished: created + seconds, plan: [],
  };
}

export function backup(kind, id, name, created, size, reason, extra = {}) {
  const folder = kind === 'farm' ? 'farm' : `android-${id}`;
  const label = stamp(created);
  const dir = `${HOME}/backups/${folder}/${label}`;
  return {
    name: `${folder}/${label}`, dir, file: `${dir}/${kind === 'farm' ? 'farm.tar.gz' : 'data.tar.zst'}`, kind,
    id: kind === 'farm' ? null : id, instance_name: name, android_id: '', created, reason: kind === 'farm' ? 'manual' : reason, size,
    sha256: 'demo'.padEnd(64, '0'), compression: kind === 'farm' ? '' : 'zstd', consistent: kind !== 'farm',
    android_version: kind === 'farm' ? '' : '13', includes_secrets: false, present: true, ...extra,
  };
}

function seedAutomation(t) {
  const created = isoLocal(t - 2 * 86400);
  const preview = ['launch example.notes', "tap 'New note'", 'type {title}', "tap 'Save'"];
  const recording = (n, instanceId) => ({
    id: `r00${n}-10${n}512`, created, device: `127.0.0.1:1700${instanceId}`, instance: instanceId, steps: 4, taps: 2, seconds: 19 + n,
    note: '', dynamic: ['title'], preview,
  });
  const program = {
    slug: 'new-note', name: 'New note', goal: 'Create a note with a given title.', package: 'example.notes', created, model: '',
    steps: 4, optional: 0, with_selector: 3, dynamic: ['title'], from_recordings: ['r001-101512', 'r002-102512', 'r003-103512'], warnings: [], notes: [],
    outline: [
      { id: 'p000', kind: 'launch', label: 'launch example.notes', selector: {}, optional: false, dynamic: null, seen_in: 3, shot: null, has_selector: false },
      { id: 'p001', kind: 'tap', label: "tap 'New note'", selector: { id: 'new_note', text: 'New note', cls: 'Button' }, optional: false, dynamic: null, seen_in: 3, shot: null, has_selector: true },
      { id: 'p002', kind: 'type', label: 'type {title}', selector: { id: 'title', cls: 'EditText' }, optional: false, dynamic: 'title', seen_in: 3, shot: null, has_selector: true },
      { id: 'p003', kind: 'tap', label: "tap 'Save'", selector: { id: 'save', text: 'Save', cls: 'Button' }, optional: false, dynamic: null, seen_in: 3, shot: null, has_selector: true },
    ],
  };
  const smoke = `# Opens the notes app and checks that its first screen is there.
description: Smoke test for Paper Notes
package: example.notes
variables: {title: Hello}
steps:
  - launch: example.notes
  - wait_for: {text: "New note"}
  - tap: {text: "New note"}
  - type: {text: "{title}"}
  - back: {}
`;
  return {
    datasets: [{
      slug: 'new-note', name: 'New note', description: 'Create a note with a given title.', package: 'example.notes', created, updated: created,
      recordings: [recording(1, 1), recording(2, 1), recording(3, 2)],
    }],
    programs: [program],
    flows: { 'smoke-test': smoke },
    examples: flows.examples.map((example) => example.name),
    values: { shared: { title: 'Shopping list', city: 'Berlin' }, pools: { 'invite-codes': { lines: ['DEMO-4F2A', 'DEMO-91C7', 'DEMO-77B0'], used: [{ value: 'DEMO-0A1E', at: t - 9000, instance: 1, run: 't0' }] } } },
    runs: [{
      id: 'r0', kind: 'flow', name: 'smoke-test', dry_run: false, created: t - 8000, state: 'done', task: '', cancelled: false,
      devices: {
        1: finishedDevice(1, 5, 'back'), 2: finishedDevice(2, 5, 'back'),
      },
    }],
    schedules: [{
      id: 'a1b2c3d4e5f6', name: 'nightly smoke test', kind: 'flow', target: 'smoke-test', selector: 'tag:tests', every: 0, cron: '0 4 * * *',
      variables: {}, dry_run: false, enabled: true, created: t - 6 * 86400, next_run: nextAt(t, 4), last_run: nextAt(t, 4) - 86400, last_task: '',
      last_result: '2 of 2 finished', runs: 6, skipped: 0,
    }],
    recording: {},
  };
}

export function finishedDevice(id, steps, label) {
  return {
    id, label: `#${id}`, serial: `127.0.0.1:${17000 + id}`, state: 'done', step: steps - 1, steps, step_label: label, message: '', error: '',
    waiting: null, done: steps, skipped: 0, recovered: 0, issues: [], dry_run: false, elapsed: 6.4, log: [],
  };
}

// The next time the clock shows `hour`:00.
export function nextAt(t, hour) {
  const d = new Date(t * 1000);
  d.setHours(hour, 0, 0, 0);
  if (d.getTime() / 1000 <= t) d.setDate(d.getDate() + 1);
  return d.getTime() / 1000;
}

function seedApprovals(t) {
  const past = (id, ago, tool, klass, summary, targets, state, extra = {}) => ({
    id, created: t - ago, expires: t - ago + 300, caller: 'mcp:demo-assistant (simulated)', session: 'demo-session-1', tool, klass, summary,
    arguments: { selector: targets.join(',') }, targets, can_remember: klass === 'change', state,
    decided_by: 'panel (demo visitor)', decided_at: state === 'expired' ? t - ago + 300 : t - ago + 40, reason: '', remembered: false, ...extra,
  });
  return [
    past('3c1d9a70', 5200, 'restart_androids', 'change', 'Restart Android 2', [2], 'approved'),
    past('8e02b4f1', 6100, 'delete_androids', 'destructive', 'Delete Android 6 and its data', [6], 'denied', { reason: 'It is the spare; keep it.' }),
    past('51aa07c3', 9000, 'stop_androids', 'change', 'Stop Android 5', [5], 'expired', { decided_by: 'timeout', reason: 'nobody answered in time' }),
  ];
}

export { APPS };
