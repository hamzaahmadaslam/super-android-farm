// Control: a wall of live views of the selected Androids. With "broadcast" on, one tap,
// swipe, key or typed text on any tile goes to every selected device; coordinates are
// normalized, so devices of different resolutions get the same gesture.

import { del, get, post } from '../api.js';
import { button, deviceActions, empty, h264Offered, loadInstances, pageHead } from '../common.js';
import { liveView } from '../components/live.js';
import { selectorBox } from '../components/table.js';
import { attempt, fail, ok } from '../components/toast.js';
import { h, parseSelector, replace, scope as newScope, selectorOf } from '../dom.js';

const RUNNING = new Set(['running', 'booted']);
const NAV = [['Back', 'BACK'], ['Home', 'HOME'], ['Recents', 'APP_SWITCH'], ['Vol −', 'VOLUME_DOWN'], ['Vol +', 'VOLUME_UP'], ['Power', 'POWER']];

export async function render(root, params, scope) {
  let instances = await loadInstances();
  const [system, windows] = await Promise.all([get('/api/system').catch(() => ({})), get('/api/windows').catch(() => null)]);
  const h264 = h264Offered(system);
  let listening = null;

  // The selection, shaped like a table's so the selector box can drive it.
  const selection = new Set();
  const listeners = [];
  const model = {
    selection,
    setSelection(ids) {
      const running = new Set(instances.filter((i) => RUNNING.has(i.state)).map((i) => i.id));
      selection.clear();
      for (const id of ids) if (running.has(id)) selection.add(id);
      for (const fn of listeners) fn(selection);
    },
    onSelection(fn) { listeners.push(fn); },
  };

  const broadcast = h('input', { type: 'checkbox', checked: true });
  const large = h('input', { type: 'checkbox' });
  const chips = h('div', { class: 'row', role: 'group', 'aria-label': 'Running Androids' });
  const wall = h('div', { class: 'wall large' });
  const typed = h('input', { type: 'text', class: 'grow', placeholder: 'Text to type on every selected Android', 'aria-label': 'Text to type on every selected Android' });
  const tiles = new Map();            // id -> { el, scope, listen }

  // A failed request is reported once per burst, not once per device per gesture.
  let lastError = 0;
  const send = (ids, kind, payload) => {
    if (!ids.length) return;
    post(`/api/input/${kind}`, { ids, ...payload }).then((result) => {
      const failed = Object.entries(result.results || {}).filter(([, r]) => !r.ok);
      if (failed.length && Date.now() - lastError > 4000) {
        lastError = Date.now();
        fail(`Input did not reach ${failed.length} Android(s). Android ${failed[0][0]}: ${failed[0][1].detail || failed[0][1].error || 'failed'}`);
      }
    }).catch((error) => {
      if (Date.now() - lastError > 4000) { lastError = Date.now(); fail(error); }
    });
  };
  const targets = (from) => (broadcast.checked ? [...selection] : [from]);

  function listenButton(id) {
    const on = listening === id;
    return button(on ? 'Stop listening' : 'Listen', () => attempt(async () => {
      if (on) await del('/api/listen');
      else await post(`/api/listen/${id}`);
      await refreshListen();
    }, on ? 'Stopped listening.' : `Listening to Android ${id} on the host’s speakers.`), `small${on ? ' primary' : ''}`);
  }

  function drawWall() {
    for (const [id, tile] of tiles) {
      if (!selection.has(id)) { tile.scope.dispose(); tiles.delete(id); }
    }
    for (const id of selection) {
      if (tiles.has(id)) continue;
      const instance = instances.find((i) => i.id === id);
      const own = newScope();
      const listen = h('span');
      const el = h('div', { class: 'tile', dataset: { id: String(id) } },
        h('div', { class: 'tile-head' },
          h('a', { class: 'name', href: `#/android/${id}` }, instance.name), listen),
        liveView(id, own, {
          send: (kind, payload) => send(targets(id), kind, payload), h264, fps: 5, width: 360, keys: false, label: instance.name,
          aspect: instance.screen && [instance.screen.width, instance.screen.height],
        }));
      tiles.set(id, { el, scope: own, listen });
    }
    for (const [id, tile] of tiles) replace(tile.listen, listenButton(id));
    replace(wall, [...selection].sort((a, b) => a - b).map((id) => tiles.get(id).el));
    if (!selection.size) {
      replace(wall, empty('Nothing selected', 'Pick the Androids to watch above, or type a selector such as "running" or "tag:eu".'));
    }
    wall.className = `wall ${large.checked ? 'large' : ''}`;
  }

  function drawChips() {
    const running = instances.filter((i) => RUNNING.has(i.state));
    replace(chips, running.map((i) => h('button', {
      type: 'button', class: 'btn small', 'aria-pressed': String(selection.has(i.id)),
      onclick: () => {
        const next = new Set(selection);
        next[next.has(i.id) ? 'delete' : 'add'](i.id);
        model.setSelection(next);
      },
    }, `${i.id} · ${i.name}`)));
    if (!running.length) {
      replace(chips, empty('No Android is running', 'Start some first; a stopped Android has no screen to show.',
        h('a', { class: 'btn primary', href: '#/androids' }, 'Go to Androids')));
    }
  }

  async function refreshListen() {
    listening = (await get('/api/listen')).listening;
    for (const [id, tile] of tiles) replace(tile.listen, listenButton(id));
  }

  model.onSelection(() => { drawChips(); drawWall(); });
  large.addEventListener('change', drawWall);
  scope.add(() => { for (const tile of tiles.values()) tile.scope.dispose(); });

  const nativeNote = windows && !windows.available.ok ? windows.available.reason : '';
  root.append(
    pageHead('Control', 'Watch and drive many Androids at once.'),
    h('div', { class: 'stack' },
      h('div', { class: 'card stack' },
        h('div', { class: 'toolbar' }, selectorBox(model, () => instances.filter((i) => RUNNING.has(i.state))),
          button('All running', () => model.setSelection(instances.map((i) => i.id))), button('None', () => model.setSelection([]))),
        chips),
      h('div', { class: 'card stack' },
        h('div', { class: 'row' },
          h('label', null, broadcast, ' Broadcast: my input on any tile goes to all selected'),
          h('label', null, large, ' Large tiles')),
        h('form', { class: 'row', onsubmit: (event) => {
          event.preventDefault();
          if (typed.value) send([...selection], 'text', { value: typed.value });
          typed.value = '';
        } }, typed, h('button', { type: 'submit', class: 'btn' }, 'Type on all')),
        h('div', { class: 'row', role: 'group', 'aria-label': 'Device buttons for every selected Android' },
          NAV.map(([label, name]) => button(label, () => send([...selection], 'key', { name }), 'small'))),
        h('div', { class: 'row' }, h('span', { class: 'muted' }, 'Setting on all selected:'), deviceActions(() => selectorOf(selection))),
        h('div', { class: 'row' },
          button('Open native windows (tiled)', () => attempt(async () => {
            if (!selection.size) throw new Error('Select at least one Android first.');
            await post('/api/windows/grid', { ids: [...selection] });
          }, 'Windows opened on the desktop of the machine that runs the daemon.'), 'small'),
          button('Close native windows', () => attempt(() => post('/api/windows/close-all'), 'Closed.'), 'small'),
          nativeNote && h('span', { class: 'small muted' }, `Native windows are not available: ${nativeNote}`)),
        h('p', { class: 'small muted' }, 'Click a tile and type to send keys; drag to swipe; hold to long-press. "Listen" plays one device’s sound on the host’s speakers.')),
      wall));

  drawChips();
  if (params[0]) {
    try { model.setSelection(parseSelector(params[0], instances)); } catch { ok('That selector was not understood; pick Androids below.'); }
  } else {
    drawWall();
  }
  refreshListen().catch(() => {});
  const timer = setInterval(async () => {
    if (document.hidden) return;
    try {
      instances = await loadInstances();
      // An Android that stopped leaves the wall; nothing is added behind the operator's back.
      const running = new Set(instances.filter((i) => RUNNING.has(i.state)).map((i) => i.id));
      if ([...selection].some((id) => !running.has(id))) model.setSelection([...selection].filter((id) => running.has(id)));
      else drawChips();
      await refreshListen();
    } catch { /* retried on the next turn */ }
  }, 5000);
  scope.add(() => clearInterval(timer));
}
