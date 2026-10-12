// Start-up: theme, session, navigation. Everything else lives in the page modules.

import { get, hooks, post, setCsrf, socketUrl } from './api.js';
import { capacityText, confirmOverCapacity } from './common.js';
import { initTasks, refresh as refreshTasks, stopTasks, track } from './components/tasks.js';
import { fail } from './components/toast.js';
import { $ } from './dom.js';
import * as android from './pages/android.js';
import * as androids from './pages/androids.js';
import * as apps from './pages/apps.js';
import * as automation from './pages/automation.js';
import * as backups from './pages/backups.js';
import * as control from './pages/control.js';
import * as dashboard from './pages/dashboard.js';
import * as media from './pages/media.js';
import * as network from './pages/network.js';
import * as settings from './pages/settings.js';
import * as system from './pages/system.js';
import { startRouter } from './router.js';

const ROUTES = {
  dashboard: dashboard.render, androids: androids.render, android: android.render, control: control.render,
  apps: apps.render, network: network.render, automation: automation.render, media: media.render,
  backups: backups.render, system: system.render, settings: settings.render,
};
// The Android detail page belongs to the "Androids" entry of the navigation.
const NAV_OF = { android: 'androids' };

// ------------------------------------------------------------------- theme
const THEME_KEY = 'sf-theme';
const THEMES = ['auto', 'light', 'dark'];

function applyTheme(theme) {
  if (theme === 'auto') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  $('#theme-toggle').textContent = `Theme: ${theme}`;
}

function initTheme() {
  let theme = 'auto';
  try { theme = localStorage.getItem(THEME_KEY) || 'auto'; } catch { /* follows the system */ }
  if (!THEMES.includes(theme)) theme = 'auto';
  applyTheme(theme);
  $('#theme-toggle').addEventListener('click', () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    try { localStorage.setItem(THEME_KEY, theme); } catch { /* not remembered, still applied */ }
    applyTheme(theme);
  });
}

// ----------------------------------------------------------------- session
let router = null;
let events = null;
let capacityTimer = 0;

function showLogin(message = '') {
  if (router) { router.stop(); router = null; }
  if (events) { events.close(); events = null; }
  clearInterval(capacityTimer);
  stopTasks();
  for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
  $('#app').hidden = true;
  $('#main').replaceChildren();          // nothing of the farm stays in the page after signing out
  $('#login').hidden = false;
  $('#login-error').textContent = message;
  $('#login-token').value = '';
  $('#login-token').focus();
}

async function capacityPill() {
  const pill = $('#capacity-pill');
  try {
    const capacity = await get('/api/system/capacity');
    if (!capacity.estimate || capacity.estimate.max_instances == null) { pill.hidden = true; return; }
    pill.hidden = false;
    pill.className = `pill ${capacity.over ? 'warn' : ''}`;
    pill.textContent = `${capacity.running} of ~${capacity.estimate.max_instances} running`;
    pill.title = `${capacityText(capacity)}${capacity.over ? `. ${(capacity.reasons || []).join('; ')}` : ''}`;
  } catch {
    pill.hidden = true;
  }
}

// Pushes from the daemon only make the next refresh happen now; polling stays the
// source of truth, so a dropped socket costs nothing but a few seconds.
function connectEvents() {
  let socket = null;
  let closed = false;
  let retry = 0;
  let pending = 0;
  const open = () => {
    if (closed) return;
    socket = new WebSocket(socketUrl('/api/events'));
    socket.addEventListener('message', () => {
      clearTimeout(pending);
      pending = setTimeout(() => { window.dispatchEvent(new CustomEvent('sf:changed')); refreshTasks(); }, 300);
    });
    socket.addEventListener('close', () => { if (!closed) retry = setTimeout(open, 5000); });
  };
  open();
  return { close() { closed = true; clearTimeout(retry); clearTimeout(pending); if (socket) socket.close(); } };
}

async function showApp() {
  $('#login').hidden = true;
  $('#app').hidden = false;
  get('/api/system').then((system) => {
    $('#farm-name').textContent = system.farm_name;
    document.title = system.farm_name;
  }).catch(() => {});
  initTasks();
  capacityPill();
  capacityTimer = setInterval(() => { if (!document.hidden) capacityPill(); }, 10000);
  events = connectEvents();
  const nav = $('#nav');
  router = startRouter($('#main'), ROUTES, 'dashboard', (name) => {
    const current = NAV_OF[name] || name;
    for (const link of nav.querySelectorAll('a')) {
      if (link.getAttribute('href') === `#/${current}`) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    nav.classList.remove('open');
    $('#nav-toggle').setAttribute('aria-expanded', 'false');
  });
}

function init() {
  initTheme();
  hooks.unauthorized = () => showLogin('Your session ended. Sign in again.');
  hooks.task = track;
  hooks.confirmOverCapacity = (error) => confirmOverCapacity(error.capacity, 1, error.message);

  $('#login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = event.target.querySelector('button');
    button.disabled = true;
    try {
      const session = await post('/api/auth/login', { token: $('#login-token').value.trim() });
      setCsrf(session.csrf);
      $('#login-token').value = '';
      await showApp();
    } catch (error) {
      $('#login-error').textContent = error.status === 401 ? 'That token is not correct.' : error.message;
    } finally {
      button.disabled = false;
    }
  });
  $('#logout').addEventListener('click', async () => {
    try { await post('/api/auth/logout'); } catch (error) { fail(error); }
    setCsrf('');
    showLogin();
  });
  $('#nav-toggle').addEventListener('click', () => {
    const open = $('#nav').classList.toggle('open');
    $('#nav-toggle').setAttribute('aria-expanded', String(open));
  });

  // A valid session cookie (a reload, a second tab) goes straight in.
  get('/api/auth/me').then((me) => { setCsrf(me.csrf); return showApp(); }, () => showLogin());
}

init();
