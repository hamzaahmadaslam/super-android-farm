// Start-up: theme, session, the shell (sidebar, top bar, command palette, shortcuts)
// and navigation. Everything else lives in the page modules.

import { get, hooks, openSocket, post, setCsrf } from './api.js';
import { ingestApprovals, refreshApprovals, startApprovals, stopApprovals, subscribe as onApprovals } from './approvals.js';
import { androidsText, capacityText, confirmOverCapacity, fleetAction, plural, UP } from './common.js';
import { confirmDialog } from './components/dialog.js';
import { initPalette, openPalette } from './components/palette.js';
import { closePopover } from './components/popover.js';
import { initShortcuts, showShortcuts } from './components/shortcuts.js';
import { initTasks, refresh as refreshTasks, stopTasks, toggle as toggleTasks, track } from './components/tasks.js';
import { fail, warn } from './components/toast.js';
import { $, actionHooks, busy, selectorOf } from './dom.js';
import { icon } from './icons.js';
import * as access from './pages/access.js';
import * as android from './pages/android.js';
import * as androids from './pages/androids.js';
import * as approvals from './pages/approvals.js';
import * as assistant from './pages/assistant.js';
import * as apps from './pages/apps.js';
import * as automation from './pages/automation.js';
import * as backups from './pages/backups.js';
import * as control from './pages/control.js';
import * as dashboard from './pages/dashboard.js';
import { guideHidden, showGuideAgain } from './pages/guide.js';
import * as media from './pages/media.js';
import * as network from './pages/network.js';
import * as pilot from './pages/pilot.js';
import * as settings from './pages/settings.js';
import * as superControl from './pages/super.js';
import * as system from './pages/system.js';
import { getPref, setPref } from './prefs.js';
import { go, startRouter } from './router.js';
import { changed, fleet } from './state.js';

const ROUTES = {
  dashboard: dashboard.render, androids: androids.render, android: android.render, control: control.render,
  apps: apps.render, network: network.render, automation: automation.render, media: media.render,
  backups: backups.render, system: system.render, settings: settings.render,
  approvals: approvals.render, access: access.render, assistant: assistant.render, pilot: pilot.render,
  super: superControl.render,
};
// The Android detail page belongs to the "Androids" entry of the navigation.
const NAV_OF = { android: 'androids' };
const TITLES = {
  dashboard: 'Dashboard', androids: 'Androids', android: 'Android', control: 'Control', apps: 'Apps', network: 'Network',
  automation: 'Automation', media: 'Media', backups: 'Backups', system: 'System', settings: 'Settings',
  approvals: 'Approvals', access: 'AI access', assistant: 'Assistant', pilot: 'AI Pilot', super: 'Super Control',
};
let currentRoute = '';
let farmName = 'Super Farm';

// ------------------------------------------------------------------- theme
const THEMES = ['auto', 'dark', 'light'];
const THEME_ICON = { auto: 'monitor', dark: 'moon', light: 'sun' };
const THEME_NAME = { auto: 'follows the system', dark: 'dark', light: 'light' };
let theme = 'auto';

function applyTheme() {
  if (theme === 'auto') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  const next = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  const toggle = $('#theme-toggle');
  toggle.replaceChildren(icon(THEME_ICON[theme]));
  toggle.setAttribute('aria-label', `Theme: ${THEME_NAME[theme]}. Switch to ${THEME_NAME[next]}`);
  toggle.title = `Theme: ${THEME_NAME[theme]}`;
}

function nextTheme() {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  setPref('theme', theme);
  applyTheme();
}

function initTheme() {
  theme = getPref('theme', 'auto');
  if (!THEMES.includes(theme)) theme = 'auto';
  applyTheme();
  $('#theme-toggle').addEventListener('click', nextTheme);
}

