// One Android: live view with control on the left, everything about it in tabs.

import { del, get, patch, post, query, upload } from '../api.js';
import { badge, button, card, deviceActions, empty, field, fleetAction, h264Offered, kv, option, pageHead, splitCommand } from '../common.js';
import { confirmDialog } from '../components/dialog.js';
import { liveView } from '../components/live.js';
import { dataTable } from '../components/table.js';
import { tabs } from '../components/tabs.js';
import { finished } from '../components/tasks.js';
import { attempt, ok } from '../components/toast.js';
import { bytes, duration, h, replace, when } from '../dom.js';
import { instanceForm, loadOptions } from '../instance-form.js';
import { go } from '../router.js';

const RUNNING = new Set(['running', 'booted', 'starting']);

// Input from a live view to a set of devices. Field names follow POST /api/input/*.
export function sendInput(ids) {
  return (kind, payload) => post(`/api/input/${kind}`, { ids: ids(), ...payload }).catch(() => {});
}

function overview(id, state) {
  return async (panel) => {
    const s = state();
    panel.append(h('div', { class: 'stack' },
      s.last_error && h('div', { class: 'banner fail', role: 'alert' }, h('div', { class: 'grow' }, h('strong', null, 'Last start failed. '), s.last_error)),
      (s.warnings || []).map((line) => h('div', { class: 'banner warn' }, line)),
      card('Status', kv({
        State: badge(s.state), 'Android version': s.android_version, Image: s.image,
        Rendering: s.gpu ? `${s.gpu.mode || 'decided at the next start'} (asked for: ${s.gpu.requested})${s.gpu.node ? ` on ${s.gpu.node}` : ''}` : '',
        Screen: s.screen ? `${s.screen.width}×${s.screen.height}, ${s.screen.dpi} dpi, ${s.screen.fps} fps` : '',
        Network: s.net ? [s.net.mode, s.net.profile, s.net.gateway, s.net.proxy].filter(Boolean).join(' · ') : '',
        'ADB': s.serial, 'Data volume': `${s.data_volume}${s.adopted ? ' (adopted)' : ''}`, 'Device profile': s.device_profile,
        Uptime: s.uptime != null ? duration(s.uptime) : '', 'Last start': when(s.last_start),
        Containers: `${s.containers.android.name}: ${s.containers.android.state} · ${s.containers.net.name}: ${s.containers.net.state}`,
        Notes: s.notes,
      }))));
    if (s.state !== 'booted') {
      panel.append(h('p', { class: 'muted' }, 'Start this Android to see its device details and change live settings.'));
      return;
    }
    const info = await get(`/api/instances/${id}/info`);
    panel.firstChild.append(
      card('Device', kv({
        Model: [info.brand, info.model].filter(Boolean).join(' '), 'Android release': info.android, 'API level': info.sdk, Renderer: info.renderer,
        Language: info.locale, 'Time zone': info.timezone, 'android_id': info.android_id,
        Screen: info.width ? `${info.width}×${info.height}, density ${info.density}` : '',
        Memory: info.memory ? `${info.memory.used_mb} MB used${info.memory.limit_mb ? ` of ${info.memory.limit_mb} MB` : ''}` : '',
        Storage: info.storage ? `${info.storage.used_mb} MB used, ${info.storage.free_mb} MB free` : '',
        'In front': info.foreground ? info.foreground.package : '', Proxy: info.proxy,
        'Dark mode': info.dark_mode, Animations: info.animations, 'Font scale': info.font_scale,
      })),
      card('Change a live setting', deviceActions(() => String(id)),
        h('p', { class: 'small muted' }, 'Applied to the running Android now. Settings under the Settings tab are the saved ones and apply at the next start.')));
  };
}

