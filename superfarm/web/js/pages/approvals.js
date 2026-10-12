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

import { get } from '../api.js';
import { approvals, approve, deny, refreshApprovals, secondsLeft, subscribe } from '../approvals.js';
import { badge, button, emptyState, linkButton, loadInstances, pageHead, panel } from '../common.js';
import { dataTable } from '../components/table.js';
import { fail, ok, warn } from '../components/toast.js';
import { h, replace, when } from '../dom.js';
import { icon } from '../icons.js';

const ARM_MS = 900;
const KLASS = {
  destructive: ['fail', 'alert', 'destructive: cannot be undone'],
  change: ['info', 'restart', 'change'],
};
const STATE_WORD = { approved: 'approved', denied: 'denied', expired: 'expired: nobody answered, nothing ran' };

const clock = (seconds) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;

function klassBadge(klass) {
  const [kind, picture, words] = KLASS[klass] || ['', 'dot', klass];
  return h('span', { class: `badge ${kind}` }, icon(picture), words);
}

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
  const targetLinks = (item) => (item.targets.length === 0 ? h('span', { class: 'muted' }, 'no Android in particular')
    : item.targets.map((id) => (nameOf(id)
      ? h('a', { class: 'tag', href: `#/android/${id}` }, `${nameOf(id)} (#${id})`)
      : h('span', { class: 'tag' }, `#${id} (no longer exists)`))));

  function drawEmpty() {
    const live = [...slots.values()].filter((slot) => slot.live).length;
    waiting.className = `badge ${live ? 'warn' : 'ok'}`;
    replace(waiting, icon(live ? 'clock' : 'check'), live ? `${live} waiting` : 'nothing waiting');
    tidy.hidden = ![...slots.values()].some((slot) => !slot.live);
    replace(nothing, slots.size === 0 ? emptyState('approval', 'Nothing is waiting for you',
      'When an AI assistant wants to change something on the farm and the policy says "ask", its request shows up here with what it would do and to which Androids. Nothing runs until you answer; a request nobody answers expires.',
      linkButton('AI access and policy', '#/access', '', 'plug')) : null);
  }

  // A request that is no longer waiting keeps its place and its height, and says what became of it.
  function settle(slot, item) {
    if (!slot.live) return;
    slot.live = false;
    slot.item = item;
    slot.el.style.minHeight = `${slot.el.offsetHeight}px`;
    slot.el.className = 'approval card decided';
    replace(slot.el,
      h('div', { class: 'approval-head' }, badge(item.state === 'approved' ? 'ok' : item.state === 'denied' ? 'fail' : 'warn', STATE_WORD[item.state] || item.state),
        h('span', { class: 'muted small' }, `asked by ${item.caller}`)),
      h('p', { class: 'approval-summary' }, item.summary),
      h('p', { class: 'small muted' },
        item.decided_by ? `Answered by ${item.decided_by}${item.decided_at ? `, ${when(item.decided_at)}` : ''}.` : '',
        item.remembered ? ' Remembered for the rest of that assistant session.' : '',
        item.reason ? ` Reason given: ${item.reason}` : ''));
    drawEmpty();
    loadHistory().catch(() => {});
  }

  async function reconcile(slot) {
    if (slot.asking) return;
    slot.asking = true;
    try {
      const data = await get(`/api/approvals/${encodeURIComponent(slot.item.id)}`);
      if (data.approval.state !== 'pending') settle(slot, data.approval);
    } catch { /* the next refresh tries again */ }
    slot.asking = false;
  }

  function card(item) {
    const destructive = item.klass === 'destructive';
    const shown = performance.now();
    const armed = () => performance.now() - shown >= ARM_MS;
    const slot = { item, live: true };
    const action = { action: `approval:${item.id}` };            // one answer at a time for this request
    const timer = h('span', { class: 'num' });
    const bar = h('span', { class: 'fill' });
    const reason = h('input', { type: 'text', maxLength: 500, placeholder: 'Reason for the assistant (optional)', 'aria-label': `Reason for denying: ${item.summary}` });

    const answer = async (work, done) => {
      if (!armed() || !slot.live) return;
      try {
        const data = await work();
        settle(slot, data.approval);
        ok(done(data.approval));
      } catch (error) {
        if (error.status === 409) {
          // Answered the other way meanwhile, or expired: show what it is now.
          warn(`Not recorded: ${error.message}`);
          await reconcile(slot);
          refreshApprovals();
        } else {
          fail(error);
        }
      }
    };
    const yes = (remember) => answer(() => approve(item.id, remember), (a) => `Approved: ${a.summary}${a.remembered ? ' (remembered for this session)' : ''}`);

    // Destructive: the first press only opens this, lower in the card and away from the pointer.
    const confirmShown = { at: 0 };
    const confirm = h('div', { class: 'approval-confirm', hidden: true },
      h('p', null, icon('alert'), ' ', h('strong', null, 'This cannot be undone. '), 'It applies to: ',
        item.targets.length ? item.targets.map((id, index) => [index ? ', ' : '', h('strong', null, nameOf(id) ? `${nameOf(id)} (#${id})` : `#${id}`)]) : 'no Android in particular', '.'),
      h('div', { class: 'row' },
        h('button', { type: 'button', class: 'btn large danger', dataset: action, onclick: () => (performance.now() - confirmShown.at >= ARM_MS ? yes(false) : undefined) },
          icon('alert'), `Yes, approve: ${item.summary}`),
        h('button', { type: 'button', class: 'btn large', onclick: () => { confirm.hidden = true; approveButton.setAttribute('aria-expanded', 'false'); } }, 'Back')));
    const approveButton = h('button', {
      type: 'button', class: `btn large ${destructive ? '' : 'primary'}`, dataset: action, 'aria-expanded': destructive ? 'false' : null,
      onclick: () => {
        if (!armed() || !slot.live) return undefined;
        if (!destructive) return yes(false);
        confirm.hidden = false;
        confirmShown.at = performance.now();
        approveButton.setAttribute('aria-expanded', 'true');
        return undefined;
      },
    }, icon('check'), destructive ? 'Approve…' : 'Approve');
    const denyButton = h('button', {
      type: 'button', class: 'btn large', dataset: action,
      onclick: () => answer(() => deny(item.id, reason.value.trim()), (a) => `Denied: ${a.summary}`),
    }, icon('x'), 'Deny');
    const remember = item.can_remember && !destructive ? h('button', {
      type: 'button', class: 'btn', dataset: action, onclick: () => yes(true),
      title: 'Also approve this same tool on these same Androids for the rest of this assistant session',
    }, 'Approve and remember for this session') : null;
    const controls = [approveButton, denyButton, remember].filter(Boolean);
    // Not armed yet: shown as such, so a click that was already on its way lands on nothing.
    for (const control of controls) control.setAttribute('aria-disabled', 'true');
    setTimeout(() => { for (const control of controls) if (!control.classList.contains('is-busy')) control.removeAttribute('aria-disabled'); }, ARM_MS);

    slot.timer = timer;
    slot.bar = bar;
    slot.controls = controls;
    slot.el = h('article', { class: `approval card ${destructive ? 'destructive' : ''}`, role: 'listitem', 'aria-label': `${item.caller} asks: ${item.summary}` },
      h('div', { class: 'approval-head' },
        klassBadge(item.klass),
        h('span', { class: 'approval-who' }, icon('plug'), 'asked by ', h('strong', null, item.caller)),
        h('span', { class: 'grow' }),
        h('span', { class: 'approval-clock', title: 'Unanswered, the request expires and nothing runs' }, icon('clock'), 'expires in ', timer)),
      h('div', { class: 'track approval-track', 'aria-hidden': 'true' }, bar),
      h('h2', { class: 'approval-summary' }, item.summary),
      h('div', { class: 'row' }, h('span', { class: 'muted' }, item.targets.length === 1 ? 'Android affected:' : 'Androids affected:'), targetLinks(item)),
      h('details', null, h('summary', null, 'Details: the tool and its arguments'),
        h('dl', { class: 'kv' },
          h('dt', null, 'Tool'), h('dd', null, h('code', null, item.tool)),
          h('dt', null, 'Arguments'), h('dd', null, h('pre', { class: 'log' }, JSON.stringify(item.arguments, null, 2))),
          h('dt', null, 'Asked'), h('dd', null, when(item.created)),
          h('dt', null, 'Session'), h('dd', null, h('code', null, item.session)),
          h('dt', null, 'Request'), h('dd', null, h('code', null, item.id)))),
      h('div', { class: 'approval-actions' }, approveButton, denyButton, reason, remember),
      confirm);
    return slot;
  }

  function tick() {
    for (const slot of slots.values()) {
      if (!slot.live) continue;
      const left = secondsLeft(slot.item);
      const total = Math.max(1, slot.item.expires - slot.item.created);
      slot.timer.textContent = clock(left);
      slot.bar.style.width = `${Math.max(0, Math.min(100, (100 * left) / total)).toFixed(1)}%`;
      slot.el.classList.toggle('soon', left < 30);
      if (left <= 0) {
        // Out of time by the server's clock: no more answers from here; ask what it became.
        for (const control of slot.controls) control.setAttribute('aria-disabled', 'true');
        reconcile(slot);
      }
    }
  }

  function draw() {
    const pending = new Set(approvals.pending.map((item) => item.id));
    for (const item of approvals.pending) {
      if (slots.has(item.id)) continue;
      const slot = card(item);
      slots.set(item.id, slot);
      list.append(slot.el);                  // always at the end: nothing above it moves
    }
    for (const slot of slots.values()) {
      if (slot.live && approvals.loaded && !pending.has(slot.item.id)) reconcile(slot);
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
