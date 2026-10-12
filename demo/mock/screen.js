// The picture of a pretend Android, drawn on a canvas from the state in device.js.
// Browser only. Every frame the panel shows comes from here as a JPEG or PNG blob; no
// picture is requested from anywhere.

import * as device from './device.js';
import { resolved, UP } from './farm.js';
import { db, now } from './store.js';

const TINTS = ['#35d6dd', '#f6bd55', '#9c93ff', '#45dc9c', '#ff7a85', '#78b2ff'];
const LIGHT = { bg: '#eef1f7', card: '#ffffff', text: '#131a28', muted: '#5b667a', accent: '#0a6c80', line: '#d5dbe7', bar: '#e2e7f1', on: '#ffffff' };
const DARK = { bg: '#0d111b', card: '#1a2131', text: '#eef2f8', muted: '#9aa6ba', accent: '#35d6dd', line: '#2a3447', bar: '#141926', on: '#05121b' };
const FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

const cache = new Map();            // "id:width" -> { canvas, key, jpeg }

function box(ctx, [l, t, r, b], radius, fill, stroke) {
  ctx.beginPath();
  ctx.roundRect(l, t, r - l, b - t, radius);
  if (fill) { ctx.fillStyle = fill; ctx.fill(); }
  if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = 1.5; ctx.stroke(); }
}

function words(ctx, text, x, y, { size = 18, color, weight = 400, align = 'left', max } = {}) {
  ctx.font = `${weight} ${size}px ${FONT}`;
  ctx.fillStyle = color;
  ctx.textAlign = align;
  ctx.textBaseline = 'middle';
  let shown = String(text);
  if (max) while (shown.length > 1 && ctx.measureText(shown).width > max) shown = `${shown.slice(0, -2)}…`;
  ctx.fillText(shown, x, y);
}

function drawBoot(ctx, rec, t) {
  ctx.fillStyle = '#000000';
  ctx.fillRect(0, 0, device.W, device.H);
  const r = resolved(rec);
  words(ctx, rec.phase === 'starting' ? 'Starting the container' : `Booting Android ${r.android_version}`, device.W / 2, 400, { size: 22, color: '#e9edf5', align: 'center', weight: 600 });
  words(ctx, 'a drawing made by the demo', device.W / 2, 436, { size: 15, color: '#8c97ad', align: 'center' });
  const beat = Math.floor(t * 3) % 3;
  for (let i = 0; i < 3; i++) {
    ctx.beginPath();
    ctx.arc(device.W / 2 - 24 + i * 24, 490, 6, 0, Math.PI * 2);
    ctx.fillStyle = i === beat ? '#35d6dd' : '#2a3447';
    ctx.fill();
  }
}

function drawSwitch(ctx, p, x, y, on) {
  box(ctx, [x, y - 13, x + 48, y + 13], 13, on ? p.accent : p.line);
  ctx.beginPath();
  ctx.arc(on ? x + 35 : x + 13, y, 9, 0, Math.PI * 2);
  ctx.fillStyle = p.card;
  ctx.fill();
}

