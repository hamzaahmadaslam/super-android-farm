// Every per-Android option in one form, used three ways:
//
//   create   "New Android": an empty field means "follow the farm default"
//   edit     one Android: fields show its own overrides; clearing one returns it to the
//            farm default
//   bulk     many Androids: only the fields ticked "change" are sent
//
// The result is always a nested patch in the shape of an InstanceSpec, which is what
// the API validates. Nothing here decides what is valid; the server does, and its
// message is shown.

import { get } from './api.js';
import { field, option } from './common.js';
import { h, uid } from './dom.js';

// What the pickers offer comes from the farm itself, never from a list in this file.
export async function loadOptions() {
  const [config, gpus, vpn, devices] = await Promise.all([
    get('/api/config'), get('/api/system/gpus'), get('/api/net/profiles'), get('/api/device-profiles'),
  ]);
  return {
    config: config.config,
    rendering: gpus.options || [],
    vpnProfiles: (vpn.profiles || []).map((p) => p.name),
    deviceProfiles: devices.profiles || {},
  };
}

const SCREENS = [
  ['480x854 @160 dpi (lightest)', 480, 854, 160],
  ['540x960 @200 dpi', 540, 960, 200],
  ['720x1280 @240 dpi', 720, 1280, 240],
  ['720x1600 @280 dpi (tall)', 720, 1600, 280],
  ['1080x1920 @420 dpi (heavy)', 1080, 1920, 420],
];

function getPath(object, path) {
  return path.split('.').reduce((at, key) => (at == null ? undefined : at[key]), object);
}

function setPath(object, path, value) {
  const keys = path.split('.');
  let at = object;
  for (const key of keys.slice(0, -1)) at = at[key] = at[key] || {};
  at[keys.at(-1)] = value;
}

const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

// An error shaped like the API's validation errors, so it is shown at the field.
function fieldError(path, message) {
  const error = new Error(`${path}: ${message}`);
  error.detail = [{ field: path, message }];
  return error;
}

// A map edited as a whole, sent the way the API merges it: only the keys that changed,
// null for each key that was removed, and null for the field itself when it was emptied
// (an empty map would merge to "no change" and remove nothing).
function mapPatch(before, after) {
  const was = before || {};
  const now = after || {};
  if (!Object.keys(now).length) return Object.keys(was).length ? null : undefined;
  const changes = {};
  for (const [key, value] of Object.entries(now)) if (was[key] !== value) changes[key] = value;
  for (const key of Object.keys(was)) if (!(key in now)) changes[key] = null;
  return Object.keys(changes).length ? changes : undefined;
}

// Controls. Each returns { el, read(), write(value) }; read() gives null for "empty".
function textControl(attrs = {}) {
  const el = h('input', { type: 'text', spellcheck: false, autocomplete: 'off', ...attrs });
  return { el, read: () => el.value.trim() || null, write: (v) => { el.value = v ?? ''; } };
}

function numberControl(attrs = {}) {
  const el = h('input', { type: 'number', ...attrs });
  return { el, read: () => (el.value === '' ? null : Number(el.value)), write: (v) => { el.value = v ?? ''; } };
}

function selectControl(choices, defaultLabel) {
  const el = h('select', null, option('', defaultLabel), choices.map(([value, label, attrs]) => option(value, label, attrs)));
  return { el, read: () => (el.value === '' ? null : el.value), write: (v) => { el.value = v ?? ''; } };
}

function triControl(defaultLabel) {
  const control = selectControl([['true', 'On'], ['false', 'Off']], defaultLabel);
  return { el: control.el, read: () => (control.el.value === '' ? null : control.el.value === 'true'), write: (v) => { control.el.value = v == null ? '' : String(v); } };
}

// A yes/no option without a farm default. Unticked reads as "empty", which each mode
// turns into the right thing (left out at creation, false when editing).
function checkControl() {
  const el = h('input', { type: 'checkbox' });
  return { el, read: () => (el.checked ? true : null), write: (v) => { el.checked = Boolean(v); } };
}

