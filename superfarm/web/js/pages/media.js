// Media: feed a video file or a live stream to the virtual camera of any selection,
// control microphones, and listen to one device on the host's speakers.

import { del, get, post, upload } from '../api.js';
import { badge, button, card, emptyState, field, loadInstances, option, pageHead } from '../common.js';
import { dataTable } from '../components/table.js';
import { attempt } from '../components/toast.js';
import { bytes, h, replace, when } from '../dom.js';
import { icon } from '../icons.js';

const RUNNING = new Set(['running', 'booted']);

export async function render(root, _params, scope) {
  const selector = h('input', { type: 'text', value: 'running', spellcheck: false });
  const source = h('select');
  const sound = h('select');
  const file = h('input', { type: 'file', accept: 'video/*,audio/*', 'aria-label': 'Video or sound file' });
  const availability = h('div');
  const feeds = h('div');
  const listenBox = h('div', { class: 'row' });
  const listenPick = h('select', { 'aria-label': 'Android to listen to' });
  const targets = () => {
    if (!selector.value.trim()) throw new Error('Type a selector first (for example: running).');
    return { selector: selector.value.trim() };
  };

  const sources = dataTable({
    label: 'Media library',
    key: (s) => s.id,
    columns: [
      { title: 'Source', cell: (s) => s.name },
      { title: 'Kind', cell: (s) => s.kind },
      { title: 'Size', class: 'num', cell: (s) => (s.size ? bytes(s.size) : '') },
      { title: 'Details', cell: (s) => (s.kind === 'live' ? [`Send your stream to ${s.server} with key `, h('code', null, s.key), s.active ? [' ', badge('ok', 'receiving')] : ''] : when(s.modified)) },
    ],
    empty: emptyState('media', 'No media yet', 'Upload a video or sound file above; it can then be played to the camera of any selection.'),
  });

  async function loadSources() {
    const list = (await get('/api/camera/sources')).sources;
    sources.update(list);
    const keep = [source.value, sound.value];
    replace(source, list.filter((s) => s.kind !== 'audio').map((s) => option(s.id, s.name)));
    replace(sound, option('source', 'Sound of the source'), option('none', 'No sound'), list.filter((s) => s.kind !== 'live').map((s) => option(s.id, `Sound of ${s.name}`)));
    if (keep[0]) source.value = keep[0];
    if (keep[1]) sound.value = keep[1];
  }

  async function loadCamera() {
    const data = await get('/api/camera');
    const a = data.available;
    replace(availability, a.ok
      ? h('div', { class: 'banner ok' }, icon('check'), h('div', { class: 'grow' }, `Cameras are ready: ${a.devices.usable.length} of ${a.devices.slots} devices usable.`))
      : h('div', { class: 'banner warn' }, icon('alert'), h('div', { class: 'grow' }, h('strong', null, 'Cameras cannot work on this host yet.'),
        h('ul', null, a.reasons.map((reason) => h('li', null, reason))))));
    const list = data.status.feeds || [];
    replace(feeds, list.length === 0 ? h('p', { class: 'muted' }, 'Nothing is playing.')
      : h('ul', null, list.map((feed) => h('li', null, badge(feed.running ? 'ok' : 'fail', feed.running ? 'playing' : 'stopped'), ' ',
        h('strong', null, feed.source), ` → Android ${(feed.ids || []).join(', ')}`, feed.error ? h('div', { class: 'small fail' }, feed.error) : null))));
  }

  async function loadListen() {
    const [status, instances] = await Promise.all([get('/api/listen'), loadInstances()]);
    const running = instances.filter((i) => RUNNING.has(i.state));
    const keep = listenPick.value;
    replace(listenPick, running.map((i) => option(String(i.id), `${i.id} · ${i.name}`)));
    if (keep) listenPick.value = keep;
    replace(listenBox,
      status.listening != null ? [badge('ok', `Listening to Android ${status.listening}`),
        button('Stop', () => attempt(() => del('/api/listen'), 'Stopped listening.').then(loadListen), 'small')]
        : h('span', { class: 'muted' }, 'Not listening to any device.'),
      running.length ? [listenPick, button('Listen', () => attempt(() => post(`/api/listen/${listenPick.value}`), 'Listening on the host’s speakers.').then(loadListen), 'small primary')]
        : h('span', { class: 'muted' }, 'Start an Android to listen to it.'));
  }

  const cameraAction = (action, body, message) => attempt(async () => {
    const result = await post(`/api/camera/${action}`, { ...targets(), ...body });
    await loadCamera();
    return result;
  }, message);

  root.append(
    pageHead('Media', 'A virtual front camera and microphone for any Android, fed from a file or a live stream.'),
    h('div', { class: 'stack' },
      availability,
      card('Play to cameras',
        h('div', { class: 'fields' },
          field('Androids (selector)', selector), field('Picture', source), field('Sound', sound)),
        h('div', { class: 'row' },
          button('Play', () => cameraAction('play', { source: source.value, sound: sound.value }, 'Playing.'), 'primary', 'play'),
          button('Stop', () => cameraAction('stop', {}, 'Stopped.'), '', 'stop'),
          button('Mute microphones', () => cameraAction('mic', { mode: 'mute' }, 'Muted.')),
          button('Unmute', () => cameraAction('mic', { mode: 'unmute' }, 'Unmuted.')),
          button('Re-attach cameras', () => cameraAction('attach', {}, 'Cameras attached.')),
          button('Install the camera driver', () => cameraAction('install-hal', {}, 'Driver installed; restart those Androids to load it.'))),
        h('h3', null, 'Playing now'), feeds),
      card('Media library',
        h('form', { class: 'toolbar', onsubmit: (event) => {
          event.preventDefault();
          return attempt(async () => {
            if (!file.files.length) throw new Error('Choose a video or sound file first.');
            await upload('/api/media', file.files[0]);
            file.value = '';
            await loadSources();
          }, 'Added to the library.');
        } }, file, h('button', { type: 'submit', class: 'btn primary' }, icon('upload'), 'Upload')),
        sources.el),
      card('Listen to one device',
        h('p', { class: 'small muted' }, 'Plays that Android’s sound on the speakers of the machine running the daemon. One device at a time.'),
        listenBox)));

  await Promise.all([loadSources(), loadCamera(), loadListen()]);
  scope.every(8000, () => Promise.all([loadCamera(), loadListen()])).catch(() => {});
}
