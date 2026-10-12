// Apps, files, device settings and shell commands across the fleet.
//
// "Uploading" an app or a file records its name and size. The mock never opens the file:
// it is not read, not stored and not sent anywhere.

import { accepted, created, route, truthy, whole } from '../backend.js';
import * as device from '../device.js';
import { ApiError, bad, conflict, findTask, fleet, memoryOf, notFound, publicTask, record, registerEffect, resolved, select } from '../farm.js';
import { libraryEntry } from '../seed.js';
import { db, hash, now, round } from '../store.js';

const PACKAGE = /^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$/;
const FORMATS = ['.apk', '.xapk', '.apks', '.apkm'];

const selectorOf = (body) => {
  if (typeof body.selector !== 'string' || !body.selector) throw new ApiError(422, 'validation_error', 'selector: Field required', { detail: [{ field: 'selector', message: 'Field required' }] });
  return select(body.selector);
};
const packageOf = (body) => {
  if (typeof body.package !== 'string' || !PACKAGE.test(body.package)) throw bad(`bad package name '${String(body.package).slice(0, 60)}'`);
  return body.package;
};
const libraryApp = (id) => {
  const app = db.library.find((entry) => entry.id === id) || db.library.find((entry) => entry.package === id);
  if (!app) throw notFound(`no app '${String(id).slice(0, 80)}' in the library`);
  return app;
};

// -------------------------------------------------------------- app library
route('GET', '/api/apps/library', () => ({ apps: db.library }));

// An invented app is made from the file's name: the package always lives under
// "example.", and the version is 1.0.0 unless the name carries one.
route('POST', '/api/apps/library', ({ file }) => {
  if (!file) throw bad('no file in the request');
  const dot = file.name.lastIndexOf('.');
  const ext = dot < 0 ? '' : file.name.slice(dot).toLowerCase();
  if (!FORMATS.includes(ext)) throw bad(`${file.name.slice(0, 80)}: unsupported file type; the library takes ${FORMATS.join(', ')}`);
  const stem = file.name.slice(0, dot).replace(/[-_ ]?v?(\d+(\.\d+){1,3})$/, '');
  const version = (/(\d+(?:\.\d+){1,3})$/.exec(file.name.slice(0, dot)) || [null, '1.0.0'])[1];
  const word = stem.toLowerCase().replace(/[^a-z0-9]+/g, '').slice(0, 24) || 'app';
  const code = version.split('.').reduce((sum, part) => sum * 100 + (Number(part) || 0), 0);
  const label = stem.replace(/[-_.]+/g, ' ').trim().replace(/\b\w/g, (c) => c.toUpperCase()).slice(0, 40) || 'Uploaded App';
  const app = libraryEntry({ package: `example.${word}`, label, version_name: version, version_code: code, format: ext.slice(1), size: file.size,
    splits: ext === '.apk' ? [] : ['config.x86_64', 'config.en'], abis: ext === '.apk' ? [] : ['x86_64'] }, now(), file.name.slice(0, 120));
  app.warnings = ['demo: the file was not opened; name, package and version are made up from its file name'];
  db.library = [app, ...db.library.filter((entry) => entry.id !== app.id)];
  return created({ app });
});

route('DELETE', '/api/apps/library/{app_id}', ({ params }) => {
  if (!db.library.some((entry) => entry.id === params.app_id)) throw notFound(`no app '${params.app_id.slice(0, 80)}' in the library`);
  db.library = db.library.filter((entry) => entry.id !== params.app_id);
  return { deleted: params.app_id };
});

