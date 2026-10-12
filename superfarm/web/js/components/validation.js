// Validation messages from the API, shown next to the field they are about.
//
// The server is the only judge of what is valid. When it refuses a value it names the
// field ({ field: "resources.memory_mb", message }); a form marks its fields with the
// same names (data-path), so the message lands under the right control instead of only
// in a toast.

import { h } from '../dom.js';
import { icon } from '../icons.js';

const CONTROL = 'input, select, textarea';

export function clearFieldErrors(...roots) {
  for (const root of roots.flat()) {
    for (const node of root.querySelectorAll('.field-error')) node.remove();
    for (const field of root.querySelectorAll('.field.invalid')) {
      field.classList.remove('invalid');
      const control = field.querySelector(CONTROL);
      if (control) control.removeAttribute('aria-invalid');
    }
  }
}

const matches = (path, name) => path === name || path.endsWith(`.${name}`) || name.endsWith(`.${path}`);

// Marks the fields an ApiError names. Returns the fields that were found on these roots
// (first one first), so the caller can bring the first into view.
export function applyFieldErrors(error, ...roots) {
  const list = roots.flat();
  clearFieldErrors(list);
  const details = error && Array.isArray(error.detail) ? error.detail.filter((d) => d && d.field && d.message) : [];
  const marked = [];
  for (const detail of details) {
    for (const root of list) {
      const field = [...root.querySelectorAll('.field[data-path]')].find((node) => matches(node.dataset.path, String(detail.field)));
      if (!field || field.classList.contains('invalid')) continue;
      field.classList.add('invalid');
      field.append(h('div', { class: 'field-error', role: 'alert' }, icon('alert'), h('span', null, detail.message)));
      const control = field.querySelector(CONTROL);
      if (control) control.setAttribute('aria-invalid', 'true');
      marked.push(field);
      break;
    }
  }
  return marked;
}

export function focusField(field) {
  const control = field && field.querySelector(CONTROL);
  if (control && control.isConnected) control.focus();
}
