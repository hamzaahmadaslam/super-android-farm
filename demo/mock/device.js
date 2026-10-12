// What is "on the screen" of a pretend Android: a tiny invented launcher, a settings
// page and one simple list app. It is a state machine over plain data, so the same code
// answers a tap from the live view, a key from the control wall, a step of an automation
// run and the recorder's question "what is under this point?".
//
// The picture itself is drawn from this state in screen.js. Nothing here is Android: no
// real launcher, app or brand is imitated.

import { db, now } from './store.js';

export const W = 480;             // the layout is designed at 480 x 854 and scaled when drawn
export const H = 854;
const BUILT_IN = [
  { package: 'example.settings', label: 'Settings', tint: 3 },
  { package: 'example.notepad', label: 'Notepad', tint: 1 },
];

export function ui(rec) {
  if (!rec.ui) rec.ui = { app: 'launcher', page: '', text: '', focus: '', lists: {}, wifi: true, volume: 7, off: false, link: '', dialog: '', rev: 0, touch: null };
  return rec.ui;
}

export function resetUi(rec) { delete rec.ui; }

const labelOf = (pkg) => {
  const builtin = BUILT_IN.find((app) => app.package === pkg);
  if (builtin) return builtin.label;
  const known = db.library.find((app) => app.package === pkg);
  return known ? known.label : pkg.split('.').pop();
};

export function apps(rec) {
  return [...BUILT_IN, ...Object.keys(rec.apps || {}).sort().map((pkg, index) => ({ package: pkg, label: labelOf(pkg), tint: (index + 2) % 6 }))];
}

// What can be touched right now: [{ id, text, cls, bounds: [left, top, right, bottom], ... }].
export function elements(rec) {
  const state = ui(rec);
  const out = [];
  const add = (id, text, cls, bounds, more = {}) => out.push({ id, text, cls, bounds, clickable: true, editable: false, focused: false, ...more });
  if (state.off) return out;
  // A pop-up covers everything else until it is answered.
  if (state.dialog) { add('dialog_dismiss', 'Not now', 'Button', [150, 470, 330, 526]); return out; }
  if (state.app === 'launcher') {
    add('search', state.text, 'EditText', [24, 250, 456, 302], { editable: true, focused: true, hint: 'Type to search' });
    apps(rec).slice(0, 12).forEach((app, index) => {
      const col = index % 4;
      const row = Math.floor(index / 4);
      add(`app_${app.package}`, app.label, 'TextView', [col * 120 + 12, 340 + row * 132, col * 120 + 108, 460 + row * 132], { package: app.package, tint: app.tint });
    });
  } else if (state.app === 'example.settings') {
    if (state.page === 'about') {
      add('back_row', 'Back to Settings', 'TextView', [0, 720, W, 780]);
    } else {
      add('wifi', 'Wi-Fi', 'Switch', [0, 96, W, 160], { checked: state.wifi });
      add('dark', 'Dark theme', 'Switch', [0, 160, W, 224], { checked: Boolean(rec.device.dark_mode) });
      add('animations', 'Animations', 'Switch', [0, 224, W, 288], { checked: Boolean(rec.device.animations) });
      add('about', 'About this device', 'TextView', [0, 288, W, 352]);
    }
  } else if (state.app === 'link') {
    add('close', 'Close', 'Button', [150, 560, 330, 616]);
  } else {
    add('new_note', 'New note', 'Button', [24, 96, 232, 148]);
    add('save', 'Save', 'Button', [248, 96, 456, 148]);
    add('title', state.text, 'EditText', [24, 168, 456, 224], { editable: true, focused: state.focus === 'title', hint: 'Tap, then type' });
  }
  if (state.app !== 'launcher') {
    add('nav_back', 'Back', 'ImageButton', [0, 800, 160, H]);
    add('nav_home', 'Home', 'ImageButton', [160, 800, 320, H]);
    add('nav_recents', 'Recents', 'ImageButton', [320, 800, W, H]);
  }
  return out;
}

export function elementAt(rec, px, py) {
  // The last match wins, so the navigation bar is checked after the content it overlaps.
  return elements(rec).filter((e) => px >= e.bounds[0] && px < e.bounds[2] && py >= e.bounds[1] && py < e.bounds[3]).pop() || null;
}

const changed = (state) => { state.rev += 1; };

export function launch(rec, pkg) {
  const state = ui(rec);
  if (!apps(rec).some((app) => app.package === pkg)) return false;
  Object.assign(state, { app: pkg, page: '', text: '', focus: '', off: false });
  changed(state);
  return true;
}

