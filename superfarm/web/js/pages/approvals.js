// Approvals: the operator's Yes or No for what AI assistants ask to do.
//
// This page is itself a control that must not be pressed by accident, so it is built
// to stand still:
//   - the list only ever grows at the end. A request that is answered or expires stays
//     in its place as a short note until the operator tidies up, so no button ever
//     slides under the pointer;
//   - the buttons of a card that just appeared (and of the whole page, right after it
//     opens) do nothing for a moment;
//   - nothing here takes the keyboard focus by itself and no shortcut answers a request;
//   - approving something destructive takes a second, separate click on a confirmation
//     inside the card, which names the Androids;
//   - the headline is the daemon's own summary of the request, never text built here
//     or supplied by the assistant.
// The card itself, with those rules, is components/approval-card.js; the Assistant's
// transcript shows the same one.

import { get } from '../api.js';
import { approvals, refreshApprovals, subscribe } from '../approvals.js';
import { badge, button, emptyState, linkButton, loadInstances, pageHead, panel } from '../common.js';
import { approvalCard, klassBadge } from '../components/approval-card.js';
import { dataTable } from '../components/table.js';
import { h, replace, when } from '../dom.js';
import { icon } from '../icons.js';

export async function render(root, _params, scope) {
  let instances = await loadInstances().catch(() => []);
  const slots = new Map();          // id -> { el, item, live, timer, bar, controls }
  const list = h('div', { class: 'approvals', role: 'list', 'aria-label': 'Requests waiting for an answer' });
  const nothing = h('div');
  const tidy = button('Clear answered requests', () => {
    for (const [id, slot] of slots) if (!slot.live) { slot.el.remove(); slots.delete(id); }
    drawEmpty();
  }, 'small', 'check');
  const waiting = h('span', { class: 'badge', role: 'status' });

  const nameOf = (id) => {
    const found = instances.find((i) => i.id === id);
    return found ? found.name : null;
  };
  function drawEmpty() {
    const live = [...slots.values()].filter((slot) => slot.live).length;
    waiting.className = `badge ${live ? 'warn' : 'ok'}`;
    replace(waiting, icon(live ? 'clock' : 'check'), live ? `${live} waiting` : 'nothing waiting');
    tidy.hidden = ![...slots.values()].some((slot) => !slot.live);
    replace(nothing, slots.size === 0 ? emptyState('approval', 'Nothing is waiting for you',
      'When an AI assistant wants to change something on the farm and the policy says "ask", its request shows up here with what it would do and to which Androids. Nothing runs until you answer; a request nobody answers expires.',
      linkButton('AI access and policy', '#/access', '', 'plug')) : null);
  }

  const tick = () => { for (const slot of slots.values()) slot.tick(); };

  function draw() {
    const pending = new Set(approvals.pending.map((item) => item.id));
    for (const item of approvals.pending) {
      if (slots.has(item.id)) continue;
      const slot = approvalCard(item, { nameOf, onSettled: () => { drawEmpty(); loadHistory().catch(() => {}); } });
      slots.set(item.id, slot);
      list.append(slot.el);                  // always at the end: nothing above it moves
    }
    for (const slot of slots.values()) {
      if (slot.live && approvals.loaded && !pending.has(slot.item.id)) slot.reconcile();
    }
    drawEmpty();
    tick();
  }

  // ---------------------------------------------------------------- history
  let filter = '';
  const chips = h('div', { class: 'chips', role: 'group', 'aria-label': 'Filter the history' });
  const history = dataTable({
    label: 'Answered requests',
    key: (a) => a.id,
    columns: [
      { title: 'Asked', cell: (a) => h('span', { class: 'nowrap' }, when(a.created)) },
      { title: 'By', cell: (a) => a.caller },
      { title: 'Request', cell: (a) => [h('div', null, a.summary), h('div', { class: 'small muted' }, klassBadge(a.klass), ' ', h('code', null, a.tool))] },
      { title: 'Answer', cell: (a) => [badge(a.state === 'approved' ? 'ok' : a.state === 'denied' ? 'fail' : 'warn', a.state),
        a.remembered ? h('div', { class: 'small muted' }, 'remembered for the session') : null,
        a.reason ? h('div', { class: 'small muted' }, `Reason: ${a.reason}`) : null] },
      { title: 'Answered by', cell: (a) => (a.decided_by ? [h('div', null, a.decided_by), h('div', { class: 'small muted' }, when(a.decided_at))] : h('span', { class: 'muted' }, 'nobody')) },
    ],
    empty: emptyState('clock', 'No answered requests yet', 'Every request, with who answered it and when, is kept here and in logs/approvals.jsonl in the farm home.'),
  });
  let past = [];
  function drawHistory() {
    const count = (state) => past.filter((a) => a.state === state).length;
    replace(chips, [['', 'All', past.length], ['approved', 'approved', count('approved')], ['denied', 'denied', count('denied')], ['expired', 'expired', count('expired')]]
      .map(([value, label, n]) => h('button', { type: 'button', class: 'chip', 'aria-pressed': String(filter === value), onclick: () => { filter = value; drawHistory(); } },
        label, h('span', { class: 'count' }, n))));
    history.update(filter ? past.filter((a) => a.state === filter) : past);
  }
  async function loadHistory() {
    past = (await get('/api/approvals', { limit: 200 })).approvals.filter((a) => a.state !== 'pending');
    drawHistory();
  }

  root.append(
    pageHead('Approvals', 'AI assistants ask here before they change anything on the farm. Nothing runs until you answer, and a request nobody answers expires by itself.',
      waiting, tidy, linkButton('AI access', '#/access', '', 'plug')),
    h('div', { class: 'stack' }, nothing, list,
      panel('History', { icon: 'clock', actions: button('Refresh', () => loadHistory(), 'small', 'restart') }, chips, history.el)));

  draw();
  scope.add(subscribe(draw));
  const ticking = setInterval(tick, 1000);
  scope.add(() => clearInterval(ticking));
  scope.on(window, 'sf:changed', () => loadInstances().then((rows) => { instances = rows; }).catch(() => {}));
  refreshApprovals();
  await scope.every(15000, loadHistory);
}
