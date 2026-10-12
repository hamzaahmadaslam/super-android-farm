// The demo's transport: the same four functions as superfarm/web/js/transport.js, answered
// inside the page. The build puts this in the panel's place of that file, and that is the
// only difference between the panel a farm serves and the panel of the demo.
//
// Nothing here opens a connection. request() calls the route table in backend.js,
// openSocket() returns an object that only relays the demo's own events, and pictures are
// blobs drawn by screen.js. A file a visitor picks is never opened: its name and size are
// passed on, its content is not read.

import { installChrome } from './chrome.js';
import { advance, db, handle, reset, save, served, subscribe } from './index.js';
import { frame, frameVersion, screenshot } from './screen.js';

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

// ------------------------------------------------------------------ request
export async function request(path, init = {}) {
  const method = (init.method || 'GET').toUpperCase();
  let body;
  let file = null;
  if (path === '/api/auth/login') {
    body = {};                      // whatever was typed as the token stops here
  } else if (typeof FormData !== 'undefined' && init.body instanceof FormData) {
    body = {};
    for (const [name, value] of init.body.entries()) {
      if (typeof value === 'string') body[name] = value;
      else file = { name: value.name, size: value.size };         // the file itself is not touched
    }
  } else if (typeof init.body === 'string') {
    try { body = JSON.parse(init.body); } catch { body = {}; }
  }
  // A short wait, so that buttons show their busy state as they do against a real daemon.
  await sleep(path.startsWith('/api/input/') ? 12 : 45 + Math.random() * 90);
  const answer = handle(method, path, body, file);
  return new Response(JSON.stringify(answer.body), { status: answer.status, headers: { 'content-type': 'application/json' } });
}

// ------------------------------------------------------------------- socket
// Looks enough like a WebSocket for the panel: events, close(), readyState.
class DemoSocket extends EventTarget {
  constructor(kind) {
    super();
    this.readyState = 0;
    this.binaryType = 'blob';
    this.stop = () => {};
    setTimeout(() => {
      if (this.readyState !== 0) return;
      if (kind !== 'events') {
        // There is no video stream to relay: the view falls back to pictures by itself.
        this.readyState = 3;
        this.dispatchEvent(new Event('error'));
        this.dispatchEvent(new CloseEvent('close', { code: 1011 }));
        return;
      }
      this.readyState = 1;
      this.dispatchEvent(new Event('open'));
      this.stop = subscribe((message) => {
        if (message.type !== 'note') this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(message) }));
      });
    }, 0);
  }

  send() {}

  close() {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.stop();
    setTimeout(() => this.dispatchEvent(new CloseEvent('close', { code: 1000 })), 0);
  }
}

export function openSocket(path) {
  const hit = served('WS', path);
  return new DemoSocket(hit ? hit.kind : 'unknown');
}

// -------------------------------------------------------------------- media
const urls = [];                    // [url, made] of frames, revoked once nothing shows them
function lend(blob, life = 15000) {
  const url = URL.createObjectURL(blob);
  const t = Date.now();
  urls.push([url, t + life]);
  while (urls.length && urls[0][1] < t) URL.revokeObjectURL(urls.shift()[0]);
  return url;
}
const NO_PICTURE = new Blob([], { type: 'image/jpeg' });           // an <img> reports this as "no picture"
const note = (lines) => new Blob([`${lines.join('\n')}\n`], { type: 'text/plain' });

// Download links are filled in at the moment they are used, so a screenshot shows the
// screen as it is then and not as it was when the page was drawn.
const downloads = new Map();        // placeholder url -> () => Blob
function download(make) {
  const url = lend(note(['Super Farm demo']), 30 * 60 * 1000);
  downloads.set(url, make);
  if (downloads.size > 300) downloads.delete(downloads.keys().next().value);
  return url;
}

export function mediaUrl(path) {
  const hit = served('GET', path);
  if (!hit) return path;
  const id = Number(hit.params.id);
  switch (hit.kind) {
    case 'frame': case 'stream': return lend(frame(id, Number(hit.query.width) || 480) || NO_PICTURE);
    case 'screenshot': return download(() => screenshot(id) || note([`Android ${id} is not running, so there is no screen to save.`]));
    case 'file': return download(() => note([
      'Super Farm demo.', `This stands in for ${hit.query.path || 'a file'} on Android ${id}.`,
      'The demo has no devices and no files: nothing was downloaded from anywhere.']));
    case 'backup': return download(() => note([
      'Super Farm demo.', `This stands in for the backup ${hit.query.archive || ''}.`,
      'A real backup is a compressed archive of an Android’s data with checksums. The demo has no data to archive.']));
    default: return path;
  }
}

export function streamInto(img, path) {
  const hit = served('GET', path);
  const id = hit ? Number(hit.params.id) : 0;
  const width = (hit && Number(hit.query.width)) || 480;
  const fps = Math.min(10, Math.max(1, (hit && Number(hit.query.fps)) || 8));
  let shown = null;
  let current = '';
  const show = () => {
    const version = `${frameVersion(id)}`;
    if (version === shown) return;
    shown = version;
    const blob = frame(id, width);
    const old = current;
    current = URL.createObjectURL(blob || NO_PICTURE);
    img.src = current;
    if (old) setTimeout(() => URL.revokeObjectURL(old), 1500);
  };
  show();
  const timer = setInterval(show, 1000 / fps);
  return () => {
    clearInterval(timer);
    img.src = '';
    if (current) URL.revokeObjectURL(current);
  };
}

// ------------------------------------------------------------------ start-up
// Time moves the pretend farm: tasks progress, Androids boot, runs advance.
setInterval(() => { if (!document.hidden) advance(); }, 500);
document.addEventListener('visibilitychange', () => { if (!document.hidden) advance(); });
window.addEventListener('pagehide', save);
document.addEventListener('click', (event) => {
  const link = event.target instanceof Element ? event.target.closest('a[download]') : null;
  const make = link && downloads.get(link.href);
  if (make) link.href = lend(make(), 60000);
}, true);

installChrome({
  subscribe,
  signedIn: () => Boolean(db.session.signedIn),
  reset() {
    const session = db.session;
    reset();
    db.session = session;           // starting over does not sign the visitor out
    save();
  },
});