// --------------------------------------------------------------- app actions
registerEffect('install', (id, appId, onlyIfOlder) => {
  const rec = db.instances[id];
  const app = db.library.find((entry) => entry.id === appId);
  if (!rec || !app) return { ok: false, detail: 'the app is no longer in the library' };
  const have = rec.apps[app.package];
  if (onlyIfOlder && have && have.version_code >= app.version_code) return { detail: `already has ${have.version}` };
  rec.apps[app.package] = { version: app.version_name, version_code: app.version_code, enabled: true };
  if (device.ui(rec)) device.ui(rec).rev += 1;
  return { detail: have ? `updated ${have.version} -> ${app.version_name}` : `installed ${app.version_name}${app.splits.length ? ` (${Math.min(2, app.splits.length)} of ${app.splits.length} splits)` : ''}` };
});

registerEffect('app', (id, action, pkg) => {
  const rec = db.instances[id];
  if (!rec) return { ok: false, detail: `there is no Android ${id}` };
  const have = rec.apps[pkg];
  const builtin = pkg === 'example.settings' || pkg === 'example.notepad';
  if (!have && !builtin) return { ok: false, detail: `${pkg} is not installed` };
  const state = device.ui(rec);
  switch (action) {
    case 'uninstall':
      if (builtin) return { ok: false, detail: `${pkg} is part of the system image` };
      delete rec.apps[pkg];
      delete state.lists[pkg];
      if (state.app === pkg) device.home(rec);
      state.rev += 1;
      return { detail: 'uninstalled' };
    case 'launch':
      if (have && !have.enabled) return { ok: false, detail: `${pkg} is disabled` };
      device.launch(rec, pkg);
      return { detail: 'launched' };
    case 'stop': if (state.app === pkg) device.home(rec); return { detail: 'stopped' };
    case 'clear': delete state.lists[pkg]; if (state.app === pkg) device.home(rec); state.rev += 1; return { detail: 'data cleared' };
    case 'enable': if (have) have.enabled = true; return { detail: 'enabled' };
    case 'disable': if (have) have.enabled = false; if (state.app === pkg) device.home(rec); return { detail: 'disabled' };
    case 'grant': return { detail: 'granted' };
    case 'revoke': return { detail: 'revoked' };
    default: return { ok: false, detail: `unknown action ${action}` };
  }
});

route('POST', '/api/apps/install', ({ body }) => {
  const who = selectorOf(body);
  const app = libraryApp(body.app_id);
  return accepted(fleet('app-install', `Install ${app.label} ${app.version_name}`, who, (id) => ({ effect: ['install', id, app.id, false] }),
    { each: 1.4, first: 1.6 }));
});
route('POST', '/api/apps/ensure', ({ body }) => {
  const who = selectorOf(body);
  const app = libraryApp(body.app_id);
  return accepted(fleet('app-ensure', `Ensure ${app.label} ${app.version_name}`, who, (id) => ({ effect: ['install', id, app.id, true] }), { each: 0.9, first: 1.0 }));
});

const TITLE = { uninstall: 'Uninstall', launch: 'Launch', stop: 'Stop', clear: 'Clear data of', enable: 'Enable', disable: 'Disable', grant: 'Grant permissions to', revoke: 'Revoke permissions of' };
for (const action of Object.keys(TITLE)) {
  route('POST', `/api/apps/${action}`, ({ body }) => {
    const who = selectorOf(body);
    const pkg = packageOf(body);
    return accepted(fleet(`app-${action}`, `${TITLE[action]} ${pkg}`, who, (id) => ({ effect: ['app', id, action, pkg] }), { each: 0.35 }));
  });
}

const SYSTEM_APPS = [['example.launcher', '13'], ['example.settings', '13'], ['example.notepad', '13']];
function installed(rec, thirdParty) {
  const rows = Object.entries(rec.apps).map(([pkg, app]) => ({
    package: pkg, version_code: app.version_code, version_name: app.version, uid: 10100 + Math.floor(hash(pkg) * 80), system: false, enabled: app.enabled,
    installed: true, installer: '', updated: '', version: app.version, label: (db.library.find((entry) => entry.package === pkg) || {}).label || '',
  }));
  if (!thirdParty) {
    rows.push(...SYSTEM_APPS.map(([pkg, version]) => ({
      package: pkg, version_code: 33, version_name: version, uid: 1000, system: true, enabled: true, installed: true, installer: '', updated: '', version, label: '',
    })));
  }
  return rows.sort((a, b) => a.package.localeCompare(b.package));
}

