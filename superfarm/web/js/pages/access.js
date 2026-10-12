// AI access: what assistants may do on this farm, tool by tool, and how to connect one.
//
// Everything shown is the daemon's: the policy and what it does with each tool come from
// GET /api/mcp/info, the sentences that explain the choices from the config schema, and
// the snippets to paste into a client from the same answer. A change is saved at once
// with PATCH /api/config, then the page reads the result back.

import { get, patch } from '../api.js';
import { badge, button, emptyState, linkButton, pageHead, panel, skeleton } from '../common.js';
import { describeConfig } from '../components/help.js';
import { tabs } from '../components/tabs.js';
import { fail, ok } from '../components/toast.js';
import { copyText, h, replace } from '../dom.js';
import { icon } from '../icons.js';
import { modelsTab } from './ai-models.js';

const MODES = [['read-only', 'Read-only', 'device'], ['ask', 'Ask me', 'approval'], ['allow', 'Allow', 'check']];
const CLASSES = [
  ['read', 'Reading', 'Looks at the farm; changes nothing.'],
  ['change', 'Changing', ''],
  ['destructive', 'Destructive', ''],
];
const DECISION = { allow: ['ok', 'runs without asking'], ask: ['warn', 'asks you first'], deny: ['fail', 'not available'] };

// "read-only: look, never change anything." out of the description of ai.policy.
const sentence = (description, key) => {
  const found = new RegExp(`(?:^|[.] )${key}: ([^.]*[.])`).exec(description || '');
  return found ? found[1] : '';
};

// A switch: a button that says on or off, saved the moment it is pressed. Being a
// button built with h(), it is busy (and deaf to a second press) until the save settles.
function toggle(label, on, save, { disabled = false, title = '' } = {}) {
  return h('button', {
    type: 'button', class: 'switch', role: 'switch', 'aria-checked': String(on), 'aria-label': label, title: title || null, disabled,
    onclick: () => save(!on),
  }, h('span', { class: 'knob', 'aria-hidden': 'true' }), h('span', { class: 'switch-word' }, on ? 'on' : 'off'));
}

export async function render(root, params, scope) {
  const view = tabs([
    { id: 'policy', label: 'Policy and tools', render: (panelEl, inner) => policyTab(panelEl, inner) },
    { id: 'models', label: 'Models', render: (panelEl) => modelsTab(panelEl) },
  ], { active: params[0] || 'policy', parent: scope, label: 'AI access', onChange: (id) => history.replaceState(null, '', `#/access/${id}`) });
  root.append(
    pageHead('AI access', 'What AI assistants may do on this farm through its tools, which models the farm itself may use, and how to connect an assistant.',
      linkButton('Approvals', '#/approvals', '', 'approval')),
    view.el);
}

