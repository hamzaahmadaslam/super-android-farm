// Contextual help: a small "?" next to a control that is not obvious. The words are
// the farm's own: each topic names config fields, and what the popover shows is the
// description the daemon publishes for them (GET /api/config/schema), so the help can
// never drift from what the option does. A topic may add one fixed sentence where no
// config field describes the thing (the selector grammar, which the panel itself parses).

import { get } from '../api.js';
import { h } from '../dom.js';
import { icon } from '../icons.js';
import { openPopover } from './popover.js';

const TOPICS = {
  rendering: { title: 'Rendering mode', paths: ['gpu.mode'] },
  'rendering-prefer': { title: 'Which GPU "auto" takes', paths: ['gpu.prefer', 'gpu.assignment'] },
  network: { title: 'Network mode', paths: ['network.mode'] },
  killswitch: { title: 'Kill switch', paths: ['network.killswitch', 'network.dns_leak_protection'] },
  capacity: { title: 'Capacity estimate', paths: ['performance.enforce_capacity', 'performance.reserve_host_mb', 'performance.memory_guard_mb'] },
  selector: {
    title: 'Selectors',
    text: ['A selector names a set of Androids: all, running, stopped, tag:name, one id (5) or a range (1-10).',
      'Join several with commas; a leading ! takes Androids out again, as in "running,!tag:spare".'],
  },
  fps: { title: 'Frames per second', paths: ['android.fps'] },
  'device-profile': { title: 'Device profile', paths: ['device.profile'] },
  trim: { title: 'Trimmed system apps', paths: ['performance.trim_packages'] },
  'backup-stop': { title: 'Backing up a running Android', paths: ['backup.stop_for_backup', 'backup.auto_before_risky'] },
  'start-pace': { title: 'Starting many at once', paths: ['performance.start_concurrency', 'performance.start_stagger_seconds'] },
};

let schema = null;
const loadSchema = () => {
  if (!schema) schema = get('/api/config/schema').catch((error) => { schema = null; throw error; });
  return schema;
};

// The schema node for "gpu.mode": follow properties, resolving $ref / allOf / anyOf.
function describe(root, path) {
  let node = root;
  for (const key of path.split('.')) {
    for (let guard = 0; node && guard < 8; guard++) {
      if (node.$ref) node = node.$ref.replace(/^#\//, '').split('/').reduce((at, part) => at && at[part], root);
      else if (node.allOf && node.allOf.length === 1) node = node.allOf[0];
      else if (node.anyOf) node = node.anyOf.find((option) => option.type !== 'null');
      else break;
    }
    node = node && node.properties ? node.properties[key] : undefined;
    if (!node) return '';
  }
  return node.description || '';
}

// The daemon's own description of one config field ("ai.policy"), or '' when it has none.
export async function describeConfig(path) {
  return describe(await loadSchema(), path);
}

async function content(topic) {
  const lines = [...(topic.text || [])];
  if (topic.paths) {
    const root = await loadSchema();
    for (const path of topic.paths) {
      const text = describe(root, path);
      if (text) lines.push([h('code', null, path), ' ', text]);
    }
  }
  return lines;
}

// The "?" button for one topic. Unknown topics render nothing rather than an empty box.
export function help(name) {
  const topic = TOPICS[name];
  if (!topic) return null;
  const button = h('button', {
    type: 'button', class: 'help-btn', 'aria-label': `Help: ${topic.title}`, 'aria-expanded': 'false', 'aria-haspopup': 'dialog',
    onclick: async (event) => {
      event.preventDefault();
      event.stopPropagation();
      // Read first, open after: the box is placed once, at its final size.
      let body;
      try {
        const lines = await content(topic);
        body = lines.length ? lines.map((line) => h('p', null, line))
          : h('p', { class: 'muted' }, 'This farm publishes no description for this option.');
      } catch (error) {
        body = h('p', { class: 'fail' }, error.message);
      }
      openPopover(button, [h('h3', null, topic.title), body], { label: topic.title, focus: true });
    },
  }, icon('help'));
  return button;
}

// A label followed by its help button, for field() and headings.
export const withHelp = (label, name) => [label, ' ', help(name)];