function drawScreen(ctx, rec, t) {
  const state = device.ui(rec);
  const p = rec.device.dark_mode ? DARK : LIGHT;
  const scale = Math.min(1.4, Math.max(0.8, Number(rec.device.font_scale) || 1));
  const { W, H } = device;
  if (state.off) {
    ctx.fillStyle = '#000000';
    ctx.fillRect(0, 0, W, H);
    words(ctx, 'screen off', W / 2, H / 2, { size: 16, color: '#3a455c', align: 'center' });
    return;
  }
  ctx.fillStyle = p.bg;
  ctx.fillRect(0, 0, W, H);
  // Status bar
  const clock = new Date(t * 1000);
  words(ctx, `${String(clock.getHours()).padStart(2, '0')}:${String(clock.getMinutes()).padStart(2, '0')}`, 16, 16, { size: 14, color: p.text, weight: 600 });
  words(ctx, `${state.wifi ? 'Wi-Fi' : 'no Wi-Fi'}  ·  vol ${state.volume}  ·  demo`, W - 16, 16, { size: 13, color: p.muted, align: 'right' });

  const list = device.elements(rec);
  const el = (id) => list.find((e) => e.id === id);
  if (state.dialog) {
    box(ctx, [60, 330, 420, 550], 22, p.card, p.line);
    words(ctx, state.dialog, W / 2, 392, { size: 20 * scale, color: p.text, align: 'center', weight: 650, max: 320 });
    words(ctx, 'A pop-up the flow does not know.', W / 2, 428, { size: 14, color: p.muted, align: 'center' });
    box(ctx, el('dialog_dismiss').bounds, 14, p.accent);
    words(ctx, 'Not now', W / 2, 498, { size: 17, color: p.on, align: 'center', weight: 650 });
  } else if (state.app === 'launcher') {
    words(ctx, `${String(clock.getHours()).padStart(2, '0')}:${String(clock.getMinutes()).padStart(2, '0')}`, W / 2, 132, { size: 64, color: p.text, align: 'center', weight: 300 });
    words(ctx, clock.toLocaleDateString('en', { weekday: 'long', month: 'long', day: 'numeric' }), W / 2, 190, { size: 16, color: p.muted, align: 'center' });
    const search = el('search');
    box(ctx, search.bounds, 26, p.card, p.line);
    words(ctx, state.text || search.hint, 46, 276, { size: 17 * scale, color: state.text ? p.text : p.muted, max: 380 });
    for (const app of list.filter((e) => e.package)) {
      const cx = (app.bounds[0] + app.bounds[2]) / 2;
      box(ctx, [cx - 34, app.bounds[1] + 6, cx + 34, app.bounds[1] + 74], 20, TINTS[app.tint % TINTS.length]);
      words(ctx, app.text.slice(0, 1).toUpperCase(), cx, app.bounds[1] + 41, { size: 30, color: '#05121b', align: 'center', weight: 700 });
      words(ctx, app.text, cx, app.bounds[1] + 96, { size: 13 * scale, color: p.text, align: 'center', max: 108 });
    }
    words(ctx, 'Tap an app. Type to fill the search box.', W / 2, 770, { size: 13, color: p.muted, align: 'center' });
  } else if (state.app === 'example.settings') {
    words(ctx, state.page === 'about' ? 'About this device' : 'Settings', 24, 62, { size: 26 * scale, color: p.text, weight: 650 });
    if (state.page === 'about') {
      const r = resolved(rec);
      const rows = [['Model', r.model], ['Android', r.android_version], ['Screen', `${r.width} × ${r.height}, ${r.dpi} dpi`], ['Rendering', rec.mode || 'unknown'], ['What this is', 'a drawing, not a device']];
      rows.forEach(([name, value], index) => {
        const y = 128 + index * 64;
        words(ctx, name, 24, y - 10, { size: 13, color: p.muted });
        words(ctx, value, 24, y + 14, { size: 18 * scale, color: p.text, max: 430 });
      });
      box(ctx, [24, 728, 456, 776], 14, p.card, p.line);
      words(ctx, 'Back to Settings', W / 2, 752, { size: 16, color: p.accent, align: 'center', weight: 600 });
    } else {
      for (const row of list.filter((e) => ['wifi', 'dark', 'animations', 'about'].includes(e.id))) {
        const y = (row.bounds[1] + row.bounds[3]) / 2;
        ctx.fillStyle = p.card;
        ctx.fillRect(0, row.bounds[1] + 1, W, 62);
        words(ctx, row.text, 24, y, { size: 18 * scale, color: p.text });
        if (row.cls === 'Switch') drawSwitch(ctx, p, W - 76, y, row.checked);
        else words(ctx, '›', W - 30, y, { size: 26, color: p.muted, align: 'center' });
      }
    }
  } else if (state.app === 'link') {
    words(ctx, 'Open a link', 24, 62, { size: 26 * scale, color: p.text, weight: 650 });
    box(ctx, [24, 110, 456, 166], 12, p.card, p.line);
    words(ctx, state.link, 40, 138, { size: 15, color: p.text, max: 400 });
    words(ctx, 'The demo shows the address', W / 2, 300, { size: 17, color: p.muted, align: 'center' });
    words(ctx, 'and does not load it.', W / 2, 326, { size: 17, color: p.muted, align: 'center' });
    box(ctx, el('close').bounds, 14, p.accent);
    words(ctx, 'Close', W / 2, 588, { size: 17, color: p.on, align: 'center', weight: 650 });
  } else {
    const label = (device.apps(rec).find((app) => app.package === state.app) || { label: state.app }).label;
    words(ctx, label, 24, 62, { size: 26 * scale, color: p.text, weight: 650, max: 430 });
    box(ctx, el('new_note').bounds, 14, p.card, p.line);
    words(ctx, 'New note', 128, 122, { size: 17 * scale, color: p.accent, align: 'center', weight: 600 });
    box(ctx, el('save').bounds, 14, p.accent);
    words(ctx, 'Save', 352, 122, { size: 17 * scale, color: p.on, align: 'center', weight: 650 });
    const field = el('title');
    box(ctx, field.bounds, 12, p.card, field.focused ? p.accent : p.line);
    words(ctx, state.text ? `${state.text}${field.focused ? '|' : ''}` : (field.focused ? '|' : field.hint), 40, 196, { size: 17 * scale, color: state.text || field.focused ? p.text : p.muted, max: 400 });
    const saved = state.lists[state.app] || [];
    words(ctx, saved.length ? 'Saved' : 'Nothing saved yet', 24, 262, { size: 13, color: p.muted });
    saved.forEach((line, index) => {
      const y = 286 + index * 58;
      box(ctx, [24, y, 456, y + 48], 12, p.card, p.line);
      words(ctx, line, 40, y + 24, { size: 16 * scale, color: p.text, max: 400 });
    });
  }
  // Navigation bar
  if (state.app !== 'launcher' || state.dialog) {
    ctx.fillStyle = p.bar;
    ctx.fillRect(0, 800, W, 54);
    ctx.strokeStyle = p.muted;
    ctx.fillStyle = p.muted;
    ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.moveTo(88, 815); ctx.lineTo(72, 827); ctx.lineTo(88, 839); ctx.stroke();
    ctx.beginPath(); ctx.arc(240, 827, 11, 0, Math.PI * 2); ctx.stroke();
    ctx.strokeRect(390, 817, 20, 20);
  }
  // Where the last touch landed, for a moment.
  if (state.touch && t - state.touch.at < 0.7) {
    const age = (t - state.touch.at) / 0.7;
    ctx.beginPath();
    ctx.arc(state.touch.x * W, state.touch.y * H, 14 + age * 22, 0, Math.PI * 2);
    ctx.strokeStyle = p.accent;
    ctx.globalAlpha = 1 - age;
    ctx.lineWidth = 4;
    ctx.stroke();
    ctx.globalAlpha = 1;
  }
}

