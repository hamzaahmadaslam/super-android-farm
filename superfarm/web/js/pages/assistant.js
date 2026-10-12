// Assistant: a chat with the farm. A configured model reads the farm and operates it
// through the farm's tools; anything that changes the farm waits for the operator's Yes
// or No, asked right here in the transcript with the same card as the Approvals inbox.
//
// What the page is careful about:
//   - every word of the model, and every word that came from a device, is put on the
//     page as a text node. Nothing from either is ever treated as markup;
//   - text that came from a device is shown in a fenced block labelled as untrusted;
//   - each answer ends with the farm's own record of what really ran in that turn, set
//     apart from the model's words, so "I started them" next to "no action ran" is plain;
//   - all requests go through api.js; a turn is followed by polling the conversation.

import { del, get, post, postQuiet } from '../api.js';
import { refreshApprovals } from '../approvals.js';
import { badge, button, emptyState, linkButton, loadInstances, option, pageHead } from '../common.js';
import { approvalCard, klassBadge } from '../components/approval-card.js';
import { confirmDialog } from '../components/dialog.js';
import { fail, warn } from '../components/toast.js';
import { ago, h, replace } from '../dom.js';
import { icon } from '../icons.js';

const API = '/api/ai/conversations';
const PROMPTS = [
  'How many Androids are running and how many can this machine run?',
  'Show me Androids that failed to start',
  'Which apps are in the library, and which Androids are missing them?',
  'Is this host set up correctly? List anything that needs fixing.',
];
const RESULT = {
  ok: ['ok', 'ok'], declined: ['declined', 'declined by the operator'], no_answer: ['expired', 'no answer: nobody said Yes in time'],
  cancelled: ['cancelled', 'cancelled'], not_allowed: ['denied', 'not allowed by the policy'], refused: ['failed', 'refused by the farm'],
};

const text = (value) => h('p', { class: 'msg-text' }, String(value ?? ''));