route('GET', '/api/apps/matrix', ({ query }) => {
  const who = select(query.selector || 'all');
  const packages = {};
  const errors = {};
  for (const id of who) {
    const rec = db.instances[id];
    if (rec.phase !== 'booted') { errors[id] = `Android ${id} is not running`; continue; }
    for (const row of installed(rec, true)) (packages[row.package] = packages[row.package] || {})[id] = { version: row.version, version_code: row.version_code, enabled: row.enabled };
  }
  const sorted = Object.fromEntries(Object.entries(packages).sort((a, b) => a[0].localeCompare(b[0])));
  if (truthy(query.detail)) return { instances: who, packages: sorted, errors };
  return { matrix: Object.fromEntries(Object.entries(sorted).map(([pkg, cells]) => [pkg, Object.fromEntries(Object.entries(cells).map(([id, cell]) => [id, cell.version]))])) };
});

function booted(id) {
  const rec = record(id);
  if (rec.phase !== 'booted') throw new ApiError(409, 'device_unavailable', `Android ${rec.spec.id} is not running; start it first`);
  return rec;
}

route('GET', '/api/instances/{id}/apps', ({ params, query }) => ({ apps: installed(booted(params.id), query.third_party === undefined ? true : truthy(query.third_party)) }));

// -------------------------------------------------------------------- files
const TREE = {
  '/sdcard': [['Alarms', 0], ['DCIM', 0], ['Documents', 0], ['Download', 0], ['Movies', 0], ['Music', 0], ['Pictures', 0]],
  '/sdcard/DCIM': [['Camera', 0]],
  '/sdcard/Download': [['welcome.txt', 412]],
  '/sdcard/Documents': [['notes-export.txt', 1_840]],
};
const FOLDERS = new Set(Object.entries(TREE).flatMap(([folder, entries]) => [folder, ...entries.filter(([, size]) => !size).map(([name]) => `${folder}/${name}`)]));
const cleanPath = (path) => {
  const value = String(path || '/sdcard').replace(/\/+$/, '') || '/sdcard';
  if (!/^\/sdcard(\/|$)/.test(value) || value.includes('..')) throw bad('the path must be under /sdcard');
  return value;
};

