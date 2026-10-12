// Networking: VPN profiles, credentials (names only), shared gateways.
//
// A profile "upload" keeps the file's name and size and nothing else: the mock never
// opens the file, so no key or address in it is read, stored or sent. Credentials are
// thrown away on arrival; only the name they were stored under is remembered.

import { created, route } from '../backend.js';
import { ApiError, bad, notFound, resolved, UP } from '../farm.js';
import { db, now } from '../store.js';

const NAME = /^[A-Za-z0-9_.-]{1,64}$/;
const GATEWAY = /^[a-z0-9][a-z0-9-]{0,30}$/;

route('GET', '/api/net/profiles', () => ({ profiles: db.profiles }));

route('POST', '/api/net/profiles', ({ file }) => {
  if (!file) throw bad('no file in the request');
  const name = file.name.replace(/[^A-Za-z0-9_.-]/g, '_').slice(0, 64);
  const provider = name.endsWith('.conf') ? 'wireguard' : name.endsWith('.ovpn') ? 'openvpn' : '';
  if (!provider) throw bad('a VPN profile is an .ovpn (OpenVPN) or .conf (WireGuard) file');
  // Invented servers in the documentation range; the file's own content is not looked at.
  const last = 20 + (db.profiles.length * 17) % 200;
  db.profiles = db.profiles.filter((p) => p.name !== name).concat({
    name, provider, size: file.size, modified: now(), valid: true, error: '', servers: [`198.51.100.${last}:${provider === 'wireguard' ? 51820 : 1194}/udp`],
    removed: [], needs_credentials: provider === 'openvpn',
  }).sort((a, b) => a.name.localeCompare(b.name));
  return created({ name });
});

route('DELETE', '/api/net/profiles/{name}', ({ params }) => {
  if (!db.profiles.some((p) => p.name === params.name)) throw notFound(`no VPN profile named '${params.name.slice(0, 64)}'`);
  db.profiles = db.profiles.filter((p) => p.name !== params.name);
  return { deleted: params.name };
});

route('GET', '/api/net/credentials', () => ({ credentials: db.credentials.map((name) => ({ name })) }));

route('PUT', '/api/net/credentials/{name}', ({ params, body }) => {
  if (!NAME.test(params.name)) throw bad('bad credentials name: use letters, digits, _ . -');
  for (const field of ['username', 'password']) {
    if (typeof body[field] !== 'string' || !body[field]) throw new ApiError(422, 'validation_error', `${field}: Field required`, { detail: [{ field, message: 'Field required' }] });
  }
  if (!db.credentials.includes(params.name)) db.credentials = [...db.credentials, params.name].sort();
  return { name: params.name, stored: true };
});

route('DELETE', '/api/net/credentials/{name}', ({ params }) => {
  if (!db.credentials.includes(params.name)) throw notFound(`no credentials named '${params.name.slice(0, 64)}'`);
  db.credentials = db.credentials.filter((name) => name !== params.name);
  return { deleted: params.name };
});

// ----------------------------------------------------------------- gateways
const members = (name) => Object.values(db.instances)
  .filter((rec) => UP.has(rec.phase) && resolved(rec).net_mode === 'gateway' && resolved(rec).gateway === name)
  .map((rec) => `${db.config.docker.name_prefix}-net-${rec.spec.id}`);

function gatewayStatus(name) {
  const prefix = db.config.docker.name_prefix;
  const cfg = db.config.network.gateways[name];
  const live = db.gateways[name] || { up: false };
  const subnet = cfg.subnet || '192.0.2.0/24';
  const base = {
    name, container: `${prefix}-gw-${name}`, network: `${prefix}-gw-${name}`, defined: true, running: Boolean(live.up), state: live.up ? 'connected' : 'stopped',
    ready: Boolean(live.up), subnet: live.up || cfg.subnet ? subnet : '', gateway_ip: live.up || cfg.subnet ? subnet.replace(/\.\d+\/\d+$/, '.2') : '',
    members: members(name), detail: '', fix: '',
  };
  if (live.up && !db.profiles.some((p) => p.name === cfg.profile)) return { ...base, state: 'error', ready: false, detail: `VPN profile ${cfg.profile} does not exist` };
  return base;
}

function gateway(name) {
  if (!GATEWAY.test(name)) throw bad(`bad gateway name '${name.slice(0, 32)}': use lower-case letters, digits and dashes`);
  if (!db.config.network.gateways[name]) throw notFound(`no gateway named '${name}'`);
  return name;
}

route('GET', '/api/net/gateways', () => ({
  gateways: Object.keys(db.config.network.gateways).sort().map((name) => ({ ...gatewayStatus(name), config: db.config.network.gateways[name] })),
}));

route('PUT', '/api/net/gateways/{name}', ({ params, body }) => {
  if (!GATEWAY.test(params.name)) throw bad(`bad gateway name '${params.name.slice(0, 32)}': use lower-case letters, digits and dashes`);
  const provider = body.provider || 'openvpn';
  if (!['openvpn', 'wireguard'].includes(provider)) throw new ApiError(422, 'validation_error', "provider: Input should be 'openvpn' or 'wireguard'");
  if (typeof body.profile !== 'string' || !body.profile) throw new ApiError(422, 'validation_error', 'profile: Field required', { detail: [{ field: 'profile', message: 'Field required' }] });
  if (!db.profiles.some((p) => p.name === body.profile)) throw notFound(`no VPN profile named '${body.profile.slice(0, 64)}'`);
  const subnet = body.subnet || '';
  // The demo keeps its addresses inside the documentation ranges.
  if (subnet && !/^(192\.0\.2|198\.51\.100|203\.0\.113)\.0\/24$/.test(subnet)) {
    throw bad('in the demo a gateway subnet is one of the documentation ranges: 192.0.2.0/24, 198.51.100.0/24 or 203.0.113.0/24 (a real farm takes any private /24)');
  }
  const entry = { provider, profile: body.profile, credentials: body.credentials || 'default', subnet };
  db.config.network.gateways[params.name] = entry;
  return { name: params.name, config: entry, note: 'saved; `up` (or starting an Android that uses it) applies it' };
});

route('DELETE', '/api/net/gateways/{name}', ({ params }) => {
  const name = gateway(params.name);
  const users = Object.values(db.instances).filter((rec) => resolved(rec).net_mode === 'gateway' && resolved(rec).gateway === name).map((rec) => rec.spec.id);
  if (users.length) throw bad(`gateway '${name}' is still the network of Android(s) ${users.slice(0, 10).join(', ')}; change their network first`);
  const prefix = db.config.docker.name_prefix;
  delete db.config.network.gateways[name];
  delete db.gateways[name];
  return { deleted: name, removed: [`${prefix}-gw-${name}`] };
});

route('POST', '/api/net/gateways/{name}/{action}', ({ params }) => {
  const name = gateway(params.name);
  const prefix = db.config.docker.name_prefix;
  if (params.action === 'up') {
    const was = db.gateways[name] && db.gateways[name].up;
    db.gateways[name] = { up: true, since: was ? db.gateways[name].since : now() };
    const status = gatewayStatus(name);
    return { name, container: status.container, network: status.network, uplink: `${prefix}-uplink`, uplink_gw: '192.0.2.1', subnet: status.subnet, gateway_ip: status.gateway_ip, action: was ? 'unchanged' : 'started' };
  }
  if (params.action === 'down') {
    db.gateways[name] = { up: false, since: 0 };
    return { name, removed: [`${prefix}-gw-${name}`] };
  }
  throw new ApiError(404, 'not_found', 'no such API route');
});