// ------------------------------------------------------------------- shell
function initShell() {
  // Icons are drawn here so that index.html stays plain markup.
  for (const node of document.querySelectorAll('[data-icon]')) node.prepend(icon(node.dataset.icon));
  for (const link of document.querySelectorAll('#nav a[data-key]')) {
    link.append(Object.assign(document.createElement('span'), { className: 'nav-key', textContent: `g ${link.dataset.key}` }));
    link.querySelector('.nav-key').setAttribute('aria-hidden', 'true');
    link.setAttribute('aria-keyshortcuts', `g ${link.dataset.key}`);
  }
  if (/Mac|iPhone|iPad/.test(navigator.platform || '')) $('#palette-key').textContent = '⌘ K';

  const app = $('#app');
  const sidebar = $('#sidebar');
  const collapse = $('#nav-collapse');
  const setRail = (on) => {
    app.classList.toggle('rail', on);
    collapse.setAttribute('aria-pressed', String(on));
    collapse.setAttribute('aria-label', on ? 'Expand the sidebar' : 'Collapse the sidebar');
    collapse.title = on ? 'Expand the sidebar ( [ )' : 'Collapse the sidebar ( [ )';
    // In the rail a link shows only its icon: the name moves to the tooltip.
    for (const link of document.querySelectorAll('#nav a')) link.title = on ? link.querySelector('.nav-label').textContent : '';
  };
  setRail(Boolean(getPref('rail', false)));
  const toggleRail = () => { const on = !app.classList.contains('rail'); setPref('rail', on); setRail(on); };
  collapse.addEventListener('click', toggleRail);

  const scrim = $('#nav-scrim');
  const menu = $('#nav-toggle');
  const sheet = (open) => {
    sidebar.classList.toggle('open', open);
    scrim.hidden = !open;
    menu.setAttribute('aria-expanded', String(open));
    if (open) sidebar.querySelector('a[aria-current="page"], a').focus();
  };
  menu.addEventListener('click', () => sheet(!sidebar.classList.contains('open')));
  scrim.addEventListener('click', () => sheet(false));
  sidebar.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && sidebar.classList.contains('open')) { sheet(false); menu.focus(); }
  });

  $('#palette-open').addEventListener('click', () => openPalette(go));
  $('#help-button').addEventListener('click', showShortcuts);
  return { closeSheet: () => sheet(false), toggleRail };
}

function commands() {
  const selected = [...fleet.selection];
  const stopped = fleet.instances.filter((i) => !UP.has(i.state)).map((i) => i.id);
  const running = fleet.instances.filter((i) => UP.has(i.state)).map((i) => i.id);
  const act = (action, ids) => fleetAction(action, ids, fleet.instances).then(changed);
  // Acting on "all" from a search box deserves one look at what "all" is.
  const actAll = async (action, ids, verb) => {
    if (ids.length > 1 && !await confirmDialog({
      title: `${verb} ${plural(ids.length, 'Android')}?`, message: `${androidsText(ids)}.`, confirm: `${verb} ${ids.length}`, icon: action === 'start' ? 'play' : 'stop',
    })) return;
    await act(action, ids);
  };
  const actions = [
    { title: 'New Android', hint: 'create one or many', icon: 'plus', keywords: 'create add instance', run: () => go('androids/new') },
    selected.length && { title: `Start the selection (${selected.length})`, hint: androidsText(selected), icon: 'play', keywords: 'boot selected', boost: 5, run: () => act('start', selected) },
    selected.length && { title: `Stop the selection (${selected.length})`, hint: androidsText(selected), icon: 'stop', keywords: 'halt selected', boost: 5, run: () => act('stop', selected) },
    stopped.length && { title: `Start all stopped Androids (${stopped.length})`, icon: 'play', keywords: 'boot everything', run: () => actAll('start', stopped, 'Start') },
    running.length && { title: `Stop all running Androids (${running.length})`, icon: 'stop', keywords: 'halt everything', run: () => actAll('stop', running, 'Stop') },
    { title: 'Open the control wall', hint: selected.length ? `for the selection (${selected.length})` : 'for every running Android', icon: 'control', keywords: 'broadcast watch live wall',
      run: () => go(`control/${selected.length ? selectorOf(selected) : 'running'}`) },
    { title: 'Upload an app', hint: 'APK, XAPK, APKS or APKM', icon: 'upload', keywords: 'install apk library add', run: () => go('apps/upload') },
    { title: 'Run doctor', hint: 'check this host again', icon: 'shield', keywords: 'host checks health diagnose', run: () => go('system/checks/run') },
    { title: 'Show tasks', icon: 'tasks', keywords: 'progress background jobs', run: () => toggleTasks(true) },
    { title: 'Switch theme', hint: `now: ${THEME_NAME[theme]}`, icon: THEME_ICON[theme], keywords: 'dark light appearance', run: nextTheme },
    { title: 'Keyboard shortcuts', icon: 'keyboard', keywords: 'help keys cheat sheet', run: showShortcuts },
    guideHidden() && { title: 'Show the setup guide', hint: 'first steps on the dashboard', icon: 'flag', keywords: 'first run onboarding checklist getting started',
      run: () => { showGuideAgain(); go('dashboard'); window.dispatchEvent(new CustomEvent('sf:guide')); } },
  ].filter(Boolean).map((entry) => ({ group: 'Actions', ...entry }));
  const pages = [...document.querySelectorAll('#nav a')].map((link) => ({
    group: 'Go to', title: link.querySelector('.nav-label').textContent, hint: `g ${link.dataset.key}`, icon: link.dataset.icon,
    run: () => { location.hash = link.getAttribute('href'); },
  }));
  const deep = [
    ['Host checks', 'system/checks', 'shield', 'doctor'], ['Android images', 'system/images', 'layers', 'pull build prepare'],
    ['Automation runs', 'automation/runs', 'automation', ''], ['Automation flows', 'automation/flows', 'automation', 'yaml'],
    ['Automation values and pools', 'automation/values', 'automation', ''], ['Automation schedules', 'automation/schedules', 'clock', 'cron'],
  ].map(([title, path, name, keywords]) => ({ group: 'Go to', title, icon: name, keywords, run: () => go(path) }));
  return [...actions, ...pages, ...deep];
}

