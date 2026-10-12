// Dialogs on the native <dialog> element: the browser provides the focus trap, Escape,
// the inert background and the return of focus to whatever opened the dialog, which is
// most of what makes a modal accessible.
//
// A dialog cannot be answered twice or by accident:
//   - one action runs at a time; while it does, every action button is busy and a
//     second press, a double click or Enter does nothing;
//   - its buttons are not armed for the first moments after it opens, so the second
//     click of a double click on "Delete" cannot land on the confirm button that just
//     appeared under the pointer;
//   - a confirmation opens with the focus on Cancel, a form with the focus in its first
//     field, where Enter submits it once;
//   - for a moment after it closes, clicks and Enter are swallowed, so the rest of a
//     double click does not land on the page that is now under the pointer.

import { h, shield, uid } from '../dom.js';
import { icon } from '../icons.js';
import { applyFieldErrors, clearFieldErrors, focusField } from './validation.js';

export const ARM_MS = 400;

// body: a node (or list of nodes). actions: [{ label, value, kind, run? }]; the last
// one is the primary action, which Enter in a field also triggers.
// A `run` may return false (or throw) to keep the dialog open, e.g. on a validation error;
// when the API names the fields it refused, the message is shown under each of them.
// Resolves with the chosen action's value, or null when dismissed.
export function openDialog({ title, body, actions = [], wide = false, icon: iconName = '', danger = false }) {
  return new Promise((resolve) => {
    const titleId = uid('dlg');
    const error = h('p', { class: 'form-error', role: 'alert' });
    const opened = performance.now();
    let result = null;
    let running = false;
    let dirty = false;

    // The shield goes up in the same breath as the dialog closes: the browser reports the
    // close a moment later, and the rest of a double click would slip through in between.
    const shut = () => { shield(); dialog.close(); };
    const setBusy = (button, on) => {
      for (const other of buttons) {
        if (on) other.setAttribute('aria-disabled', 'true');
        else other.removeAttribute('aria-disabled');
      }
      button.classList.toggle('is-busy', on);
      if (on) button.setAttribute('aria-busy', 'true');
      else button.removeAttribute('aria-busy');
    };

    const choose = async (action, button) => {
      if (running || performance.now() - opened < ARM_MS) return;
      error.textContent = '';
      clearFieldErrors(dialog);
      if (action.run) {
        running = true;
        setBusy(button, true);
        try {
          const value = await action.run();
          if (value === false) return;
          result = value === undefined ? (action.value ?? true) : value;
        } catch (e) {
          const marked = applyFieldErrors(e, dialog);
          error.textContent = e.message;
          if (marked.length) {
            // The field may sit in a collapsed "Advanced" block.
            for (let node = marked[0].parentElement; node && node !== dialog; node = node.parentElement) {
              if (node.tagName === 'DETAILS') node.open = true;
            }
            focusField(marked[0]);
          }
          return;
        } finally {
          running = false;
          setBusy(button, false);
        }
      } else {
        result = action.value ?? true;
      }
      shut();
    };

    const primary = actions.at(-1);
    const buttons = actions.map((action) => {
      // The primary action is the form's submit button: a click and Enter in a field
      // both arrive as one submit event.
      const button = h('button', { type: action === primary ? 'submit' : 'button', class: `btn ${action.kind || ''}` }, action.label);
      if (action !== primary) button.addEventListener('click', (event) => { event.preventDefault(); choose(action, button); });
      return button;
    });
    const cancel = h('button', { type: 'button', class: 'btn', onclick: () => { if (!running) shut(); } }, actions.length ? 'Cancel' : 'Close');
    const form = h('form', { method: 'dialog', class: 'dialog-form', novalidate: true },
      h('div', { class: 'dialog-head' },
        iconName && h('span', { class: `dialog-icon ${danger ? 'danger' : ''}` }, icon(iconName, 'lg')),
        h('h2', { id: titleId }, title),
        h('button', { type: 'button', class: 'btn ghost icon-only', 'aria-label': 'Close', title: 'Close', onclick: () => { if (!running) shut(); } }, icon('x'))),
      h('div', { class: 'dialog-body' }, body, error),
      h('div', { class: 'dialog-foot' }, cancel, buttons));
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      if (primary) choose(primary, buttons.at(-1));
    });
    const dialog = h('dialog', { 'aria-labelledby': titleId, class: wide ? 'wide' : '' }, form);
    dialog.addEventListener('input', () => { dirty = true; });
    // Escape while an action runs must not close the dialog under it.
    dialog.addEventListener('cancel', (event) => { if (running) event.preventDefault(); });
    // A click on the dimmed background dismisses, as Cancel does, unless something was
    // typed: a stray click must not throw a filled-in form away.
    let downOutside = false;
    const outside = (event) => {
      const box = dialog.getBoundingClientRect();
      return event.target === dialog && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom);
    };
    dialog.addEventListener('pointerdown', (event) => { downOutside = outside(event); });
    dialog.addEventListener('click', (event) => {
      if (downOutside && outside(event) && !dirty && !running && performance.now() - opened >= ARM_MS) shut();
      downOutside = false;
    });
    dialog.addEventListener('close', () => { shield(); dialog.remove(); resolve(result); });
    document.body.append(dialog);
    dialog.showModal();
    const field = dialog.querySelector('.dialog-body input:not([type="hidden"]):not([type="checkbox"]):not(:disabled), .dialog-body select:not(:disabled), .dialog-body textarea:not(:disabled)');
    (danger || !field ? cancel : field).focus();
  });
}

// For anything destructive or hard to undo. Resolves true only on the explicit button.
// `details` is where the caller lists exactly what will be affected.
export function confirmDialog({ title, message, confirm = 'Confirm', danger = false, details, icon: iconName }) {
  return openDialog({
    title,
    icon: iconName || (danger ? 'alert' : 'help'),
    danger,
    body: [h('p', null, message), details],
    actions: [{ label: confirm, kind: danger ? 'danger' : 'primary', value: true }],
  }).then((value) => value === true);
}