function canvasFor(id, width) {
  const rec = db.instances[id];
  if (!rec || !UP.has(rec.phase)) return null;
  const t = now();
  const r = resolved(rec);
  const w = Math.max(96, Math.min(1080, Math.round(width)));
  const h = Math.round((w * r.height) / r.width);
  const slot = `${id}:${w}`;
  const key = `${device.frameKey(rec, t)}:${h}`;
  let entry = cache.get(slot);
  if (!entry) { entry = { canvas: document.createElement('canvas'), key: '', jpeg: null }; cache.set(slot, entry); }
  if (entry.key === key) return entry;
  entry.canvas.width = w;
  entry.canvas.height = h;
  const ctx = entry.canvas.getContext('2d');
  ctx.setTransform(w / device.W, 0, 0, h / device.H, 0, 0);
  if (rec.phase === 'booted') drawScreen(ctx, rec, t); else drawBoot(ctx, rec, t);
  entry.key = key;
  entry.jpeg = null;
  return entry;
}

function toBlob(canvas, type, quality) {
  const data = canvas.toDataURL(type, quality);
  const raw = atob(data.slice(data.indexOf(',') + 1));
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return new Blob([bytes], { type });
}

// A JPEG of what Android `id` shows now, or null when it is not running.
export function frame(id, width = 480) {
  const entry = canvasFor(id, width);
  if (!entry) return null;
  if (!entry.jpeg) entry.jpeg = toBlob(entry.canvas, 'image/jpeg', 0.82);
  return entry.jpeg;
}

export function screenshot(id) {
  const rec = db.instances[id];
  const entry = canvasFor(id, rec ? resolved(rec).width : 480);
  return entry ? toBlob(entry.canvas, 'image/png') : null;
}

// Changes whenever frame() would return a different picture.
export function frameVersion(id) {
  const rec = db.instances[id];
  return rec && UP.has(rec.phase) ? device.frameKey(rec, now()) : 'off';
}

export function forget() { cache.clear(); }
