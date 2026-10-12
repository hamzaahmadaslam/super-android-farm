// The first-run guide on the dashboard: a short checklist from an empty machine to an
// Android running an app. Every step is read from the farm's real state (host checks,
// images, Androids, the app library); nothing is ticked because a button was pressed.
// It is a card, never a wizard: it can be hidden, and it goes away by itself once
// every step is done.

import { get } from '../api.js';
import { button, linkButton, UP } from '../common.js';
import { ok } from '../components/toast.js';
import { copyText, h } from '../dom.js';
import { icon } from '../icons.js';
import { getPref, setPref } from '../prefs.js';

const HIDDEN = 'guide-hidden';
const clip = (words, most = 220) => (words.length > most ? `${words.slice(0, most)}…` : words);

export const guideHidden = () => Boolean(getPref(HIDDEN, false));
export const showGuideAgain = () => setPref(HIDDEN, null);

// What the steps need, read together. Each part may fail on its own without hiding the rest.
export async function loadGuideState() {
  const safe = (promise, fallback) => promise.catch(() => fallback);
  const [doctor, images, config, library] = await Promise.all([
    safe(get('/api/system/doctor'), null), safe(get('/api/images'), null),
    safe(get('/api/config'), null), safe(get('/api/apps/library'), null),
  ]);
  return { doctor, images, config, library };
}

function steps(state, instances) {
  const checks = state.doctor ? state.doctor.checks : [];
  const failing = checks.filter((check) => check.status === 'fail');
  const warnings = checks.filter((check) => check.status === 'warn').length;
  const version = state.config ? state.config.config.android.default_version : null;
  const image = state.images && version ? state.images.images.find((entry) => String(entry.version) === String(version)) : null;
  const first = instances[0];
  const up = instances.filter((i) => UP.has(i.state));
  const apps = state.library ? state.library.apps.length : 0;
  return [
    {
      done: Boolean(state.doctor) && failing.length === 0,
      title: 'This host can run Androids',
      text: !state.doctor ? 'The host checks could not be read.'
        : failing.length ? `${failing.length} host check(s) fail. Each has a command that fixes it:`
          : `Every required host check passes${warnings ? `; ${warnings} warning(s) are worth a look` : ''}.`,
      detail: failing.slice(0, 3).map((check) => h('div', null,
        h('strong', null, check.title), ' ', h('span', { class: 'muted' }, clip(check.detail)),
        check.fix && h('div', { class: 'fix' }, h('pre', null, check.fix),
          button('Copy', () => copyText(check.fix).then(() => ok('Command copied.'), () => {}), 'small', 'copy')))),
      go: (kind) => linkButton(failing.length ? 'All host checks' : 'Host checks', '#/system/checks', kind),
    },
    {
      done: Boolean(image && image.built),
      title: version ? `The Android ${version} image is on this machine` : 'The default Android image is on this machine',
      text: !image ? 'The image list could not be read.'
        : image.built ? `${image.tag} is ready${image.stale ? ` (stale: ${image.stale_reason})` : ''}.`
          : `Android ${version} is the version new Androids get. "Prepare" pulls the base image if needed and builds the farm image; it also happens automatically at the first start.`,
      go: (kind) => linkButton(image && image.built ? 'Images' : 'Prepare the image', '#/system/images', kind),
    },
    {
      done: instances.length > 0,
      title: 'Create your first Android',
      text: instances.length ? `${instances.length} defined.` : 'An Android is one container with its own data, network and identity.',
      go: (kind) => linkButton('New Android', '#/androids/new', kind, 'plus'),
    },
    {
      done: up.length > 0,
      title: 'Start it',
      text: up.length ? `${up.length} running.` : first ? `${first.name} is ${first.state}. Open it and press Start; the first start takes the longest.` : 'Once an Android exists, start it to see and control its screen.',
      go: (kind) => (first ? linkButton(up.length ? 'Open Androids' : `Open ${first.name}`, up.length ? '#/androids' : `#/android/${first.id}`, kind) : null),
    },
    {
      done: apps > 0,
      title: 'Add an app',
      text: apps ? `${apps} in the library, ready to install on any selection.` : 'Upload an APK, XAPK, APKS or APKM once, then install it on any selection of Androids.',
      go: (kind) => linkButton('Upload an app', '#/apps/upload', kind, 'upload'),
    },
  ];
}

// The card, or null when there is nothing left to guide (or it was hidden).
export function guideCard(state, instances, onHide) {
  if (guideHidden() || !state) return null;
  const list = steps(state, instances);
  const done = list.filter((step) => step.done).length;
  if (done === list.length) return null;
  const next = list.findIndex((step) => !step.done);
  return h('section', { class: 'card guide', 'aria-labelledby': 'guide-title' },
    h('div', { class: 'card-head' },
      h('h2', { id: 'guide-title' }, icon('flag'), 'Get this farm running'),
      h('div', { class: 'guide-progress' },
        h('progress', { max: list.length, value: done, 'aria-label': `${done} of ${list.length} steps done` }),
        h('span', { class: 'small muted nowrap' }, `${done} of ${list.length} done`)),
      h('button', {
        type: 'button', class: 'btn ghost small', title: 'Hide this guide. "Show the setup guide" in the command palette brings it back.',
        onclick: () => { setPref(HIDDEN, true); onHide(); },
      }, icon('x'), 'Hide')),
    h('ol', { class: 'guide-steps' }, list.map((step, index) => h('li', { class: `guide-step ${step.done ? 'done' : index === next ? 'next' : ''}` },
      h('span', { class: 'mark', 'aria-hidden': 'true' }, step.done ? icon('check') : String(index + 1)),
      h('div', { class: 'what' },
        h('strong', null, step.title, h('span', { class: `badge ${step.done ? 'ok' : ''}` }, icon(step.done ? 'check' : 'dot'), step.done ? 'done' : 'not done')),
        h('p', null, step.text),
        !step.done && step.detail),
      // Only the step to do next carries the accent.
      h('div', { class: 'go' }, step.go(index === next ? 'primary' : ''))))));
}
