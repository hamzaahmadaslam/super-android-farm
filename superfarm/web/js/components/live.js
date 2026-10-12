// Seeing and touching a device.
//
//   thumbnails(scope)  cheap stills for grids: one JPEG per tile per second, only for
//                      tiles on screen and only while the tab is visible
//   liveView(...)      the interactive view: MJPEG in an <img> by default; the device's
//                      own H.264 through WebCodecs when the daemon offers it and the
//                      browser can decode it, falling back silently
//
// Gestures are reported in normalized coordinates (0..1 of the picture), so the same
// tap lands on the same spot of devices with different resolutions. The view does not
// send anything itself: the page decides where input goes (one device, every selected
// device, or the recorder).

import { query, socketUrl } from '../api.js';
import { h } from '../dom.js';
import { icon } from '../icons.js';

const TAP_SLOP = 0.02;          // movement below 2% of the picture is still a tap
const LONG_PRESS_MS = 600;

// ---------------------------------------------------------------- thumbnails
export function thumbnails(scope, { fps = 1, width = 240 } = {}) {
  const visible = new Set();
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) visible[entry.isIntersecting ? 'add' : 'delete'](entry.target);
  }, { rootMargin: '100px' });
  const timer = setInterval(() => {
    if (document.hidden) return;
    for (const img of visible) {
      // Never queue a second request behind a slow one: a busy device just updates less often.
      if (img.dataset.loading || !img.isConnected) continue;
      // A device that is being stopped or restarted from here has no picture to give, and
      // one that just failed to give a frame is left alone for a few seconds.
      if (Number(img.dataset.retryAt || 0) > Date.now()) continue;
      const card = img.closest('[data-thumb-owner]');
      if (card && card.querySelector('.is-busy')) continue;
      img.dataset.loading = '1';
      img.src = `/api/instances/${img.dataset.id}/frame.jpg${query({ width, t: Date.now() })}`;
    }
  }, 1000 / fps);
  scope.add(() => { clearInterval(timer); observer.disconnect(); visible.clear(); });

  return {
    // An <img> for one Android. `alt` describes it for screen readers.
    image(id, alt) {
      const img = h('img', { class: 'thumb', alt, dataset: { id: String(id) }, decoding: 'async' });
      const done = () => { delete img.dataset.loading; };
      img.addEventListener('load', done);
      img.addEventListener('error', () => { done(); img.dataset.retryAt = String(Date.now() + 5000); });
      observer.observe(img);
      return img;
    },
    forget(img) { observer.unobserve(img); visible.delete(img); },
  };
}

// ------------------------------------------------------------------- H.264
// Frames arrive as: 1 byte flags (1 = key frame, 2 = starts with SPS/PPS), 8 bytes
// timestamp (microseconds), 2 + 2 bytes width and height, then one Annex-B access unit.
const HEADER = 13;

// "avc1.PPCCLL" from the SPS that precedes every key frame.
export function codecFromAnnexB(data) {
  for (let i = 0; i + 7 < data.length; i++) {
    if (data[i] === 0 && data[i + 1] === 0 && data[i + 2] === 1 && (data[i + 3] & 0x1f) === 7) {
      return 'avc1.' + [data[i + 4], data[i + 5], data[i + 6]].map((b) => b.toString(16).padStart(2, '0')).join('');
    }
  }
  return null;
}

