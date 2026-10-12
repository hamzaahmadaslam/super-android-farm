// Dialogs on the native <dialog> element: the browser provides the focus trap, Escape
// and the inert background, which is most of what makes a modal accessible.

import { h, uid } from '../dom.js';

// body: a node (or list of nodes). actions: [{ label, value, kind, run? }].
// A `run` may return false (or throw) to keep the dialog open, e.g. on a validation error.
// Resolves with the chosen action's value, or null when dismissed.
export function openDialog({ title, body, actions = [], wide = false }) {
  return new Promise((resolve) => {
    const titleId = uid('dlg');
    const error = h('p', { class: 'form-error', role: 'alert' });
    let result = null;
    const choose = async (action, button) => {
      error.textContent = '';
      if (action.run) {
        button.disabled = true;
        try {
          const value = await action.run();
          if (value === false) return;
          result = value === undefined ? (action.value ?? true) : value;
        } catch (e) {
          error.textContent = e.message;
          return;
        } finally {
          button.disabled = false;
        }
      } else {
        result = action.value ?? true;
      }
      dialog.close();
    };
    const buttons = actions.map((action) => {
      const button = h('button', { type: action.submit ? 'submit' : 'button', class: `btn ${action.kind || ''}` }, action.label);
      button.addEventListener('click', (event) => { event.preventDefault(); choose(action, button); });
      return button;
    });
    const dialog = h('dialog', { 'aria-labelledby': titleId, class: wide ? 'wide' : '' },
      h('form', { method: 'dialog', class: 'dialog-form' },
        h('div', { class: 'dialog-head' },
          h('h2', { id: titleId }, title),
          h('button', { type: 'button', class: 'btn ghost small', 'aria-label': 'Close', onclick: () => dialog.close() }, '×')),
        h('div', { class: 'dialog-body' }, body, error),
        h('div', { class: 'dialog-foot' },
          h('button', { type: 'button', class: 'btn', onclick: () => dialog.close() }, actions.length ? 'Cancel' : 'Close'),
          buttons)));
    dialog.addEventListener('close', () => { dialog.remove(); resolve(result); });
    document.body.append(dialog);
    dialog.showModal();
  });
}

// For anything destructive or hard to undo. Resolves true only on the explicit button.
export function confirmDialog({ title, message, confirm = 'Confirm', danger = false, details }) {
  return openDialog({
    title,
    body: [h('p', null, message), details],
    actions: [{ label: confirm, kind: danger ? 'danger' : 'primary', value: true }],
  }).then((value) => value === true);
}