function linesControl(placeholder) {
  const el = h('textarea', { rows: 2, placeholder, spellcheck: false });
  return {
    el,
    read: () => { const lines = el.value.split('\n').map((l) => l.trim()).filter(Boolean); return lines.length ? lines : null; },
    write: (v) => { el.value = (v || []).join('\n'); },
  };
}

function propsControl() {
  const el = h('textarea', { rows: 2, placeholder: 'key=value, one per line', spellcheck: false });
  return {
    el,
    read: () => {
      const out = {};
      for (const line of el.value.split('\n').map((l) => l.trim()).filter(Boolean)) {
        const at = line.indexOf('=');
        if (at < 1) throw new Error(`"${line}" is not key=value`);
        out[line.slice(0, at).trim()] = line.slice(at + 1).trim();
      }
      return Object.keys(out).length ? out : null;
    },
    write: (v) => { el.value = Object.entries(v || {}).map(([k, val]) => `${k}=${val}`).join('\n'); },
  };
}

const tagsControl = () => {
  const control = textControl({ placeholder: 'eu, batch-2' });
  return {
    el: control.el,
    read: () => { const tags = control.el.value.split(',').map((t) => t.trim().toLowerCase()).filter(Boolean); return tags.length ? tags : null; },
    write: (v) => { control.el.value = (v || []).join(', '); },
  };
};

