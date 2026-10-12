// One approval request as a card with the operator's two buttons. The Approvals inbox and
// the Assistant's transcript show the same card, so the safety is written once:
//
//   - the headline is the daemon's own summary of the request, never text built here or
//     supplied by the assistant;
//   - the buttons do nothing for a moment after the card appears (a click that was
//     already on its way lands on nothing), and one answer runs at a time: Approve, Deny
//     and the rest share one busy state through the double-press guard (dom.js);
//   - nothing takes the keyboard focus by itself;
//   - approving something destructive takes a second, separate click on a confirmation
//     inside the card, lower down and away from the pointer, which names the Androids;
//   - once answered (here or anywhere else) or expired, the card keeps its place and its
//     height and says what became of it, so nothing below it moves.

import { get } from '../api.js';
import { approve, deny, refreshApprovals, secondsLeft } from '../approvals.js';
import { badge } from '../common.js';
import { h, replace, when } from '../dom.js';
import { icon } from '../icons.js';
import { fail, ok, warn } from './toast.js';

export const ARM_MS = 900;
const KLASS = {
  destructive: ['fail', 'alert', 'destructive: cannot be undone'],
  change: ['info', 'restart', 'change'],
  read: ['', 'dot', 'read'],
};
const STATE_WORD = { approved: 'approved', denied: 'denied', expired: 'expired: nobody answered, nothing ran' };

const clock = (seconds) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;

export function klassBadge(klass) {
  const [kind, picture, words] = KLASS[klass] || ['', 'dot', klass || 'unknown'];
  return h('span', { class: `badge ${kind}` }, icon(picture), words);
}

// item: an approval as the API gives it. nameOf(id) -> the Android's name or null.
// onSettled(slot) is called once, when the request stops waiting.
// keepHeight: an answered card keeps the height it had (the inbox, where other cards
// with buttons sit below it); in a transcript, where nothing below it can be pressed
// by mistake, it may shrink to its note.
// Returns the slot: { el, item, live, tick(), settle(item), reconcile() }.
export function approvalCard(item, { nameOf = () => null, onSettled = () => {}, keepHeight = true } = {}) {
  const targetLinks = () => (item.targets.length === 0 ? h('span', { class: 'muted' }, 'no Android in particular')
    : item.targets.map((id) => (nameOf(id)
      ? h('a', { class: 'tag', href: `#/android/${id}` }, `${nameOf(id)} (#${id})`)
      : h('span', { class: 'tag' }, `#${id}`))));

  // A request that is no longer waiting keeps its place and its height, and says what became of it.
  function settle(slot, answered) {
    if (!slot.live) return;
    slot.live = false;
    slot.item = answered;
    if (keepHeight) slot.el.style.minHeight = `${slot.el.offsetHeight}px`;
    slot.el.className = 'approval card decided';
    replace(slot.el,
      h('div', { class: 'approval-head' }, badge(answered.state === 'approved' ? 'ok' : answered.state === 'denied' ? 'fail' : 'warn', STATE_WORD[answered.state] || answered.state),
        h('span', { class: 'muted small' }, `asked by ${answered.caller}`)),
      h('p', { class: 'approval-summary' }, answered.summary),
      h('p', { class: 'small muted' },
        answered.decided_by ? `Answered by ${answered.decided_by}${answered.decided_at ? `, ${when(answered.decided_at)}` : ''}.` : '',
        answered.remembered ? ' Remembered for the rest of that assistant session.' : '',
        answered.reason ? ` Reason given: ${answered.reason}` : ''));
    onSettled(slot);
  }

  async function reconcile(slot) {
    if (slot.asking || !slot.live) return;
    slot.asking = true;
    try {
      const data = await get(`/api/approvals/${encodeURIComponent(slot.item.id)}`);
      if (data.approval.state !== 'pending') settle(slot, data.approval);
    } catch { /* the next refresh tries again */ }
    slot.asking = false;
  }

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
      h('div', { class: 'row' }, h('span', { class: 'muted' }, item.targets.length === 1 ? 'Android affected:' : 'Androids affected:'), targetLinks()),
      h('details', null, h('summary', null, 'Details: the tool and its arguments'),
        h('dl', { class: 'kv' },
          h('dt', null, 'Tool'), h('dd', null, h('code', null, item.tool)),
          h('dt', null, 'Arguments'), h('dd', null, h('pre', { class: 'log' }, JSON.stringify(item.arguments, null, 2))),
          h('dt', null, 'Asked'), h('dd', null, when(item.created)),
          h('dt', null, 'Session'), h('dd', null, h('code', null, item.session)),
          h('dt', null, 'Request'), h('dd', null, h('code', null, item.id)))),
      h('div', { class: 'approval-actions' }, approveButton, denyButton, reason, remember),
      confirm);
    slot.tick = () => {
      if (!slot.live) return;
      const left = secondsLeft(slot.item);
      const total = Math.max(1, slot.item.expires - slot.item.created);
      timer.textContent = clock(left);
      bar.style.width = `${Math.max(0, Math.min(100, (100 * left) / total)).toFixed(1)}%`;
      slot.el.classList.toggle('soon', left < 30);
      if (left <= 0) {
        // Out of time by the server's clock: no more answers from here; ask what it became.
        for (const control of controls) control.setAttribute('aria-disabled', 'true');
        reconcile(slot);
      }
    };
    slot.settle = (answered) => settle(slot, answered);
    slot.reconcile = () => reconcile(slot);
    slot.tick();
    return slot;
}