route('GET', '/api/instances/{id}/files', ({ params, query }) => {
  const rec = booted(params.id);
  const path = cleanPath(query.path);
  const fixed = (TREE[path] || []).map(([name, size]) => ({ name, path: `${path}/${name}`, type: size ? 'file' : 'dir', size: size || 4096, modified: Math.round(rec.spec.created), mode: size ? '660' : '770' }));
  const pushed = (rec.files || []).filter((entry) => entry.path.slice(0, entry.path.lastIndexOf('/')) === path)
    .map((entry) => ({ name: entry.path.slice(entry.path.lastIndexOf('/') + 1), path: entry.path, type: 'file', size: entry.size, modified: Math.round(entry.modified), mode: '660' }));
  if (!FOLDERS.has(path)) throw notFound(`${path} does not exist on the device`);
  const entries = [...fixed, ...pushed].sort((a, b) => (a.type !== 'dir') - (b.type !== 'dir') || a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
  return { path, entries };
});

registerEffect('push', (id, path, size) => {
  const rec = db.instances[id];
  if (!rec) return { ok: false, detail: `there is no Android ${id}` };
  rec.files = [...(rec.files || []).filter((entry) => entry.path !== path), { path, size, modified: now() }].slice(-40);
  return { detail: `pushed to ${path}` };
});

route('POST', '/api/files/push', ({ body, query, file }) => {
  if (!file) throw bad('no file in the request');
  const who = select(body.selector || query.selector || '');
  const remote = body.remote || query.remote || '/sdcard/Download/';
  const folder = cleanPath(remote.endsWith('/') ? remote : remote.slice(0, remote.lastIndexOf('/')));
  const name = remote.endsWith('/') ? file.name.replace(/[/\\]/g, '_').slice(0, 120) : remote.slice(remote.lastIndexOf('/') + 1);
  return accepted(fleet('file-push', `Push ${name}`, who, (id) => ({ effect: ['push', id, `${folder}/${name}`, file.size] }), { each: 0.6 }));
});
route('POST', '/api/files/gallery', ({ body, query, file }) => {
  if (!file) throw bad('no file in the request');
  const who = select(body.selector || query.selector || '');
  const name = file.name.replace(/[/\\]/g, '_').slice(0, 120);
  return accepted(fleet('file-gallery', `Add ${name} to the gallery`, who, (id) => ({ effect: ['push', id, `/sdcard/Pictures/${name}`, file.size], detail: 'added to the gallery' }), { each: 0.6 }));
});

// ----------------------------------------------------------- device settings
registerEffect('device', (id, field, value, detail) => {
  const rec = db.instances[id];
  if (!rec) return { ok: false, detail: `there is no Android ${id}` };
  if (field === 'wake') device.wake(rec);
  else if (field === 'reboot') device.home(rec);
  else if (field === 'open-url') device.openLink(rec, value);
  else if (field !== 'clipboard') { rec.device[field] = value; device.ui(rec).rev += 1; }
  return { detail };
});

const setting = (kind, title, read) => route('POST', `/api/device/${kind}`, ({ body }) => {
  const who = selectorOf(body);
  const [heading, field, value, detail] = read(body);
  return accepted(fleet(`device-${kind}`, heading || title, who, (id) => ({ effect: ['device', id, field, value, detail] }), { each: 0.3 }));
});
const text = (body, field, max) => {
  if (typeof body[field] !== 'string' || !body[field] || body[field].length > max) throw new ApiError(422, 'validation_error', `${field}: Field required`, { detail: [{ field, message: 'Field required' }] });
  return body[field];
};
const flag = (body) => {
  if (typeof body.enabled !== 'boolean') throw new ApiError(422, 'validation_error', 'enabled: Input should be a valid boolean');
  return body.enabled;
};
setting('wake', 'Wake and unlock', () => ['', 'wake', null, 'awake']);
setting('reboot', 'Restart Android (soft)', () => ['', 'reboot', null, 'framework restarted in 6.2 s']);
setting('open-url', 'Open a link', (body) => { const url = text(body, 'url', 2048); return [`Open ${url.slice(0, 60)}`, 'open-url', url, 'opened']; });
setting('clipboard', 'Set the clipboard', (body) => { text(body, 'text', 100000); return ['', 'clipboard', null, 'clipboard set']; });
setting('locale', 'Set locale', (body) => { const tag = text(body, 'locale', 35); if (!/^[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,8})*$/.test(tag)) throw bad(`bad locale '${tag}'`); return [`Set locale ${tag}`, 'locale', tag, `locale ${tag}`]; });
setting('timezone', 'Set time zone', (body) => { const zone = text(body, 'timezone', 100); if (!/^[A-Za-z_]+(\/[A-Za-z0-9_+-]+)*$/.test(zone)) throw bad(`bad time zone '${zone}'`); return [`Set time zone ${zone}`, 'timezone', zone, `time zone ${zone}`]; });
setting('geo', 'Set location', (body) => {
  if (body.lat == null && body.lon == null) return ['Remove mock location', 'geo', '', 'mock location removed'];
  if (typeof body.lat !== 'number' || typeof body.lon !== 'number' || Math.abs(body.lat) > 90 || Math.abs(body.lon) > 180) throw bad('latitude is -90..90 and longitude -180..180');
  return [`Set location ${body.lat.toFixed(4)},${body.lon.toFixed(4)}`, 'geo', `${body.lat},${body.lon}`, 'mock location set'];
});
setting('proxy', 'Set proxy', (body) => { const value = typeof body.proxy === 'string' ? body.proxy : ''; if (value && !/^[A-Za-z0-9.-]+:\d{1,5}$/.test(value)) throw bad('a proxy is host:port, or empty to switch it off'); return [`Set proxy ${value || 'off'}`, 'proxy', value, value ? `proxy ${value}` : 'proxy off']; });
setting('dark-mode', 'Dark mode', (body) => { const on = flag(body); return [`Dark mode ${on ? 'on' : 'off'}`, 'dark_mode', on, on ? 'dark' : 'light']; });
setting('animations', 'Animations', (body) => { const on = flag(body); return [`Animations ${on ? 'on' : 'off'}`, 'animations', on, on ? 'on' : 'off']; });
setting('font-scale', 'Font scale', (body) => {
  if (typeof body.scale !== 'number' || body.scale < 0.5 || body.scale > 2) throw new ApiError(422, 'validation_error', 'scale: Input should be between 0.5 and 2');
  return [`Font scale ${body.scale}`, 'font_scale', body.scale, `font scale ${body.scale}`];
});

