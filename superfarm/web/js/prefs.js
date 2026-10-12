// What the operator chose about the panel itself (theme, view mode, sidebar, filters),
// remembered in this browser. Nothing of the farm is stored here and never a secret;
// with storage blocked the panel works the same and simply forgets.

const KEY = 'sf-ui';
let cache = null;

function load() {
  if (cache) return cache;
  cache = {};
  try {
    const parsed = JSON.parse(localStorage.getItem(KEY) || '{}');
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) cache = parsed;
  } catch { /* unreadable or blocked: start empty */ }
  return cache;
}

export function getPref(name, fallback = null) {
  const value = load()[name];
  return value === undefined ? fallback : value;
}

export function setPref(name, value) {
  const all = load();
  if (value == null) delete all[name];
  else all[name] = value;
  try { localStorage.setItem(KEY, JSON.stringify(all)); } catch { /* not remembered, still applied */ }
}
