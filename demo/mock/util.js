// Small pure helpers shared by the seed and the routes.

export function hex(length) {
  let out = '';
  while (out.length < length) out += Math.floor(Math.random() * 16).toString(16);
  return out;
}

export const clone = (value) => JSON.parse(JSON.stringify(value));
export const pad = (n, width = 2) => String(n).padStart(width, '0');
export const round = (value, digits = 1) => Math.round(value * 10 ** digits) / 10 ** digits;

// "20261012-040000", the way backups and recordings are named.
export function stamp(seconds) {
  const d = new Date(seconds * 1000);
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
}

// "2026-10-12T11:36:22", the way automation dates are written.
export function isoLocal(seconds) {
  const d = new Date(seconds * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// A number that is the same for the same text: stable "random" figures per Android.
export function hash(text) {
  let h = 2166136261;
  for (const ch of String(text)) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  return (h >>> 0) / 4294967295;
}

// JSON merge patch (RFC 7386), as PATCH /api/config and PATCH /api/instances take it.
export function mergePatch(target, patch) {
  if (patch === null || typeof patch !== 'object' || Array.isArray(patch)) return clone(patch);
  const out = target && typeof target === 'object' && !Array.isArray(target) ? { ...target } : {};
  for (const [key, value] of Object.entries(patch)) {
    if (value === null) delete out[key];
    else out[key] = mergePatch(out[key], value);
  }
  return out;
}

export function slug(name) {
  return String(name).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64);
}