const RENDERER = { software: 'ANGLE (software rendering; demo)', intel: 'Mesa (demo integrated graphics)', nvidia: 'ANGLE (Vulkan relay, demo discrete graphics card)' };

route('GET', '/api/instances/{id}/info', ({ params }) => {
  const rec = booted(params.id);
  const r = resolved(rec);
  const props = rec.spec.device.props || {};
  const used = 2100 + Object.keys(rec.apps).length * 140 + (rec.files || []).reduce((sum, entry) => sum + entry.size / 1048576, 0);
  return {
    brand: props['ro.product.brand'] || '', manufacturer: props['ro.product.manufacturer'] || '', model: props['ro.product.model'] || '', device: props['ro.product.device'] || '',
    android: r.android_version, sdk: { 9: 28, 10: 29, 11: 30, 12: 31, 13: 33, 14: 34, 15: 35 }[r.android_version] || 33, renderer: RENDERER[rec.mode] || '',
    locale: rec.device.locale || db.config.device.locale, timezone: rec.device.timezone || 'UTC', android_id: rec.spec.device.android_id, serial: rec.spec.device.serial,
    width: r.width, height: r.height, density: r.dpi, uptime: round(now() - (rec.lastStart || now())),
    memory: { used_mb: Math.round(memoryOf(rec.spec.id)), limit_mb: r.memory_mb }, storage: { used_mb: Math.round(used), free_mb: Math.round(58000 - used) },
    battery: { level: 100, status: 'full' }, proxy: rec.device.proxy || '', dark_mode: Boolean(rec.device.dark_mode), animations: Boolean(rec.device.animations),
    font_scale: rec.device.font_scale || 1, foreground: device.foreground(rec),
  };
});

route('GET', '/api/instances/{id}/logcat', ({ params, query }) => {
  const rec = booted(params.id);
  const lines = whole(query.lines ?? 200, 1, 5000, 'lines');
  if (query.filter && query.filter.length > 200) throw bad('filter is longer than 200 characters');
  const front = device.foreground(rec).package;
  const base = Math.round(rec.lastStart || now());
  const stampOf = (offset) => new Date((base + offset) * 1000).toTimeString().slice(0, 8);
  const all = [
    `${stampOf(1)} I demo    : these log lines are invented by the demo`,
    `${stampOf(2)} I Zygote  : boot completed`,
    `${stampOf(3)} I ActivityManager: Start proc for example.launcher`,
    ...Object.keys(rec.apps).map((pkg, index) => `${stampOf(5 + index)} I PackageManager: ${pkg} ready`),
    `${stampOf(40)} I ActivityTaskManager: Displayed ${front}`,
    `${stampOf(41)} D InputDispatcher: focus on ${front}`,
  ];
  const kept = query.filter ? all.filter((line) => line.includes(query.filter)) : all;
  return { id: rec.spec.id, text: kept.slice(-lines).join('\n') + (kept.length ? '\n' : '') };
});