function apps(id, state) {
  return async (panel) => {
    if (state().state !== 'booted') {
      panel.append(empty('This Android is not running', 'Start it to list, install and remove apps.'));
      return;
    }
    const system = h('input', { type: 'checkbox' });
    const pick = h('select', { 'aria-label': 'App from the library' });
    const act = (action, pkg, extra = {}) => attempt(async () => {
      const data = await post(`/api/apps/${action}`, { selector: String(id), package: pkg, ...extra });
      await finished(data.task);
      await load();
    });
    const table = dataTable({
      label: 'Installed apps',
      columns: [
        { title: 'App', cell: (a) => [a.label ? h('div', null, a.label) : null, h('code', null, a.package)] },
        { title: 'Version', cell: (a) => a.version_name || a.version || a.version_code },
        { title: 'Enabled', cell: (a) => (a.enabled ? 'yes' : 'no') },
        { title: '', class: 'actions', cell: (a) => [
          button('Launch', () => act('launch', a.package), 'small'), ' ',
          button('Stop', () => act('stop', a.package), 'small'), ' ',
          button('Clear data', async () => {
            if (await confirmDialog({ title: 'Clear app data?', message: `${a.package} on Android ${id} is reset to a fresh install.`, confirm: 'Clear data', danger: true })) act('clear', a.package);
          }, 'small'), ' ',
          button('Uninstall', async () => {
            if (await confirmDialog({ title: 'Uninstall app?', message: `${a.package} and its data are removed from Android ${id}.`, confirm: 'Uninstall', danger: true })) act('uninstall', a.package);
          }, 'small danger'),
        ] },
      ],
      key: (a) => a.package,
      empty: empty('No apps installed', 'Pick an app from the library above, or upload one on the Apps page.'),
    });
    async function load() {
      const data = await get(`/api/instances/${id}/apps`, { third_party: !system.checked });
      table.update(data.apps);
    }
    system.addEventListener('change', () => load().catch(() => {}));
    const library = (await get('/api/apps/library')).apps;
    replace(pick, library.map((app) => option(app.id, `${app.label} ${app.version_name} (${app.package})`)));
    panel.append(h('div', { class: 'toolbar' },
      library.length ? [pick, button('Install or update', () => attempt(async () => {
        const data = await post('/api/apps/install', { selector: String(id), app_id: pick.value });
        await finished(data.task);
        await load();
      }), 'primary')] : h('span', { class: 'muted' }, 'The app library is empty. ', h('a', { href: '#/apps' }, 'Upload an app')),
      h('label', null, system, ' Show system apps')), table.el);
    await load();
  };
}

function files(id, state) {
  return async (panel) => {
    if (state().state !== 'booted') {
      panel.append(empty('This Android is not running', 'Start it to browse and transfer files.'));
      return;
    }
    const path = h('input', { type: 'text', value: '/sdcard', 'aria-label': 'Folder on the device', spellcheck: false });
    const file = h('input', { type: 'file', 'aria-label': 'File to send' });
    const table = dataTable({
      label: 'Files',
      key: (e) => e.path,
      columns: [
        { title: 'Name', cell: (e) => (e.type === 'dir'
          ? h('button', { type: 'button', class: 'btn small ghost', dataset: { focus: e.path }, onclick: () => open(e.path) }, `${e.name}/`)
          : h('a', { href: `/api/instances/${id}/files/pull${query({ path: e.path })}`, download: e.name }, e.name)) },
        { title: 'Size', class: 'num', cell: (e) => (e.type === 'dir' ? '' : bytes(e.size)) },
        { title: 'Changed', cell: (e) => when(e.modified) },
      ],
      empty: empty('This folder is empty', 'Send a file here with the form above.'),
    });
    async function open(folder) {
      const data = await attempt(() => get(`/api/instances/${id}/files`, { path: folder }));
      if (!data) return;
      path.value = data.path;
      table.update(data.entries);
    }
    const send = (route, fields) => attempt(async () => {
      if (!file.files.length) throw new Error('Choose a file first.');
      const data = await upload(route, file.files[0], { selector: String(id), ...fields });
      await finished(data.task);
      await open(path.value);
    });
    panel.append(
      h('form', { class: 'toolbar', onsubmit: (event) => { event.preventDefault(); open(path.value); } },
        path, h('button', { type: 'submit', class: 'btn' }, 'Open'),
        button('Up', () => open(path.value.replace(/\/[^/]+\/?$/, '') || '/sdcard'))),
      h('div', { class: 'toolbar' }, file,
        button('Send to this folder', () => send('/api/files/push', { remote: `${path.value.replace(/\/$/, '')}/` })),
        button('Add to the gallery', () => send('/api/files/gallery', {}))),
      table.el);
    await open('/sdcard');
  };
}

