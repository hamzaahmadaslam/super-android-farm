// Network: VPN profiles, credentials (write-only), shared gateways, and what every
// Android's connection is doing right now.

import { del, get, post, put, upload } from '../api.js';
import { badge, button, card, empty, field, loadInstances, option, pageHead } from '../common.js';
import { confirmDialog, openDialog } from '../components/dialog.js';
import { dataTable } from '../components/table.js';
import { attempt, ok } from '../components/toast.js';
import { bytes, h, replace } from '../dom.js';

const RUNNING = new Set(['running', 'booted']);

export async function render(root, _params, scope) {
  let profiles = [];
  let credentials = [];
  const live = new Map();      // instance id -> net status, filled on demand

  // ------------------------------------------------------------- profiles
  const profileFile = h('input', { type: 'file', accept: '.ovpn,.conf', multiple: true, 'aria-label': 'VPN profile files' });
  const profileTable = dataTable({
    label: 'VPN profiles',
    key: (p) => p.name,
    columns: [
      { title: 'Profile', cell: (p) => h('code', null, p.name) },
      { title: 'Type', cell: (p) => p.provider },
      { title: 'Servers', cell: (p) => (p.servers || []).slice(0, 3).join(', ') + ((p.servers || []).length > 3 ? ` +${p.servers.length - 3}` : '') },
      { title: 'Status', cell: (p) => (p.valid
        ? [badge('ok', 'valid'), p.needs_credentials ? ' needs credentials' : '', (p.removed || []).length ? ` · removed for safety: ${p.removed.join(', ')}` : '']
        : [badge('fail', 'invalid'), ' ', p.error]) },
      { title: 'Size', class: 'num', cell: (p) => bytes(p.size) },
      { title: '', class: 'actions', cell: (p) => button('Delete', async () => {
        if (!await confirmDialog({ title: 'Delete this profile?', message: `${p.name} is removed. Androids pinned to it will fail to start until they get another.`, confirm: 'Delete', danger: true })) return;
        await attempt(() => del(`/api/net/profiles/${encodeURIComponent(p.name)}`), 'Profile deleted.');
        await loadProfiles();
      }, 'small danger') },
    ],
    empty: empty('No VPN profiles', 'Upload the .ovpn (OpenVPN) or .conf (WireGuard) files from your provider. Directives that run scripts are stripped on import.'),
  });
  async function loadProfiles() {
    profiles = (await get('/api/net/profiles')).profiles;
    profileTable.update(profiles);
  }

  // ---------------------------------------------------------- credentials
  const credName = h('input', { type: 'text', value: 'default', spellcheck: false, autocomplete: 'off' });
  const credUser = h('input', { type: 'text', autocomplete: 'off', spellcheck: false });
  const credPass = h('input', { type: 'password', autocomplete: 'new-password' });
  const credList = h('div', { class: 'row' });
  async function loadCredentials() {
    credentials = (await get('/api/net/credentials')).credentials;
    replace(credList, credentials.length ? credentials.map((c) => h('span', { class: 'pill' }, c.name, ' ',
      h('button', { type: 'button', class: 'btn small ghost', 'aria-label': `Delete credentials ${c.name}`, onclick: async () => {
        if (!await confirmDialog({ title: 'Delete these credentials?', message: `"${c.name}" is removed from the farm's secrets.`, confirm: 'Delete', danger: true })) return;
        await attempt(() => del(`/api/net/credentials/${encodeURIComponent(c.name)}`), 'Credentials deleted.');
        await loadCredentials();
      } }, '×'))) : h('span', { class: 'muted' }, 'None stored yet.'));
  }

  // ------------------------------------------------------------- gateways
  const gatewayTable = dataTable({
    label: 'Shared VPN gateways',
    key: (g) => g.name,
    columns: [
      { title: 'Gateway', cell: (g) => h('code', null, g.name) },
      { title: 'State', cell: (g) => [badge(g.state), g.detail ? h('div', { class: 'small muted' }, g.detail) : null] },
      { title: 'Profile', cell: (g) => (g.config || {}).profile },
      { title: 'Subnet', cell: (g) => g.subnet || (g.config || {}).subnet || 'assigned when it starts' },
      { title: 'Androids', cell: (g) => (g.members || []).length },
      { title: '', class: 'actions', cell: (g) => [
        button('Up', () => attempt(() => post(`/api/net/gateways/${encodeURIComponent(g.name)}/up`), 'Gateway is starting.').then(loadGateways), 'small'), ' ',
        button('Down', () => attempt(() => post(`/api/net/gateways/${encodeURIComponent(g.name)}/down`), 'Gateway stopped.').then(loadGateways), 'small'), ' ',
        button('Delete', async () => {
          if (!await confirmDialog({ title: 'Delete this gateway?', message: `${g.name} and its network are removed. It must not be in use by any Android.`, confirm: 'Delete', danger: true })) return;
          await attempt(() => del(`/api/net/gateways/${encodeURIComponent(g.name)}`), 'Gateway deleted.');
          await loadGateways();
        }, 'small danger'),
      ] },
    ],
    empty: empty('No shared gateways', 'A gateway is one VPN connection that several Androids share. Add one, then set an Android’s network mode to "Shared VPN gateway".'),
  });
  async function loadGateways() {
    gatewayTable.update((await get('/api/net/gateways')).gateways);
  }
  async function addGateway() {
    const name = h('input', { type: 'text', spellcheck: false, placeholder: 'eu1' });
    const profile = h('select', null, profiles.map((p) => option(p.name)));
    const provider = h('select', null, option('openvpn', 'OpenVPN'), option('wireguard', 'WireGuard'));
    const creds = h('select', null, option('default'), credentials.filter((c) => c.name !== 'default').map((c) => option(c.name)));
    const subnet = h('input', { type: 'text', placeholder: 'chosen automatically', spellcheck: false });
    if (!profiles.length) { ok('Upload a VPN profile first: a gateway connects with one.'); return; }
    await openDialog({
      title: 'Add a shared gateway',
      body: h('div', { class: 'fields' }, field('Name', name, 'Short, lowercase.'), field('VPN profile', profile), field('VPN software', provider),
        field('Credentials', creds), field('Subnet (/24)', subnet, 'Leave empty to take one from docker.subnet_pool.')),
      actions: [{ label: 'Save', kind: 'primary', run: async () => {
        await put(`/api/net/gateways/${encodeURIComponent(name.value.trim())}`, { provider: provider.value, profile: profile.value, credentials: creds.value, subnet: subnet.value.trim() });
        ok('Gateway saved. "Up" starts it; so does starting an Android that uses it.');
        await loadGateways();
      } }],
    });
  }

  // ---------------------------------------------------------- per Android
  const statusTable = dataTable({
    label: 'Network status per Android',
    columns: [
      { title: 'Android', cell: (i) => h('a', { href: `#/android/${i.id}/network` }, `${i.id} · ${i.name}`) },
      { title: 'Mode', cell: (i) => (i.net || {}).mode },
      { title: 'Profile / gateway', cell: (i) => (live.get(i.id) || {}).profile || (i.net || {}).profile || (i.net || {}).gateway || (i.net || {}).proxy || '' },
      { title: 'Tunnel', cell: (i) => { const s = live.get(i.id); return s ? [badge(s.state), s.detail ? h('div', { class: 'small muted' }, s.detail) : null] : (RUNNING.has(i.state) ? '' : h('span', { class: 'muted' }, 'not running')); } },
      { title: 'Public address', cell: (i) => (live.get(i.id) || {}).public_ip || '' },
      { title: 'Kill switch', cell: (i) => {
        const s = live.get(i.id);
        if (!s || !s.killswitch) return (i.net || {}).killswitch ? 'wanted' : 'off';
        return s.killswitch.active ? badge('ok', 'active') : badge(s.killswitch.enabled ? 'fail' : '', s.killswitch.enabled ? 'NOT active' : 'off');
      } },
    ],
    empty: empty('No Androids yet', 'Create one on the Androids page.'),
  });
  let instances = [];
  async function loadStatus(refreshAddress) {
    instances = await loadInstances();
    statusTable.update(instances);
    // One request per running Android, a few at a time: each asks its sidecar container.
    const queue = instances.filter((i) => RUNNING.has(i.state)).map((i) => i.id);
    const worker = async () => {
      for (let id = queue.shift(); id !== undefined; id = queue.shift()) {
        try {
          live.set(id, await get(`/api/instances/${id}/net`, refreshAddress ? { refresh: true } : null));
        } catch (error) {
          live.set(id, { state: 'unknown', detail: error.message });
        }
        statusTable.update(instances);
      }
    };
    await Promise.all([worker(), worker(), worker(), worker()]);
  }

  root.append(
    pageHead('Network', 'How Androids reach the internet: directly, through their own VPN tunnel, or through a shared gateway.'),
    h('div', { class: 'stack' },
      h('section', { class: 'card' },
        h('div', { class: 'card-head' }, h('h2', null, 'Status per Android'),
          button('Refresh', () => attempt(() => loadStatus(false)), 'small'),
          button('Check public addresses', () => attempt(() => loadStatus(true), 'Addresses checked.'), 'small')),
        statusTable.el),
      card('VPN profiles',
        h('form', { class: 'toolbar', onsubmit: (event) => {
          event.preventDefault();
          attempt(async () => {
            if (!profileFile.files.length) throw new Error('Choose one or more profile files first.');
            for (const one of profileFile.files) await upload('/api/net/profiles', one);
            profileFile.value = '';
            await loadProfiles();
          }, 'Profiles imported.');
        } }, profileFile, h('button', { type: 'submit', class: 'btn primary' }, 'Import')),
        profileTable.el),
      card('VPN credentials',
        h('p', { class: 'small muted' }, 'Username and password for profiles that ask for them. They are stored in the farm’s secrets and can be replaced or deleted, never read back.'),
        credList,
        h('form', { class: 'fields', autocomplete: 'off', onsubmit: (event) => {
          event.preventDefault();
          attempt(async () => {
            await put(`/api/net/credentials/${encodeURIComponent(credName.value.trim())}`, { username: credUser.value, password: credPass.value });
            credUser.value = '';
            credPass.value = '';
            await loadCredentials();
          }, 'Credentials stored.');
        } },
        field('Name', credName, '"default" is used unless a gateway or the config names another.'),
        field('Username', credUser), field('Password', credPass),
        h('div', { class: 'field' }, h('span', null, '\u00a0'), h('button', { type: 'submit', class: 'btn' }, 'Store')))),
      h('section', { class: 'card' },
        h('div', { class: 'card-head' }, h('h2', null, 'Shared gateways'), button('Add gateway', addGateway, 'small primary')),
        gatewayTable.el)));

  await Promise.all([loadProfiles(), loadCredentials(), loadGateways()]);
  loadStatus(false).catch(() => {});
  scope.every(15000, () => loadGateways()).catch(() => {});
}