async function policyTab(root, scope) {
  const policyBox = h('div', null, skeleton(3));
  const toolsBox = h('div', null, skeleton(6));
  const connectBox = h('div', null, skeleton(4));
  const texts = { policy: '', ask_for: '', auto_approve: '', deny: '', timeout: '', enabled: '' };

  root.append(
    h('div', { class: 'stack' },
      h('div', { class: 'banner info', role: 'note' }, icon('info'),
        h('div', { class: 'grow' }, h('strong', null, 'What this guards. '),
          'The policy and the approvals are a gate for assistants that work through the farm’s MCP server (sf mcp). ',
          'Anything that holds the API token can call the API directly, without asking: the token is control of the farm. Connect only clients you would let run commands as you.')),
      panel('Policy', { icon: 'shield' }, policyBox),
      panel('Tools', { icon: 'apps' }, toolsBox),
      panel('Connect an assistant', { icon: 'plug' }, connectBox)));

  // One save at a time; the page is drawn again from what the daemon then reports.
  const save = async (ai, done) => {
    try {
      await patch('/api/config', { patch: { ai } });
      ok(done);
    } catch (error) {
      fail(error);
    }
    await load();
  };

  function drawPolicy(info) {
    const changing = info.ask_for.includes('change');
    replace(policyBox,
      h('div', { class: 'choices', role: 'radiogroup', 'aria-label': 'What AI assistants may do' }, MODES.map(([value, label, picture]) => h('button', {
        type: 'button', class: 'choice', role: 'radio', 'aria-checked': String(info.policy === value), dataset: { action: 'ai-policy' },
        onclick: () => (info.policy === value ? undefined : save({ policy: value }, `AI policy is now "${label}".`)),
      }, h('span', { class: 'choice-head' }, icon(picture, 'lg'), h('strong', null, label), info.policy === value && h('span', { class: 'badge ok' }, icon('check'), 'current')),
      h('span', { class: 'choice-text' }, sentence(texts.policy, value) || value)))),
      !MODES.every(([value]) => sentence(texts.policy, value)) && texts.policy ? h('p', { class: 'small muted' }, texts.policy) : null,
      info.policy === 'ask' && h('div', { class: 'setting-row' },
        h('div', { class: 'grow' }, h('strong', null, 'Ask before changing actions too'), h('div', { class: 'small muted' }, texts.ask_for)),
        toggle('Ask before changing actions too', changing,
          (on) => save({ ask_for: on ? ['change', 'destructive'] : ['destructive'] }, on ? 'Changing actions now ask first.' : 'Only destructive actions ask now.'))),
      h('p', { class: 'small muted' }, `A request waits ${info.approval_timeout_seconds} seconds for an answer. `, texts.timeout, ' ',
        h('a', { href: '#/settings/ai' }, 'All AI settings')));
  }

  function drawTools(info) {
    const asking = info.policy === 'ask';
    const row = (tool) => {
      const [kind, words] = DECISION[tool.decision] || ['', tool.decision];
      const auto = info.auto_approve.includes(tool.name);
      const denied = info.deny.includes(tool.name);
      const without = (list, name) => list.filter((entry) => entry !== name);
      return h('tr', null,
        h('th', { scope: 'row' }, h('div', null, tool.title), h('code', null, tool.name)),
        h('td', null, h('span', { class: 'small muted' }, tool.description)),
        h('td', null, badge(kind === 'ok' ? 'ok' : kind === 'warn' ? 'warn' : 'fail', `${tool.decision}: ${words}`), h('div', { class: 'small muted' }, tool.reason)),
        h('td', null, tool.class === 'read' ? h('span', { class: 'muted small' }, 'never asks')
          : toggle(`Never ask before ${tool.title}`, auto,
            (on) => save({ auto_approve: on ? [...info.auto_approve, tool.name] : without(info.auto_approve, tool.name) },
              on ? `${tool.title} no longer asks.` : `${tool.title} asks again.`),
            { disabled: !asking || denied, title: denied ? 'Switched off for assistants' : asking ? '' : 'Only applies under the policy "Ask me"' })),
        h('td', null, toggle(`Never allow ${tool.title}`, denied,
          (on) => save({ deny: on ? [...info.deny, tool.name] : without(info.deny, tool.name) },
            on ? `${tool.title} is now switched off for assistants.` : `${tool.title} is available to assistants again.`))));
    };
    replace(toolsBox,
      h('p', { class: 'small muted' }, 'Never ask: ', texts.auto_approve, ' Never allow: ', texts.deny),
      CLASSES.map(([klass, label, note]) => {
        const tools = info.tools.filter((tool) => tool.class === klass);
        if (!tools.length) return null;
        return h('div', { class: 'stack tight' },
          h('h3', null, label, ' ', h('span', { class: 'muted small' }, `${tools.length} tool(s)${note ? `. ${note}` : ''}`)),
          h('div', { class: 'table-wrap' }, h('table', { class: 'dense tools', 'aria-label': `${label} tools` },
            h('thead', null, h('tr', null, ['Tool', 'What it does', 'Under the current policy', 'Never ask', 'Never allow'].map((title) => h('th', { scope: 'col' }, title)))),
            h('tbody', null, tools.map(row)))));
      }),
      info.tools.length === 0 && emptyState('apps', 'No tools', 'This farm publishes no tools for assistants.'));
  }

  function drawConnect(info) {
    const config = info.client_config || {};
    const snippet = (title, note, value) => {
      const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
      return h('div', { class: 'stack tight' },
        h('div', { class: 'row between' }, h('h3', null, title),
          button('Copy', () => copyText(text).then(() => ok(`${title}: copied.`), () => fail(new Error('Copying is not available in this browser; select the text instead.'))), 'small', 'copy')),
        note && h('p', { class: 'small muted' }, note),
        h('pre', { class: 'log', tabindex: '0', 'aria-label': title }, text));
    };
    replace(connectBox,
      h('div', { class: 'row' }, badge(info.enabled ? 'ok' : 'warn', info.enabled ? 'MCP server enabled' : 'MCP server switched off'),
        info.sdk_version ? h('span', { class: 'small muted' }, `MCP library ${info.sdk_version}`) : badge('warn', 'MCP library not installed'),
        h('span', { class: 'small muted' }, texts.enabled)),
      config.stdio && snippet('For a client on this machine (stdio)', 'Paste into the client’s MCP configuration. The client starts sf mcp itself, as you.', config.stdio),
      config.http && snippet('For a client that connects over HTTP', `Loopback only, port ${info.http_port}; the bearer token is the one sf token prints.`, config.http),
      config.http_start && snippet('Start the HTTP server', '', config.http_start));
  }

  async function load() {
    const info = await get('/api/mcp/info');
    drawPolicy(info);
    drawTools(info);
    drawConnect(info);
  }

  const [policy, askFor, auto, deny, timeout, enabled] = await Promise.all(
    ['ai.policy', 'ai.ask_for', 'ai.auto_approve', 'ai.deny', 'ai.approval_timeout_seconds', 'ai.mcp.enabled'].map((path) => describeConfig(path).catch(() => '')));
  Object.assign(texts, { policy, ask_for: askFor, auto_approve: auto, deny, timeout, enabled });
  await load();
  scope.on(window, 'sf:config', () => load().catch(() => {}));
}
