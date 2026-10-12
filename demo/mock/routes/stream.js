// Touching devices: broadcast input, and the native windows a real host would open.
// The pictures themselves (frames, the stream, screenshots) are not requests in the demo:
// they are drawn in the page and handed out by transport.js as blob URLs.

import { route } from '../backend.js';
import * as device from '../device.js';
import { ApiError, record, targets } from '../farm.js';
import { db } from '../store.js';

const number = (body, field) => {
  if (typeof body[field] !== 'number' || !Number.isFinite(body[field])) throw new ApiError(422, 'validation_error', `${field}: Input should be a valid number`, { detail: [{ field, message: 'Input should be a valid number' }] });
  return body[field];
};

// One gesture on every target: each Android answers for itself.
function input(body, act) {
  const results = {};
  for (const id of targets(body)) {
    const rec = db.instances[id];
    if (rec.phase !== 'booted') { results[id] = { ok: false, detail: `Android ${id} is not running`, ms: 0 }; continue; }
    act(rec);
    results[id] = { ok: true, detail: '', ms: 18 + (id % 7) };
  }
  return { ok: Object.values(results).every((r) => r.ok), results };
}

route('POST', '/api/input/tap', ({ body }) => { const x = number(body, 'x'); const y = number(body, 'y'); return input(body, (rec) => device.tap(rec, x, y)); });
route('POST', '/api/input/long-press', ({ body }) => { const x = number(body, 'x'); const y = number(body, 'y'); return input(body, (rec) => device.tap(rec, x, y)); });
route('POST', '/api/input/swipe', ({ body }) => {
  const [x1, y1, x2, y2] = ['x1', 'y1', 'x2', 'y2'].map((field) => number(body, field));
  return input(body, (rec) => device.swipe(rec, x1, y1, x2, y2));
});
route('POST', '/api/input/text', ({ body }) => {
  if (typeof body.value !== 'string' || !body.value) throw new ApiError(422, 'validation_error', 'value: Field required', { detail: [{ field: 'value', message: 'Field required' }] });
  return input(body, (rec) => device.type(rec, body.value));
});
route('POST', '/api/input/key', ({ body }) => {
  if (body.name == null || body.name === '') throw new ApiError(422, 'validation_error', 'name: Field required', { detail: [{ field: 'name', message: 'Field required' }] });
  return input(body, (rec) => device.key(rec, body.name));
});

// ------------------------------------------------------------------ windows
// On a real host these are scrcpy windows on the desktop the daemon runs on. The demo has
// no desktop, and says so instead of pretending a window opened somewhere.
const NO_DESKTOP = 'the demo has no desktop to open windows on; on a real host these are scrcpy windows next to the daemon';
route('GET', '/api/windows', () => ({ running: db.windows, available: { ok: false, reason: NO_DESKTOP, version: '' } }));
route('POST', '/api/windows/close-all', () => { db.windows = []; return { ok: true, running: [] }; });
route('POST', '/api/windows/{action}', ({ params, body }) => {
  if (!['open', 'close', 'grid'].includes(params.action)) throw new ApiError(404, 'not_found', 'no such API route');
  targets(body).forEach((id) => record(id));
  throw new ApiError(503, 'unavailable', NO_DESKTOP);
});