function startH264(id, canvas, onFail) {
  const context = canvas.getContext('2d');
  let decoder = null;
  let waitingForKey = true;
  let closed = false;
  const stop = () => {
    if (closed) return;
    closed = true;
    try { socket.close(); } catch { /* already closed */ }
    try { if (decoder && decoder.state !== 'closed') decoder.close(); } catch { /* already closed */ }
  };
  const giveUp = () => { const first = !closed; stop(); if (first) onFail(); };
  const socket = new WebSocket(socketUrl(`/api/instances/${id}/h264`));
  socket.binaryType = 'arraybuffer';
  socket.addEventListener('error', giveUp);
  socket.addEventListener('close', giveUp);
  socket.addEventListener('message', (event) => {
    if (typeof event.data === 'string' || closed) return;        // status text is not needed to decode
    const view = new DataView(event.data);
    if (view.byteLength <= HEADER) return;
    const flags = view.getUint8(0);
    const key = Boolean(flags & 1);
    const data = new Uint8Array(event.data, HEADER);
    try {
      if (key && flags & 2 && (!decoder || decoder.state === 'closed')) {
        const codec = codecFromAnnexB(data);
        if (!codec) return;
        decoder = new VideoDecoder({
          output(frame) {
            if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
              canvas.width = frame.displayWidth;
              canvas.height = frame.displayHeight;
            }
            context.drawImage(frame, 0, 0);
            frame.close();
          },
          error: giveUp,
        });
        // No `description`: the stream is Annex-B and carries its parameter sets in band.
        decoder.configure({ codec, optimizeForLatency: true });
      }
      if (!decoder) return;
      // A stalled tab must not build a backlog; drop to the next key frame instead.
      if (decoder.decodeQueueSize > 15) waitingForKey = true;
      if (waitingForKey && !key) return;
      waitingForKey = false;
      decoder.decode(new EncodedVideoChunk({
        type: key ? 'key' : 'delta', timestamp: Number(view.getBigUint64(1)), data,
      }));
    } catch {
      giveUp();
    }
  });
  return stop;
}

// ---------------------------------------------------------------- live view
const KEYS = {
  Enter: 'ENTER', Backspace: 'DEL', Delete: 'FORWARD_DEL', Escape: 'BACK',
  ArrowUp: 'DPAD_UP', ArrowDown: 'DPAD_DOWN', ArrowLeft: 'DPAD_LEFT', ArrowRight: 'DPAD_RIGHT',
  Home: 'MOVE_HOME', End: 'MOVE_END', PageUp: 'PAGE_UP', PageDown: 'PAGE_DOWN',
};
let h264Failed = false;
// [label, Android key name, icon]. The first three are the navigation bar.
export const DEVICE_KEYS = [
  ['Back', 'BACK', 'back'], ['Home', 'HOME', 'home'], ['Recents', 'APP_SWITCH', 'recents'],
  ['Volume down', 'VOLUME_DOWN', 'volume-down'], ['Volume up', 'VOLUME_UP', 'volume-up'], ['Power', 'POWER', 'power'],
];

// The device buttons as a group of icon buttons. `press(name)` sends one key.
export function deviceKeys(press, { label = 'Device buttons', small = false, extra } = {}) {
  return h('div', { class: 'live-keys', role: 'group', 'aria-label': label },
    DEVICE_KEYS.map(([text, name, picture], index) => [
      index === 3 ? h('span', { class: 'sep', 'aria-hidden': 'true' }) : null,
      h('button', { type: 'button', class: `btn ghost icon-only ${small ? 'small' : ''}`, 'aria-label': text, title: text, onclick: () => press(name) }, icon(picture)),
    ]),
    extra ? [h('span', { class: 'sep', 'aria-hidden': 'true' }), extra] : null);
}