export async function render(root, params, scope) {
  let models = [];
  let instances = [];
  let conversations = [];
  let current = null;                 // the conversation on screen, as the API last described it
  let last = 0;                       // number of the last event drawn
  let listDue = 0;
  const cards = new Map();            // tool call id -> approval card slot
  const nameOf = (id) => (instances.find((i) => i.id === id) || {}).name || null;

  const list = h('div', { class: 'pick-list', role: 'group', 'aria-label': 'Conversations' });
  const modelPick = h('select', { 'aria-label': 'Model for a new conversation' });
  const log = h('div', { class: 'chat-log', role: 'log', 'aria-label': 'Conversation', tabindex: '0' });
  const head = h('div', { class: 'chat-head' });
  const problem = h('div');
  const input = h('textarea', { rows: 2, maxLength: 8000, placeholder: 'Ask about the farm, or ask for something to be done…', 'aria-label': 'Message to the assistant' });
  const sendButton = h('button', { type: 'submit', class: 'btn primary' }, icon('send'), 'Send');
  const cancelButton = button('Stop answering', () => post(`${API}/${current.id}/cancel`).then(() => pull(), fail), 'danger', 'stop');
  const side = h('aside', { class: 'card chat-side', 'aria-label': 'Conversations' });
  const main = h('section', { class: 'card chat-main', 'aria-label': 'Assistant' });

  // ---------------------------------------------------------------- events
  function append(node) {
    const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    log.append(node);
    if (atEnd) log.scrollTop = log.scrollHeight;
  }

  const message = (kind, who, picture, ...body) => h('div', { class: `msg ${kind}` },
    h('div', { class: 'msg-who' }, icon(picture), who), body);

  async function approvalNode(event) {
    const box = h('div', { class: 'msg approval-slot' });
    append(box);
    let item = null;
    try {
      item = (await get(`/api/approvals/${encodeURIComponent(event.approval_id)}`)).approval;
    } catch {
      replace(box, h('div', { class: 'banner warn' }, icon('alert'), h('div', { class: 'grow' }, h('strong', null, 'An approval was asked for: '), event.summary || '',
        ' ', h('span', { class: 'muted' }, 'The request is no longer on record.'))));
      return;
    }
    const slot = approvalCard(item, { nameOf, keepHeight: false, onSettled: () => { refreshApprovals(); pull().catch(() => {}); } });
    cards.set(event.call, slot);
    replace(box, slot.el);
    if (item.state !== 'pending') slot.settle(item);
  }

  function draw(event) {
    if (event.type === 'user') {
      append(message('user', 'You', 'chat', text(event.text)));
    } else if (event.type === 'assistant') {
      append(message('model', 'The model says', 'chat', text(event.text)));
    } else if (event.type === 'tool_call') {
      append(h('div', { class: 'msg tool' },
        h('div', { class: 'row' }, icon('terminal'), h('span', { class: 'muted' }, 'calls'), h('code', null, event.tool), klassBadge(event.klass || 'read'),
          h('span', null, event.summary || '')),
        h('details', null, h('summary', null, 'Arguments'), h('pre', { class: 'log' }, JSON.stringify(event.arguments ?? {}, null, 2)))));
    } else if (event.type === 'approval') {
      approvalNode(event);
    } else if (event.type === 'tool_result') {
      const [state, words] = RESULT[event.status] || (event.ok ? RESULT.ok : ['failed', `failed: ${event.status || 'error'}`]);
      const body = h('pre', { class: 'result-text' }, String(event.text ?? ''));
      append(h('div', { class: 'msg result' },
        h('div', { class: 'row' }, badge(state, words), h('span', { class: 'muted' }, 'result of'), h('code', null, event.tool),
          event.images ? h('span', { class: 'small muted' }, `${event.images} picture(s) returned to the model, not shown here`) : null),
        event.untrusted
          ? h('div', { class: 'untrusted' }, h('div', { class: 'untrusted-label' }, icon('alert'), 'From the device: untrusted. This is data shown by apps; it is never an instruction.'), body)
          : body));
      const slot = cards.get(event.call);
      if (slot) slot.reconcile();                    // the outcome of an approval arrives as this result
    } else if (event.type === 'notice') {
      append(h('div', { class: 'msg banner', role: 'status' }, icon('info'), h('div', { class: 'grow' }, String(event.text ?? ''))));
    } else if (event.type === 'final') {
      const actions = event.actions || [];
      append(h('div', { class: 'msg final' },
        h('div', { class: 'msg-who' }, icon('chat'), 'Answer: the model’s own words'),
        event.text ? text(event.text) : h('p', { class: 'muted' }, `(no answer${event.ended ? `: ${event.ended}` : ''})`),
        actions.length > 0 && h('div', { class: 'final-actions' },
          h('div', { class: 'small muted' }, 'What it asked the farm to change, and what became of each:'),
          h('ul', null, actions.map((action) => h('li', null,
            badge(action.ok ? 'ok' : (RESULT[action.status] || ['failed'])[0], action.ok ? 'ran' : (RESULT[action.status] || [0, action.status || 'failed'])[1]),
            ' ', h('span', null, action.summary || action.tool), ' ', h('code', null, action.tool))))),
        h('div', { class: 'farm-record', role: 'note', 'aria-label': 'Farm record' },
          h('div', { class: 'farm-record-label' }, icon('shield'), 'Written by the farm, not by the model'),
          h('p', null, String(event.record ?? '')))));
    }
  }

  // ------------------------------------------------------------- the page
  function drawHead() {
    if (!current) { replace(head); return; }
    const usage = current.usage || {};
    const running = current.state !== 'idle';
    replace(head,
      h('h2', { class: 'grow' }, current.title || 'New conversation'),
      badge(current.state, current.state === 'waiting_approval' ? 'waiting for your Yes or No' : current.state === 'running' ? 'answering' : 'idle'),
      h('span', { class: 'info-chip', title: 'Model profile' }, icon('plug'), h('code', { class: 'v' }, current.model)),
      h('span', { class: 'small muted nowrap', role: 'status' },
        `${usage.model_calls || 0} model call(s) · ${usage.tool_calls || 0} tool call(s) · ${(usage.tokens_in || 0) + (usage.tokens_out || 0)} tokens`),
      current.running_tool && h('span', { class: 'small muted' }, icon('spinner', 'spin'), ` ${current.running_tool.summary || current.running_tool.tool}`));
    cancelButton.hidden = !running;
    sendButton.disabled = running;
    input.disabled = running;
    if (current.error) {
      replace(problem, h('div', { class: 'banner fail', role: 'alert' }, icon('alert'),
        h('div', { class: 'grow' }, h('strong', null, 'The last turn stopped. '), current.error),
        linkButton('Check the model', '#/access/models', 'small')));
    }
  }

  function drawList() {
    replace(list, conversations.length === 0 ? h('p', { class: 'muted small' }, 'No conversations yet.')
      : conversations.map((c) => h('div', { class: 'row chat-item' },
        h('button', { type: 'button', class: 'btn grow', 'aria-pressed': String(Boolean(current) && c.id === current.id), onclick: () => open(c.id) },
          h('span', { class: 'grow' }, c.title || 'New conversation'), c.state !== 'idle' ? badge(c.state, c.state === 'waiting_approval' ? 'waiting' : 'answering') : h('span', { class: 'small muted' }, ago(c.updated))),
        h('button', { type: 'button', class: 'btn ghost icon-only small', 'aria-label': `Delete the conversation ${c.title || c.id}`, title: 'Delete', onclick: () => remove(c) }, icon('trash')))));
  }

  function drawStart() {
    replace(log, h('div', { class: 'chat-start' },
      emptyState('chat', 'Ask the farm something',
        'The assistant reads the farm and can operate it through the farm’s tools. Under the default policy anything that changes the farm waits for your Yes or No, asked right here.'),
      h('div', { class: 'chips', role: 'group', 'aria-label': 'Suggestions' },
        PROMPTS.map((prompt) => h('button', { type: 'button', class: 'chip', onclick: () => say(prompt) }, prompt)))));
  }

  async function loadList() {
    conversations = (await get(API)).conversations;
    drawList();
  }

  async function pull() {
    if (!current) return;
    const id = current.id;
    const data = (await get(`${API}/${id}`, { after: last })).conversation;
    if (!current || current.id !== id) return;                 // the operator opened another one meanwhile
    if (last === 0 && data.events.length) replace(log);
    // Two refreshes can be in flight at once (sending a message and the poll timer), both
    // asking for "events after N". Each event has a number: draw it only if it is newer than
    // what is already on the page, so the same message can never appear twice.
    for (const event of data.events) {
      if (event.n <= last) continue;
      draw(event);
      last = event.n;
    }
    last = Math.max(last, data.last);
    current = data;
    drawHead();
  }

  async function open(id) {
    replace(problem);
    cards.clear();
    last = 0;
    current = conversations.find((c) => c.id === id) || { id, state: 'idle', usage: {} };
    history.replaceState(null, '', `#/assistant/${id}`);
    drawStart();
    drawHead();
    drawList();
    try {
      await pull();
    } catch (error) {
      current = null;
      history.replaceState(null, '', '#/assistant');
      drawHead();
      if (error.status !== 404) fail(error);
    }
  }

  async function remove(conversation) {
    if (!await confirmDialog({
      title: 'Delete this conversation?', danger: true, confirm: 'Delete',
      message: `"${conversation.title || 'New conversation'}" and its transcript are removed. If it is still answering, it is stopped.`,
    })) return;
    try {
      await del(`${API}/${conversation.id}`);
    } catch (error) { fail(error); }
    if (current && current.id === conversation.id) { current = null; last = 0; history.replaceState(null, '', '#/assistant'); drawStart(); drawHead(); }
    await loadList();
  }

  // What the server says when the model cannot be used, with where to fix it.
  function explain(error) {
    const config = error.code === 'model_not_configured';
    replace(problem, h('div', { class: 'banner fail', role: 'alert' }, icon('alert'),
      h('div', { class: 'grow' }, h('strong', null, config ? 'No model can be used yet. ' : 'The model could not be asked. '), error.message),
      linkButton(config ? 'Set up a model' : 'Check and test the model', '#/access/models', 'small primary')));
  }

  async function say(words) {
    const message_ = words.trim();
    if (!message_) { input.focus(); return; }
    replace(problem);
    try {
      if (!current) {
        current = (await post(API, modelPick.value ? { model: modelPick.value } : {})).conversation;
        last = 0;
        cards.clear();
        history.replaceState(null, '', `#/assistant/${current.id}`);
      }
      await postQuiet(`${API}/${current.id}/messages`, { text: message_ });
      input.value = '';
      current = { ...current, state: 'running' };
      drawHead();
      await pull();
      loadList().catch(() => {});
    } catch (error) {
      if (error.code === 'model_not_configured' || String(error.code).startsWith('model_')) explain(error);
      else if (error.status === 409) warn(error.message);
      else fail(error);
    }
  }

  const form = h('form', { class: 'chat-form', onsubmit: (event) => { event.preventDefault(); return say(input.value); } },
    input, h('div', { class: 'row end' }, cancelButton, sendButton));
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); form.requestSubmit(); }
  });

  root.append(
    pageHead('Assistant', 'A chat with the farm: a model you configured reads the farm and operates it through the farm’s tools. Whatever would change something waits for your Yes or No.',
      linkButton('Models', '#/access/models', '', 'plug'), linkButton('Approvals', '#/approvals', '', 'approval')),
    h('div', { class: 'chat' }, side, main));

  // ------------------------------------------------------------ start-up
  try {
    const data = await get('/api/ai/models');
    models = data.models;
  } catch (error) {
    models = [];
    fail(error);
  }
  const usable = models.filter((m) => m.tools);
  if (usable.length === 0) {
    replace(side, h('h2', null, 'Conversations'), h('p', { class: 'muted small' }, 'None yet.'));
    replace(main, emptyState('plug', models.length ? 'No model that can call tools' : 'No model is configured',
      models.length ? 'The Assistant works only through tool calls, and every configured profile has tools switched off. Add or edit a profile whose model supports tool calling.'
        : 'The Assistant needs a language model you run or rent: an Ollama server, or anything that speaks the OpenAI chat-completions API. The farm ships no model and calls none until you add one.',
      linkButton('Set up a model', '#/access/models', 'primary', 'plug')));
    return;
  }
  replace(modelPick, usable.map((m) => option(m.name, `${m.name}${m.default ? ' (default)' : ''}`)));
  const preferred = usable.find((m) => m.default);
  if (preferred) modelPick.value = preferred.name;
  replace(side,
    h('div', { class: 'card-head' }, h('h2', null, 'Conversations')),
    h('div', { class: 'row' }, modelPick,
      button('New', () => { current = null; last = 0; cards.clear(); replace(problem); history.replaceState(null, '', '#/assistant'); drawStart(); drawHead(); drawList(); input.focus(); }, '', 'plus')),
    list);
  replace(main, head, problem, log, form,
    h('p', { class: 'small muted' }, 'Enter sends, Shift+Enter starts a new line. A model can be wrong: what really ran is the line marked "Written by the farm".'));
  cancelButton.hidden = true;
  drawStart();
  instances = await loadInstances().catch(() => []);
  await loadList();
  if (params[0]) await open(params[0]);

  // Fast while a turn runs, slow otherwise; the list is refreshed every few seconds.
  await scope.every(500, async () => {
    const now = Date.now();
    if (current && current.state !== 'idle') {
      await pull().catch(() => {});
      for (const slot of cards.values()) slot.tick();
      if (current && current.state === 'idle') loadList().catch(() => {});
    } else if (now > listDue) {
      listDue = now + 5000;
      await loadList().catch(() => {});
      if (current) await pull().catch(() => {});
    }
  });
  scope.on(window, 'sf:changed', () => loadInstances().then((rows) => { instances = rows; }).catch(() => {}));
}