// The whole list of options. `modes` limits where one appears; `empty` is what an
// emptied field sends in edit/bulk mode when null is not accepted by the spec.
function fields(options, mode) {
  const cfg = options.config;
  const versions = Object.keys(cfg.android.images).sort((a, b) => Number(a) - Number(b));
  // A choice with a node is one exact card ("intel@/dev/dri/renderD128"); the reasons for
  // unavailable ones are long, so they are listed under the picker instead of inside it.
  const rendering = options.rendering.map((o) => [
    o.node ? `${o.value}@${o.node}` : o.value, o.available ? o.label : `${o.label} (unavailable)`, { disabled: !o.available },
  ]);
  const unavailable = options.rendering.filter((o) => !o.available);
  const gateways = Object.keys(cfg.network.gateways || {});
  const deviceProfiles = Object.entries(options.deviceProfiles).map(([name, p]) => [name, p.label || name]);
  const farm = (value) => `Farm default (${value})`;
  return [
    { group: 'Identity', items: [
      { path: 'name', label: 'Name', control: textControl({ maxLength: 60 }), modes: ['create', 'edit'], empty: '',
        help: mode === 'create' ? 'With a count above 1 each gets a number appended.' : '' },
      { path: 'tags', label: 'Tags', control: tagsControl(), empty: [], help: 'Comma-separated. Select by tag with tag:name.' },
      { path: 'notes', label: 'Notes', control: textControl({ maxLength: 2000 }), modes: ['edit'], empty: '' },
      { path: 'autostart', label: 'Start with the daemon', control: checkControl(), empty: false },
    ] },
    { group: 'Android', items: [
      { path: 'android_version', label: 'Android version', control: selectControl(versions.map((v) => [v, `Android ${v}`]), farm(cfg.android.default_version)),
        help: mode === 'create' ? 'A farm may mix versions freely.' : 'Changing the version of an Android that has data is refused unless forced: data written by a newer release does not boot on an older one.' },
      { path: 'device.profile', label: 'Device profile', topic: 'device-profile', control: selectControl([['random', 'Random preset'], ['none', 'None (the image’s own identity)'], ...deviceProfiles], farm(cfg.device.profile)),
        empty: cfg.device.profile, help: 'The phone model this Android reports to apps.' },
      { path: 'device.locale', label: 'Language', control: textControl({ placeholder: cfg.device.locale }), help: 'For example en-US.' },
      { path: 'device.timezone', label: 'Time zone', control: textControl({ placeholder: cfg.device.timezone || 'host’s zone' }), help: 'An Olson name, for example Europe/Berlin.' },
      { path: 'device.geo', label: 'Mock location', control: textControl({ placeholder: cfg.device.geo || 'lat,lon' }), help: 'lat,lon, for example 52.52,13.405.' },
      { path: 'camera', label: 'Virtual camera and microphone', control: triControl(farm(cfg.media.camera.enabled ? 'on' : 'off')) },
    ] },
    { group: 'Rendering and screen', items: [
      { path: 'gpu', label: 'Rendering', topic: 'rendering', apiPath: 'gpu_mode', control: selectControl(rendering, farm(cfg.gpu.mode)), virtual: true,
        help: ['CPU only, integrated or discrete GPU, a vendor, or one exact card. Takes effect at the next start.',
          unavailable.length ? h('details', null, h('summary', null, `Why ${unavailable.length} choice(s) are unavailable on this machine`),
            h('ul', null, unavailable.map((o) => h('li', null, h('strong', null, o.label), `: ${o.reason}`)))) : null] },
      { path: 'screen', label: 'Screen preset', control: selectControl(SCREENS.map(([label], i) => [String(i), label]), 'Custom / farm default'), virtual: true, modes: ['create', 'edit', 'bulk'] },
      { path: 'width', label: 'Width (px)', control: numberControl({ min: 240, max: 3840, placeholder: cfg.android.width }) },
      { path: 'height', label: 'Height (px)', control: numberControl({ min: 320, max: 3840, placeholder: cfg.android.height }) },
      { path: 'dpi', label: 'Density (dpi)', control: numberControl({ min: 80, max: 640, placeholder: cfg.android.dpi }) },
      { path: 'fps', label: 'Frames per second', topic: 'fps', control: numberControl({ min: 5, max: 120, placeholder: cfg.android.fps }) },
      { path: 'use_profile_screen', label: 'Use the device profile’s screen shape', control: checkControl(), modes: ['create'], help: 'Same aspect ratio as the phone, at the farm’s width.' },
    ] },
    { group: 'Resources', items: [
      { path: 'resources.cpus', label: 'CPU cores', control: numberControl({ min: 0.25, step: 0.25, placeholder: cfg.resources.cpus }) },
      { path: 'resources.memory_mb', label: 'Memory (MB)', control: numberControl({ min: 512, step: 64, placeholder: cfg.resources.memory_mb }) },
      { path: 'resources.memory_swap_mb', label: 'Memory + swap (MB)', control: numberControl({ min: 0, step: 64, placeholder: cfg.resources.memory_swap_mb }) },
      { path: 'resources.cpuset', label: 'Pin to cores', control: textControl({ placeholder: '0-3 or 8,9' }) },
    ] },
    { group: 'Network', items: [
      { path: 'network.mode', label: 'Network mode', topic: 'network', control: selectControl([['direct', 'Direct (host connection)'], ['vpn', 'VPN (one tunnel per Android)'], ['gateway', 'Shared VPN gateway'], ['proxy', 'HTTP proxy'], ['none', 'None (offline)']], farm(cfg.network.mode)) },
      { path: 'network.vpn_profile', label: 'VPN profile', control: selectControl(options.vpnProfiles.map((p) => [p, p]), `Chosen automatically (${cfg.network.vpn.selection})`),
        help: options.vpnProfiles.length ? 'Used when the mode is VPN.' : 'No profiles yet: import one on the Network page.' },
      { path: 'network.gateway', label: 'Gateway', control: selectControl(gateways.map((g) => [g, g]), cfg.network.gateway ? farm(cfg.network.gateway) : 'Farm default'),
        help: gateways.length ? 'Used when the mode is shared gateway.' : 'No gateways yet: define one on the Network page.' },
      { path: 'network.proxy', label: 'Proxy', control: textControl({ placeholder: cfg.network.proxy || 'host:port' }), help: 'Used when the mode is HTTP proxy.' },
      { path: 'network.killswitch', label: 'Kill switch', topic: 'killswitch', control: triControl(farm(cfg.network.killswitch ? 'on' : 'off')), help: 'Blocks traffic that would leave outside the tunnel.' },
    ] },
    { group: 'Advanced', items: [
      { path: 'data_volume', label: 'Adopt an existing data volume', control: textControl({ placeholder: 'Docker volume name' }), modes: ['create'], help: 'Use an Android created elsewhere. Count must be 1; its identity is kept.' },
      { path: 'image', label: 'Custom image', control: textControl({ placeholder: 'repository:tag' }), modes: ['create', 'edit'], help: 'Run this exact image instead of the one configured for the version.' },
      { path: 'extra_boot_args', label: 'Extra boot arguments', control: linesControl('androidboot.key=value, one per line'), empty: [] },
      { path: 'extra_props', label: 'Extra properties', control: propsControl(), empty: {}, merge: true, modes: ['create', 'edit'],
        help: 'System properties set on this Android after boot. Deleting a line here removes that property again.' },
    ] },
  ];
}

