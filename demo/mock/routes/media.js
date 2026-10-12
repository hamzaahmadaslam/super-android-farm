// Virtual camera and microphone, the media library, and listening to one device.
// No picture or sound exists in the demo: these routes keep the lists a real farm shows.

import { created, route } from '../backend.js';
import { ApiError, bad, conflict, notFound, record, targets, UP } from '../farm.js';
import { db, now } from '../store.js';

const VIDEO = ['.mp4', '.mkv', '.mov', '.webm', '.avi'];
const AUDIO = ['.mp3', '.wav', '.ogg', '.m4a', '.flac', '.opus'];
const camera = () => db.config.media.camera;
const rtmp = () => `rtmp://${camera().rtmp_bind}:${camera().rtmp_port}/live`;

function availability() {
  const c = camera();
  const first = c.device_base + 1;
  const all = Array.from({ length: c.slots }, (_, index) => first + index);
  const reasons = c.enabled ? [] : ['media.camera.enabled is off: set it to true in the farm’s config, then run `sudo sf host apply`.'];
  return {
    enabled: c.enabled, ok: c.enabled, reasons, module: true,
    devices: { first, slots: c.slots, usable: c.enabled ? all : [], missing: c.enabled ? [] : all, foreign: [], no_access: [] },
    tools: { ffmpeg: '/usr/bin/ffmpeg', ffprobe: '/usr/bin/ffprobe', 'v4l2-ctl': '/usr/bin/v4l2-ctl' },
    overlay: { 13: { path: '/home/demo/.local/share/superfarm/overlays/camera/13', present: c.enabled, missing: [] } },
    android_versions: ['13'], rtmp: `${rtmp()}/camera`,
  };
}

function status() {
  const instances = {};
  for (const feed of db.camera.feeds) {
    for (const id of feed.ids) instances[id] = { device: `/dev/video${camera().device_base + id}`, format: '640x360', copier: true, forwarder: true, muted: db.camera.muted.includes(id), feed: feed.id };
  }
  return { enabled: camera().enabled, feeds: db.camera.feeds.map((feed) => ({ ...feed, running: true })), instances, muted: db.camera.muted, rtmp: `${rtmp()}/camera` };
}

function ready() {
  if (!camera().enabled) throw conflict('the virtual camera is switched off on this farm: set media.camera.enabled to true (Settings, Media), then run `sudo sf host apply`');
}

route('GET', '/api/camera', () => ({ available: availability(), status: status() }));
route('GET', '/api/camera/sources', () => ({
  sources: [...db.media, { id: 'rtmp', name: 'Live stream (RTMP)', kind: 'live', server: rtmp(), key: 'camera', active: false }],
}));

route('POST', '/api/camera/play', ({ body }) => {
  ready();
  const who = targets(body).filter((id) => UP.has(db.instances[id].phase));
  if (!who.length) throw bad('none of the selected Androids is running');
  const source = db.media.find((entry) => entry.id === body.source) || (body.source === 'rtmp' ? { id: 'rtmp' } : null);
  if (!source) throw notFound(`no media file named '${String(body.source).slice(0, 80)}'`);
  for (const feed of db.camera.feeds) feed.ids = feed.ids.filter((id) => !who.includes(id));
  const feed = { id: `feed-${Math.round(now())}`, source: source.id, sound: body.sound || 'source', ids: who, started: now() };
  db.camera.feeds = [...db.camera.feeds.filter((entry) => entry.ids.length), feed];
  return { ok: true, feed: { ...feed, running: true }, results: Object.fromEntries(who.map((id) => [id, { ok: true, detail: 'ready' }])) };
});

route('POST', '/api/camera/stop', ({ body }) => {
  const who = body.ids || body.selector ? targets(body) : Object.keys(db.instances).map(Number);
  const stopped = [];
  for (const feed of db.camera.feeds) {
    const before = feed.ids.length;
    feed.ids = feed.ids.filter((id) => !who.includes(id));
    if (feed.ids.length !== before) stopped.push(feed.id);
  }
  db.camera.feeds = db.camera.feeds.filter((feed) => feed.ids.length);
  return { ok: true, stopped, ids: [...who].sort((a, b) => a - b) };
});

route('POST', '/api/camera/mic', ({ body }) => {
  if (!['mute', 'unmute', 'solo'].includes(body.mode)) throw new ApiError(422, 'validation_error', "mode: Input should be 'mute', 'unmute' or 'solo'");
  const who = targets(body, { allowEmpty: body.mode === 'unmute' });
  const everyone = Object.keys(db.instances).map(Number);
  if (body.mode === 'mute') db.camera.muted = [...new Set([...db.camera.muted, ...who])].sort((a, b) => a - b);
  else if (body.mode === 'unmute') db.camera.muted = who.length ? db.camera.muted.filter((id) => !who.includes(id)) : [];
  else db.camera.muted = everyone.filter((id) => !who.includes(id));
  return { ok: true, mode: body.mode, muted: db.camera.muted };
});

route('POST', '/api/camera/attach', ({ body }) => { ready(); return { ok: true, ids: targets(body) }; });
route('POST', '/api/camera/install-hal', ({ body }) => {
  ready();
  const who = targets(body);
  return { ok: true, results: Object.fromEntries(who.map((id) => [id, { ok: true, detail: 'driver installed; restart the Android to load it' }])) };
});

// The file's name and size are listed; its content is never opened.
route('POST', '/api/media', ({ file }) => {
  if (!file) throw bad('no file in the request');
  const name = file.name.replace(/[/\\]/g, '_').slice(0, 120);
  const ext = name.slice(name.lastIndexOf('.')).toLowerCase();
  if (![...VIDEO, ...AUDIO].includes(ext)) throw bad(`unsupported media type; the library takes ${[...AUDIO, ...VIDEO].sort().join(' ')}`);
  db.media = [...db.media.filter((entry) => entry.id !== name), { id: name, name, kind: VIDEO.includes(ext) ? 'video' : 'audio', size: file.size, modified: now() }]
    .sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
  return created({ name, size: file.size });
});

route('DELETE', '/api/media/{name}', ({ params }) => {
  if (!db.media.some((entry) => entry.id === params.name)) throw notFound(`no such media file: ${params.name.slice(0, 80)}`);
  db.media = db.media.filter((entry) => entry.id !== params.name);
  return { deleted: params.name };
});

// ------------------------------------------------------------------- listen
route('GET', '/api/listen', () => (db.listening == null ? { listening: null } : { listening: db.listening.id, since: db.listening.since, port: 0 }));
route('POST', '/api/listen/{id}', ({ params }) => {
  const rec = record(params.id);
  if (!UP.has(rec.phase)) throw conflict(`could not start listening to Android ${rec.spec.id}: it is not running`);
  db.listening = { id: rec.spec.id, since: now() };
  return { listening: rec.spec.id, since: db.listening.since, port: 0 };
});
route('DELETE', '/api/listen', () => { db.listening = null; return { listening: null }; });