// ----------------------------------------------------------------- session
let router = null;
let events = null;
let capacityTimer = 0;
let shell = null;

function showLogin(message = '') {
  if (router) { router.stop(); router = null; }
  if (events) { events.close(); events = null; }
  clearInterval(capacityTimer);
  stopTasks();
  stopApprovals();
  closePopover();
  for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
  $('#app').hidden = true;
  $('#main').replaceChildren();          // nothing of the farm stays in the page after signing out
  $('#login').hidden = false;
  $('#login-error').textContent = message;
  $('#login-token').value = '';
  $('#login-token').focus();
  document.title = farmName;
}

async function capacityPill() {
  const pill = $('#capacity-pill');
  try {
    const capacity = await get('/api/system/capacity');
    if (!capacity.estimate || capacity.estimate.max_instances == null) { pill.hidden = true; return; }
    pill.hidden = false;
    pill.classList.toggle('warn', Boolean(capacity.over));
    $('#capacity-count').textContent = `${capacity.running} / ~${capacity.estimate.max_instances}`;
    const words = `${capacityText(capacity)}${capacity.over ? `. ${(capacity.reasons || []).join('; ')}` : ''}`;
    pill.title = words;
    pill.setAttribute('aria-label', words);
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
    socket = openSocket('/api/events');
    socket.addEventListener('message', (event) => {
      // An approval request that was created or decided travels in the message itself.
      try {
        const data = JSON.parse(event.data);
        if (data && data.approvals) ingestApprovals(data.approvals);
        if (data && data.type === 'resync') refreshApprovals();
      } catch { /* not JSON: still a reason to refresh */ }
      clearTimeout(pending);
      pending = setTimeout(() => { changed(); refreshTasks(); capacityPill(); }, 300);
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
    farmName = system.farm_name;
    $('#farm-name').textContent = farmName;
    document.title = document.title.includes(' · ') ? `${document.title.split(' · ')[0]} · ${farmName}` : farmName;
  }).catch(() => {});
  initTasks();
  // A request that arrives while another page is open is announced; the toast only leads
  // to the inbox, it never answers anything.
  startApprovals((arrived) => {
    if (currentRoute === 'approvals') return;
    // On the Assistant page its own requests are asked in the transcript already.
    const fresh = currentRoute === 'assistant' ? arrived.filter((item) => !String(item.caller).startsWith('assistant:')) : arrived;
    if (!fresh.length) return;
    const first = fresh[0];
    warn(fresh.length === 1 ? `${first.caller} asks for approval: ${first.summary}` : `${fresh.length} requests from AI assistants are waiting for your approval.`,
      { action: { label: 'Review', run: () => go('approvals') }, timeout: 20000 });
  });
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
    currentRoute = name;
    document.title = `${TITLES[name] || 'Super Farm'} · ${farmName}`;
    // Back, Forward or a link: nothing that belonged to the page left behind stays open.
    closePopover();
    for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
    shell.closeSheet();
  });
}

