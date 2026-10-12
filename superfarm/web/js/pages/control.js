// Control: a wall of live views of the selected Androids. With "broadcast" on, one tap,
// swipe, key or typed text on any tile goes to every selected device; coordinates are
// normalized, so devices of different resolutions get the same gesture.

import { del, get, post } from '../api.js';
import { button, deviceActions, emptyState, h264Offered, linkButton, loadInstances, pageHead, panel, plural } from '../common.js';
import { deviceKeys, liveView } from '../components/live.js';
import { selectorBox } from '../components/table.js';
import { attempt, fail, ok } from '../components/toast.js';
import { h, parseSelector, replace, scope as newScope, selectorOf } from '../dom.js';
import { icon } from '../icons.js';
import { rememberInstances } from '../state.js';

const RUNNING = new Set(['running', 'booted']);

export async function render(root, params, scope) {
  let instances = await loadInstances();
  rememberInstances(instances);
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
  const chips = h('div', { class: 'chips', role: 'group', 'aria-label': 'Running Androids' });
  const target = h('span', { class: 'badge info', role: 'status' });
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
    }, on ? 'Stopped listening.' : `Listening to Android ${id} on the host’s speakers.`), `small${on ? ' primary' : ''}`, 'volume-up');
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
      replace(wall, instances.some((i) => RUNNING.has(i.state))
        ? emptyState('control', 'Nothing on the wall yet',
          'Pick the Androids to watch above, or type a selector such as "running" or "tag:eu". Each one you pick shows its live screen here.',
          button('Show all running', () => model.setSelection(instances.map((i) => i.id)), 'primary', 'control'))
        : emptyState('device', 'No Android is running', 'The wall shows live screens, and a stopped Android has no screen to show. Start some first.',
          linkButton('Go to Androids', '#/androids', 'primary', 'devices')));
    }
    wall.className = `wall ${large.checked ? 'large' : ''}`;
    replace(target, icon('control'), broadcast.checked ? `input goes to ${plural(selection.size, 'Android')}` : 'input goes to the tile you touch');
  }

  function drawChips() {
    const running = instances.filter((i) => RUNNING.has(i.state));
    replace(chips, running.map((i) => h('button', {
      type: 'button', class: 'chip', 'aria-pressed': String(selection.has(i.id)),
      onclick: () => {
        const next = new Set(selection);
        next[next.has(i.id) ? 'delete' : 'add'](i.id);
        model.setSelection(next);
      },
    }, h('span', { class: 'count' }, `#${i.id}`), i.name)));
    if (!running.length) replace(chips, h('span', { class: 'muted' }, 'No Android is running.'));
  }

  async function refreshListen() {
    listening = (await get('/api/listen')).listening;
    for (const [id, tile] of tiles) replace(tile.listen, listenButton(id));
  }

  model.onSelection(() => { drawChips(); drawWall(); });
  large.addEventListener('change', drawWall);
  broadcast.addEventListener('change', drawWall);
  scope.add(() => { for (const tile of tiles.values()) tile.scope.dispose(); });

  const nativeNote = windows && !windows.available.ok ? windows.available.reason : '';
  root.append(
    pageHead('Control', 'Watch and drive many Androids at once: with broadcast on, one tap, swipe or typed text goes to every Android on the wall.'),
    h('div', { class: 'stack' },
      panel('On the wall', { icon: 'devices', actions: [
        button('All running', () => model.setSelection(instances.map((i) => i.id)), 'small'), button('None', () => model.setSelection([]), 'small')] },
      h('div', { class: 'stack tight' },
        h('div', { class: 'toolbar' }, selectorBox(model, () => instances.filter((i) => RUNNING.has(i.state)))),
        chips)),
      panel('Broadcast', { icon: 'control', actions: target },
        h('div', { class: 'stack tight' },
          h('div', { class: 'row' },
            h('label', null, broadcast, ' Broadcast: my input on any tile goes to all selected'),
            h('label', null, large, ' Large tiles')),
          h('form', { class: 'row', onsubmit: (event) => {
            event.preventDefault();
            // Nothing to send is said, not swallowed.
            if (!selection.size) { fail(new Error('Select at least one Android first.')); return; }
            if (!typed.value) { typed.focus(); return; }
            send([...selection], 'text', { value: typed.value });
            typed.value = '';
          } }, typed, h('button', { type: 'submit', class: 'btn' }, icon('keyboard'), 'Type on all')),
          h('div', { class: 'row' },
            deviceKeys((name) => send([...selection], 'key', { name }), { label: 'Device buttons for every selected Android' }),
            h('span', { class: 'chip-sep', 'aria-hidden': 'true' }),
            h('span', { class: 'muted small' }, 'Setting on all selected:'), deviceActions(() => selectorOf(selection))),
          h('div', { class: 'row' },
            button('Open native windows (tiled)', () => attempt(async () => {
              if (!selection.size) throw new Error('Select at least one Android first.');
              await post('/api/windows/grid', { ids: [...selection] });
            }, 'Windows opened on the desktop of the machine that runs the daemon.'), 'small', 'open'),
            button('Close native windows', () => attempt(() => post('/api/windows/close-all'), 'Native windows closed.'), 'small'),
            nativeNote && h('span', { class: 'small muted' }, `Native windows are not available: ${nativeNote}`)),
          h('p', { class: 'small muted' }, 'Click a tile and type to send keys; drag to swipe; hold to long-press. "Listen" plays one device’s sound on the host’s speakers.'))),
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
      else { drawChips(); if (!selection.size) drawWall(); }
      await refreshListen();
    } catch { /* retried on the next turn */ }
  }, 5000);
  scope.add(() => clearInterval(timer));
}