function network(id) {
  return async (panel) => {
    const box = h('div', { class: 'stack' });
    panel.append(box);
    const load = async (refresh) => {
      const net = await get(`/api/instances/${id}/net`, refresh ? { refresh: true } : null);
      const ks = net.killswitch || {};
      replace(box,
        card('Connection', kv({
          Mode: net.mode, State: badge(net.state), Profile: net.profile, Gateway: net.gateway, Provider: net.provider,
          'Public address': net.public_ip || 'not checked yet', 'Tunnel address': net.tunnel_ip,
          'Connected since': when(net.since), Reconnects: net.restarts || '', Detail: net.detail, 'How to fix': net.fix,
        }), h('div', { class: 'row' }, button('Check the public address', () => attempt(() => load(true))))),
        card('Kill switch', kv({
          Wanted: ks.enabled, Active: ks.active == null ? '' : badge(ks.active ? 'ok' : 'fail', ks.active ? 'active' : 'not active'),
          Problems: (ks.problems || []).join('; '),
        }), h('p', { class: 'small muted' }, 'With the kill switch active nothing leaves this Android outside its tunnel, also while the tunnel is down.')));
    };
    await load(false);
  };
}

function settings(id, state, reload) {
  return async (panel) => {
    const options = await loadOptions();
    const form = instanceForm(options, { mode: 'edit', spec: state().spec });
    const force = h('input', { type: 'checkbox' });
    panel.append(h('form', {
      class: 'stack',
      onsubmit: (event) => {
        event.preventDefault();
        attempt(async () => {
          const changes = form.value();
          if (!Object.keys(changes).length) throw new Error('Nothing was changed.');
          const data = await patch(`/api/instances/${id}`, { patch: changes, force: force.checked });
          ok(data.note || 'Saved.');
          await reload();
        });
      },
    },
    h('p', { class: 'muted' }, 'These are this Android’s own settings. An empty field follows the farm default (shown as the placeholder). Changes apply at the next start.'),
    form.el,
    field('Force a change that can lose data (another Android version on existing data; a backup is taken first)', force),
    h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn primary' }, 'Save'),
      RUNNING.has(state().state) ? button('Save does not restart: restart now', () => fleetAction('restart', [id]).then(reload)) : null)));
  };
}

function logs(id) {
  return async (panel) => {
    const source = h('select', { 'aria-label': 'Log source' }, option('android', 'Android container'), option('net', 'Network container'), option('logcat', 'Android logcat'));
    const lines = h('input', { type: 'number', min: 10, max: 5000, value: 200, 'aria-label': 'Lines' });
    const filter = h('input', { type: 'text', placeholder: 'only lines containing… (logcat)', 'aria-label': 'Filter' });
    const out = h('pre', { class: 'log', tabindex: '0', 'aria-label': 'Log output' });
    const load = () => attempt(async () => {
      let content;
      if (source.value === 'logcat') content = (await get(`/api/instances/${id}/logcat`, { lines: lines.value, filter: filter.value })).text;
      else content = (await get(`/api/instances/${id}/logs`, { tail: lines.value }))[source.value];
      out.textContent = content || (source.value === 'android'
        ? '(this container wrote nothing to its own log; what Android logs is under "Android logcat")' : '(nothing logged)');
      out.scrollTop = out.scrollHeight;
    });
    panel.append(h('form', { class: 'toolbar', onsubmit: (event) => { event.preventDefault(); load(); } },
      source, lines, filter, h('button', { type: 'submit', class: 'btn' }, 'Refresh')), out);
    source.addEventListener('change', load);
    await load();
  };
}