function init() {
  initTheme();
  shell = initShell();
  initPalette(commands);
  initShortcuts({
    go,
    palette: () => { if (!$('#app').hidden) openPalette(go); },
    pages: Object.fromEntries([...document.querySelectorAll('#nav a[data-key]')].map((link) => [
      link.dataset.key, [link.getAttribute('href').slice(2), link.querySelector('.nav-label').textContent]])),
    global: [
      { key: '[', label: 'Collapse or expand the sidebar', run: () => shell.toggleRail() },
      { key: 'T', label: 'Show or hide the task list', run: () => { if (!$('#app').hidden) toggleTasks(); } },
    ],
  });
  // Losing the daemon is said once, in a banner, and mended without a reload: while it
  // is away a light probe runs, and when it answers again every page reads afresh.
  const dot = $('#link-dot');
  const notice = $('#offline');
  let probe = 0;
  const check = () => get('/api/health').catch(() => {});
  hooks.link = (up) => {
    if (dot.classList.contains('off') !== up) return;
    dot.classList.toggle('off', !up);
    dot.setAttribute('aria-label', up ? 'Connected to the farm daemon' : 'The farm daemon does not answer');
    dot.title = up ? 'Connected to the farm daemon' : 'The farm daemon does not answer';
    notice.hidden = up;
    clearInterval(probe);
    if (!up) {
      probe = setInterval(check, 2500);
    } else if (router) {
      if ($('#main .page-error')) router.refresh();
      else changed();
      refreshTasks();
      capacityPill();
    }
  };
  $('#offline-retry').addEventListener('click', (event) => busy(event.currentTarget, check));
  // The inbox indicator: how many requests wait. It pulses (not under reduced motion)
  // and says the number in words for assistive technology.
  onApprovals((state) => {
    const link = $('#approvals-link');
    for (const dot of [$('#approvals-count'), $('#nav-approvals')]) {
      dot.hidden = state.count === 0;
      dot.textContent = String(state.count);
    }
    link.classList.toggle('attention', state.count > 0);
    const words = state.count ? `Approvals: ${state.count} waiting for your answer` : 'Approvals: nothing waiting';
    link.setAttribute('aria-label', words);
    link.title = words;
  });
  hooks.unauthorized = () => showLogin('Your session ended. Sign in again.');
  hooks.task = track;
  hooks.confirmOverCapacity = (error) => confirmOverCapacity(error.capacity, 1, error.message);

  actionHooks.error = fail;
  // These two are in index.html, not built with h(): the same guard, applied by hand.
  $('#login-form').addEventListener('submit', (event) => {
    event.preventDefault();
    busy(event.target, async () => {
      try {
        const session = await post('/api/auth/login', { token: $('#login-token').value.trim() });
        setCsrf(session.csrf);
        $('#login-token').value = '';
        await showApp();
      } catch (error) {
        $('#login-error').textContent = error.status === 401 ? 'That token is not correct.' : error.message;
      }
    });
  });
  $('#logout').addEventListener('click', (event) => busy(event.currentTarget, async () => {
    try { await post('/api/auth/logout'); } catch (error) { fail(error); }
    setCsrf('');
    showLogin();
  }));

  // A valid session cookie (a reload, a second tab) goes straight in.
  get('/api/auth/me').then((me) => { setCsrf(me.csrf); return showApp(); }, () => showLogin());
}

init();