// -------------------------------------------------------------------- shell
// A handful of harmless commands with invented output. Nothing is executed anywhere.
const DESTRUCTIVE = /^(rm|dd|mkfs|reboot|wipe|format)$/;

function pretend(rec, argv, root) {
  const [name, ...args] = argv;
  const line = argv.join(' ');
  const props = { 'ro.product.model': rec.spec.device.props['ro.product.model'], 'ro.build.version.release': resolved(rec).android_version, 'ro.serialno': rec.spec.device.serial };
  if (name === 'id') return [root ? 'uid=0(root) gid=0(root) groups=0(root)' : 'uid=2000(shell) gid=2000(shell) groups=2000(shell)', 0];
  if (name === 'whoami') return [root ? 'root' : 'shell', 0];
  if (name === 'echo') return [args.join(' '), 0];
  if (name === 'date') return [new Date(now() * 1000).toString(), 0];
  if (name === 'uname') return ['Linux localhost 6.12.0-demo x86_64 (invented by the demo)', 0];
  if (name === 'getprop') return [args[0] ? (props[args[0]] || '') : Object.entries(props).map(([k, v]) => `[${k}]: [${v}]`).join('\n'), 0];
  if (line.startsWith('pm list packages')) return [installed(rec, args.includes('-3')).map((row) => `package:${row.package}`).join('\n'), 0];
  if (name === 'ls') return [(TREE[cleanPathOrNull(args[args.length - 1])] || []).map(([entry]) => entry).join('\n') || `ls: ${args[args.length - 1] || '.'}: No such file or directory`, 0];
  if (name === 'wm' && args[0] === 'size') return [`Physical size: ${resolved(rec).width}x${resolved(rec).height}`, 0];
  return [`/system/bin/sh: ${name}: not available in the demo. It knows: id, whoami, date, echo, uname, getprop, pm list packages, ls /sdcard, wm size`, 127];
}
const cleanPathOrNull = (path) => { try { return cleanPath(path); } catch { return ''; } };

registerEffect('shell', (id, taskHint, argv, root, _step, task) => {
  const rec = db.instances[id];
  if (!rec) return { ok: false, detail: `there is no Android ${id}` };
  const [output, code] = pretend(rec, argv, root);
  (db.shell[task.id] = db.shell[task.id] || {})[id] = { output, code, timed_out: false, truncated: false, seconds: round(0.05 + hash(`${task.id}${id}`) * 0.2, 2) };
  return { ok: code === 0, detail: code === 0 ? 'exit 0' : `exit ${code}` };
});

route('POST', '/api/shell', ({ body }) => {
  const who = selectorOf(body);
  if (!Array.isArray(body.argv) || !body.argv.length || body.argv.some((arg) => typeof arg !== 'string')) throw new ApiError(422, 'validation_error', 'argv: Field required');
  if (DESTRUCTIVE.test(body.argv[0]) && !body.force) throw conflict(`'${body.argv.join(' ').slice(0, 80)}' looks destructive and is refused by the denylist (control.shell_denylist); run it with force if you mean it`, 'refused');
  const task = fleet('shell', `shell: ${body.argv.join(' ').slice(0, 60)}`, who, (id) => ({ effect: ['shell', id, '', body.argv, Boolean(body.root)] }), { each: 0.2, first: 0.4 });
  // Keep the outputs of the latest few commands only.
  for (const key of Object.keys(db.shell).slice(0, -8)) delete db.shell[key];
  return accepted(task);
});

route('GET', '/api/shell/{task_id}/output', ({ params }) => {
  const task = findTask(params.task_id);
  if (!task || task.kind !== 'shell') throw notFound(`no shell task '${params.task_id.slice(0, 40)}'`);
  return { task: publicTask(task), outputs: db.shell[task.id] || {} };
});