function shell(id) {
  return (panel) => {
    const command = h('input', { type: 'text', class: 'grow mono', placeholder: 'pm list packages -3', 'aria-label': 'Command', spellcheck: false, autocomplete: 'off' });
    const root = h('input', { type: 'checkbox' });
    const out = h('pre', { class: 'log', tabindex: '0', 'aria-label': 'Command output' });
    const run = (force) => attempt(async () => {
      const argv = splitCommand(command.value);
      if (!argv.length) throw new Error('Type a command.');
      let data;
      try {
        data = await post('/api/shell', { selector: String(id), argv, root: root.checked, force });
      } catch (error) {
        // The denylist refused it: the reason is shown, and the operator may insist.
        if (error.code !== 'refused' || force) throw error;
        if (await confirmDialog({ title: 'Run a destructive command?', message: error.message, confirm: 'Run anyway', danger: true })) run(true);
        return;
      }
      out.textContent = 'running…';
      await finished(data.task);
      const answer = await get(`/api/shell/${data.task.id}/output`);
      const result = answer.outputs[String(id)];
      const outcome = (answer.task.results || {})[String(id)];
      out.textContent = result
        ? `${result.output || '(no output)'}\n\n[exit ${result.code ?? 'killed'}${result.timed_out ? ', timed out' : ''}${result.truncated ? ', output cut' : ''}, ${result.seconds}s]`
        : `The command did not run: ${outcome ? outcome.detail : 'no result was recorded'}`;
    });
    panel.append(
      h('form', { class: 'toolbar', onsubmit: (event) => { event.preventDefault(); run(false); } },
        command, h('label', null, root, ' as root'), h('button', { type: 'submit', class: 'btn primary' }, 'Run')),
      h('p', { class: 'small muted' }, 'Runs inside this Android. The command is passed as separate arguments; nothing goes through a shell on the host.'),
      out);
  };
}

export async function render(root, params, scope) {
  const id = Number(params[0]);
  let state = await get(`/api/instances/${id}`);
  const system = await get('/api/system').catch(() => ({}));
  const head = h('div');
  const left = h('div', { class: 'stack' });
  let liveFor = null;

  function draw() {
    const running = RUNNING.has(state.state);
    const act = (action) => fleetAction(action, [id]).then(reload);
    replace(head, pageHead(state.name, `Android ${id}`,
      badge(state.state),
      running ? button('Stop', () => act('stop')) : button('Start', () => act('start'), 'primary'),
      button('Restart', () => act('restart')),
      button('Recreate', () => act('recreate')),
      button('Clone', async () => {
        const withData = h('input', { type: 'checkbox' });
        const yes = await confirmDialog({
          title: `Clone Android ${id}`, message: 'The copy gets the same settings and a new device identity.', confirm: 'Clone',
          details: field('Copy the data too (this Android must be stopped)', withData),
        });
        if (!yes) return;
        const data = await attempt(() => post(`/api/instances/${id}/clone`, { with_data: withData.checked }));
        if (data) { ok(`Created Android ${data.instance.id}.`); go(`android/${data.instance.id}`); }
      }),
      button('Delete', async () => {
        const keep = h('input', { type: 'checkbox' });
        const yes = await confirmDialog({
          title: `Delete ${state.name}?`, confirm: 'Delete', danger: true,
          message: 'Its containers and settings are removed, and its data unless you keep it. With backup.auto_before_risky on (the default) a backup is taken first.',
          details: field('Keep the data volume', keep),
        });
        if (!yes) return;
        if (await attempt(() => del(`/api/instances/${id}${query({ keep_data: keep.checked || null })}`), `Android ${id} deleted.`)) go('androids');
      }, 'danger')));
    // The live view is rebuilt only when the Android goes up or down, never on a refresh.
    if (liveFor !== running) {
      liveFor = running;
      replace(left, running
        ? liveView(id, scope, { send: sendInput(() => [id]), h264: h264Offered(system), label: state.name, aspect: state.screen && [state.screen.width, state.screen.height] })
        : empty('Not running', 'Start this Android to see and control its screen.', button('Start', () => act('start'), 'primary')));
    }
  }

  async function reload() {
    state = await get(`/api/instances/${id}`);
    draw();
  }

  const getState = () => state;
  const view = tabs([
    { id: 'overview', label: 'Overview', render: overview(id, getState) },
    { id: 'apps', label: 'Apps', render: apps(id, getState) },
    { id: 'files', label: 'Files', render: files(id, getState) },
    { id: 'network', label: 'Network', render: network(id) },
    { id: 'settings', label: 'Settings', render: settings(id, getState, reload) },
    { id: 'logs', label: 'Logs', render: logs(id) },
    { id: 'shell', label: 'Shell', render: shell(id) },
  ], { active: params[1] || 'overview', parent: scope, onChange: (tab) => history.replaceState(null, '', `#/android/${id}/${tab}`) });

  root.append(head, h('div', { class: 'split' }, left, view.el));
  draw();
  const timer = setInterval(() => { if (!document.hidden) reload().catch(() => {}); }, 5000);
  scope.add(() => clearInterval(timer));
}