// Returns { el, value() }. value() is the patch (create: the template) and throws a
// readable Error when a field cannot be parsed.
export function instanceForm(options, { mode, spec, groups: only }) {
  const groups = fields(options, mode).filter(({ group }) => !only || only.includes(group));
  const active = [];
  const byPath = {};
  const el = h('div', { class: 'stack' });
  for (const { group, items } of groups) {
    const shown = items.filter((item) => !item.modes || item.modes.includes(mode));
    if (!shown.length) continue;
    const grid = h('div', { class: 'fields' });
    for (const item of shown) {
      byPath[item.path] = item;
      const wide = item.control.el.tagName === 'TEXTAREA';
      let apply = null;
      if (mode === 'bulk' && !item.virtual) {
        const id = uid('ap');
        apply = h('input', { type: 'checkbox', id });
        item.control.el.addEventListener('input', () => { apply.checked = true; });
        item.apply = apply;
      }
      if (mode === 'edit' && spec && !item.virtual) {
        item.initial = getPath(spec, item.path);
        item.control.write(item.initial);
      }
      const node = field(item.label, item.control.el, item.help, { path: item.apiPath || item.path, topic: item.topic });
      if (wide) node.classList.add('wide');
      if (apply) node.append(h('label', { class: 'small muted', for: apply.id }, apply, ' change this'));
      grid.append(node);
      active.push(item);
    }
    const wrap = group === 'Advanced' ? h('details', null, h('summary', null, 'Advanced'), grid)
      : h('fieldset', null, h('legend', null, group), grid);
    el.append(wrap);
  }

  // The two pickers that are shorthand for other fields.
  const gpu = byPath.gpu;
  let gpuTouched = false;
  if (gpu) {
    if (mode === 'edit' && spec && spec.gpu_mode) gpu.control.write(spec.gpu_node ? `${spec.gpu_mode}@${spec.gpu_node}` : spec.gpu_mode);
    gpu.control.el.addEventListener('change', () => { gpuTouched = true; });
  }
  const screen = byPath.screen;
  if (screen) {
    screen.control.el.addEventListener('change', () => {
      const preset = SCREENS[Number(screen.control.el.value)];
      if (!preset || screen.control.el.value === '') return;
      ['width', 'height', 'dpi'].forEach((key, i) => {
        byPath[key].control.write(preset[i + 1]);
        if (byPath[key].apply) byPath[key].apply.checked = true;
      });
    });
  }

  function value() {
    const patch = {};
    for (const item of active) {
      // Letters in a number box: the browser keeps them from us, so say so at the field.
      if (item.control.el.validity && item.control.el.validity.badInput) throw fieldError(item.apiPath || item.path, 'This is not a number.');
      if (item.virtual) continue;
      const read = item.control.read();
      const cleared = read == null ? (item.empty !== undefined ? item.empty : null) : read;
      if (mode === 'create') {
        if (read != null) setPath(patch, item.path, read);
      } else if (mode === 'bulk') {
        if (item.apply.checked) setPath(patch, item.path, cleared);
      } else if (item.merge) {
        const changes = mapPatch(item.initial, read);
        if (changes !== undefined) setPath(patch, item.path, changes);
      } else if (!same(cleared, item.initial ?? (item.empty !== undefined ? item.empty : null))) {
        setPath(patch, item.path, cleared);
      }
    }
    // "nvidia@/dev/dri/renderD129" is one exact card; a bare mode leaves the card to the farm.
    const picked = gpu ? gpu.control.read() : null;
    if (gpu && (mode === 'create' ? picked != null : gpuTouched)) {
      const [gpuMode, node] = (picked || '').split('@');
      patch.gpu_mode = gpuMode || null;
      if (node || mode !== 'create') patch.gpu_node = node || null;
    }
    return patch;
  }

  return { el, value };
}