// options:
//   send(kind, payload)  kind = tap | long-press | swipe | text | key, payload as the body of
//                        POST /api/input/<kind> without the targets. Required.
//   h264                 offer the H.264 transport (the daemon's capability says so)
//   fps, width           of the MJPEG stream
//   aspect               [width, height] of the device screen, to reserve the space
//   keys                 the Back / Home / ... buttons: true (under the picture, the
//                        default), 'side' (a strip beside it) or false
//   frame                draw the picture inside a device bezel
//   extra                more controls for the end of the button strip
export function liveView(id, scope, { send, h264 = false, fps = 8, width = 480, aspect, keys = true, label, frame = false, extra }) {
  const note = h('span', { class: 'live-note' });
  const say = (words, live = false) => { note.textContent = words; note.classList.toggle('on', live); };
  say('connecting');
  const surface = h('div', {
    class: 'live-surface', tabindex: '0', role: 'application',
    'aria-label': `${label || `Android ${id}`} screen. Click or drag to touch it; type to send keys. Escape sends Back.`,
  }, note);
  if (aspect && aspect[0] && aspect[1]) surface.style.aspectRatio = `${aspect[0]} / ${aspect[1]}`;
  let picture = null;
  let stopTransport = () => {};

  function startMjpeg() {
    const img = h('img', { alt: '', draggable: false });
    img.addEventListener('load', () => { say('live', true); surface.style.aspectRatio = ''; }, { once: true });
    img.addEventListener('error', () => { say('no picture (is this Android running?)'); });
    img.src = `/api/instances/${id}/stream.mjpg${query({ fps, width })}`;
    show(img);
    // Clearing src is what ends the multipart request; removing the element alone does not.
    stopTransport = () => { img.src = ''; };
  }

  function show(element) {
    if (picture) picture.remove();
    picture = element;
    surface.prepend(element);
  }

  function start() {
    stopTransport();
    if (h264 && !h264Failed && 'VideoDecoder' in window && 'EncodedVideoChunk' in window) {
      const canvas = h('canvas', { 'aria-hidden': 'true' });
      show(canvas);
      say('live (H.264)', true);
      stopTransport = startH264(id, canvas, () => {
        // Once it failed, the other views of this session go straight to MJPEG.
        h264Failed = true;
        if (!stopped && !document.hidden) startMjpeg();
      });
    } else {
      startMjpeg();
    }
  }

  // A hidden tab keeps no stream open: devices nobody looks at should cost nothing.
  let stopped = false;
  scope.on(document, 'visibilitychange', () => {
    if (stopped) return;
    if (document.hidden) { stopTransport(); stopTransport = () => {}; say('paused'); } else start();
  });
  scope.add(() => { stopped = true; stopTransport(); clearTimeout(typing); });

  // ------------------------------------------------------------- pointer
  const point = (event) => {
    const box = (picture || surface).getBoundingClientRect();
    const clamp = (v) => Math.min(1, Math.max(0, v));
    return [clamp((event.clientX - box.left) / box.width), clamp((event.clientY - box.top) / box.height)];
  };
  let down = null;
  surface.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    surface.focus({ preventScroll: true });
    surface.setPointerCapture(event.pointerId);
    down = { at: point(event), time: performance.now() };
    event.preventDefault();
  });
  surface.addEventListener('pointerup', (event) => {
    if (!down) return;
    const [x1, y1] = down.at;
    const [x2, y2] = point(event);
    const ms = Math.round(performance.now() - down.time);
    down = null;
    const r = (v) => Math.round(v * 10000) / 10000;
    if (Math.hypot(x2 - x1, y2 - y1) > TAP_SLOP) {
      send('swipe', { x1: r(x1), y1: r(y1), x2: r(x2), y2: r(y2), ms: Math.min(3000, Math.max(80, ms)) });
    } else if (ms >= LONG_PRESS_MS) {
      send('long-press', { x: r(x1), y: r(y1), ms: Math.min(5000, ms) });
    } else {
      send('tap', { x: r(x1), y: r(y1) });
    }
  });
  surface.addEventListener('pointercancel', () => { down = null; });
  surface.addEventListener('contextmenu', (event) => event.preventDefault());

  // ------------------------------------------------------------ keyboard
  // Printable characters are batched for a moment so a typed word is one request,
  // and a named key always goes out after the text typed before it.
  let buffer = '';
  let typing = 0;
  const flush = () => {
    clearTimeout(typing);
    if (buffer) send('text', { value: buffer });
    buffer = '';
  };
  surface.addEventListener('keydown', (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    // Tab is never captured: keyboard users must always be able to leave the view.
    if (KEYS[event.key]) {
      flush();
      send('key', { name: KEYS[event.key] });
    } else if (event.key.length === 1) {
      buffer += event.key;
      clearTimeout(typing);
      typing = setTimeout(flush, 250);
    } else {
      return;
    }
    event.preventDefault();
  });
  surface.addEventListener('paste', (event) => {
    const pasted = event.clipboardData && event.clipboardData.getData('text');
    if (pasted) { event.preventDefault(); flush(); send('text', { value: pasted }); }
  });
  surface.addEventListener('blur', flush);

  const buttons = keys && deviceKeys((name) => send('key', { name }), { extra });

  start();
  return h('div', { class: `live ${keys === 'side' ? 'side' : ''}` }, frame ? h('div', { class: 'device' }, surface) : surface, buttons);
}