export function home(rec) {
  const state = ui(rec);
  Object.assign(state, { app: 'launcher', page: '', text: '', focus: '', off: false });
  changed(state);
}

function back(rec) {
  const state = ui(rec);
  if (state.app === 'example.settings' && state.page === 'about') { state.page = ''; changed(state); } else home(rec);
}

function saveLine(rec) {
  const state = ui(rec);
  if (!state.text.trim()) return;
  const list = state.lists[state.app] || [];
  state.lists[state.app] = [state.text.trim().slice(0, 40), ...list].slice(0, 6);
  state.text = '';
  state.focus = '';
}

// x and y are 0..1 of the screen. Returns the element that was hit, or null.
export function tap(rec, x, y) {
  const state = ui(rec);
  state.touch = { x, y, at: now() };
  if (state.off) { state.off = false; changed(state); return null; }
  const hit = elementAt(rec, x * W, y * H);
  if (hit) {
    if (hit.id === 'dialog_dismiss') state.dialog = '';
    else if (hit.package) launch(rec, hit.package);
    else if (hit.id === 'nav_back' || hit.id === 'back_row') back(rec);
    else if (hit.id === 'nav_home' || hit.id === 'nav_recents' || hit.id === 'close') home(rec);
    else if (hit.id === 'wifi') state.wifi = !state.wifi;
    else if (hit.id === 'dark') rec.device.dark_mode = !rec.device.dark_mode;
    else if (hit.id === 'animations') rec.device.animations = !rec.device.animations;
    else if (hit.id === 'about') state.page = 'about';
    else if (hit.id === 'new_note') { state.text = ''; state.focus = 'title'; } else if (hit.id === 'title') state.focus = 'title';
    else if (hit.id === 'save') saveLine(rec);
  }
  changed(state);
  return hit;
}

export function swipe(rec, x1, y1, x2, y2) {
  const state = ui(rec);
  state.touch = { x: x2, y: y2, at: now(), from: { x: x1, y: y1 } };
  // A swipe up from the bottom edge is "home", as on a phone.
  if (!state.off && y1 > 0.9 && y2 < 0.6) home(rec);
  changed(state);
}

export function type(rec, text) {
  const state = ui(rec);
  if (state.off) return;
  const takes = state.app === 'launcher' || state.focus === 'title' || !['example.settings', 'link'].includes(state.app);
  if (takes) {
    if (state.app !== 'launcher') state.focus = 'title';
    state.text = (state.text + String(text)).slice(-60);
  }
  changed(state);
}

export function key(rec, name) {
  const state = ui(rec);
  const which = String(name).toUpperCase();
  if (which === 'POWER') state.off = !state.off;
  else if (which === 'HOME' || which === 'APP_SWITCH') home(rec);
  else if (which === 'BACK') back(rec);
  else if (which === 'DEL' || which === 'FORWARD_DEL') state.text = state.text.slice(0, -1);
  else if (which === 'ENTER') { if (state.app === 'launcher') state.text = ''; else saveLine(rec); } else if (which === 'VOLUME_UP') state.volume = Math.min(15, state.volume + 1);
  else if (which === 'VOLUME_DOWN') state.volume = Math.max(0, state.volume - 1);
  changed(state);
}

export function openLink(rec, url) {
  const state = ui(rec);
  Object.assign(state, { app: 'link', page: '', text: '', focus: '', link: String(url).slice(0, 200), off: false });
  changed(state);
}

export function wake(rec) {
  const state = ui(rec);
  state.off = false;
  changed(state);
}

export function foreground(rec) {
  const state = ui(rec);
  if (state.app === 'launcher') return { package: 'example.launcher', activity: 'example.launcher.Home' };
  if (state.app === 'link') return { package: 'example.browser', activity: 'example.browser.View' };
  return { package: state.app, activity: `${state.app}.MainActivity` };
}

// A string that changes whenever the picture would: used to skip drawing the same frame.
export function frameKey(rec, t) {
  if (rec.phase !== 'booted') return `${rec.phase}:${Math.floor(t * 3)}`;
  const state = ui(rec);
  const touching = state.touch && t - state.touch.at < 0.7 ? Math.floor((t - state.touch.at) * 10) : '';
  return `${state.rev}:${rec.device.dark_mode}:${rec.device.font_scale}:${new Date(t * 1000).getMinutes()}:${touching}`;
}
