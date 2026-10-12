// Backups: create, list, verify, restore, delete. No archive exists: a backup here is a
// row in a list, and restoring one makes a new pretend Android or changes an existing one.

import { accepted, created, media, route } from '../backend.js';
import { addBackup, ApiError, bad, conflict, ids, notFound, record, registerEffect, select, submit, UP } from '../farm.js';
import { backup, HOME, MAX_ANDROIDS, newSpec, nextAt } from '../seed.js';
import { clone, db, now } from '../store.js';

const NAME = /^(android-\d{1,4}|farm)\/\d{8}-\d{6}(-\d+)?$/;

function known(name) {
  if (typeof name !== 'string' || !NAME.test(name)) throw bad('bad backup name: use one as listed (android-<id>/<time> or farm/<time>)');
  const found = db.backups.find((b) => b.name === name);
  if (!found) throw notFound(`no backup named ${name}`);
  return found;
}

function schedule() {
  const cfg = db.config.backup;
  const out = { schedule: cfg.schedule.trim(), selector: cfg.schedule_selector, next_run: 0, last_run: 0, skipped: 0, error: '' };
  if (!out.schedule) return out;
  const fields = out.schedule.split(/\s+/);
  if (fields.length !== 5) return { ...out, error: 'a cron line has five fields: minute hour day-of-month month day-of-week' };
  return { ...out, next_run: /^\d+$/.test(fields[1]) ? nextAt(now(), Number(fields[1])) + (Number(fields[0]) || 0) * 60 : now() + 3600 };
}

route('GET', '/api/backups', () => ({ backups: db.backups, directory: `${HOME}/backups`, schedule: schedule() }));

registerEffect('backup', (id) => {
  const rec = db.instances[id];
  if (!rec) return { ok: false, detail: `there is no Android ${id}` };
  rec.activity = '';
  const entry = addBackup(id, 'manual');
  if (!entry) return { detail: 'skipped: no data yet' };
  const keep = db.config.backup.keep;
  const pruned = [];
  if (keep > 0) {
    const mine = db.backups.filter((b) => b.kind === 'android' && b.id === id);
    for (const old of mine.slice(keep)) { pruned.push(old.name); db.backups = db.backups.filter((b) => b !== old); }
  }
  return { detail: `backed up ${(entry.size / 1024 ** 3).toFixed(1)} GB${UP.has(rec.phase) && db.config.backup.stop_for_backup ? ' (paused for the copy, then carried on)' : ''}`, output: pruned.length ? { pruned } : undefined };
});

route('POST', '/api/backups', ({ body }) => {
  if (typeof body.selector !== 'string' || !body.selector) throw new ApiError(422, 'validation_error', 'selector: Field required', { detail: [{ field: 'selector', message: 'Field required' }] });
  const who = select(body.selector);
  if (!who.length) throw bad('the selector matches no Android');
  const plan = [];
  who.forEach((id, index) => {
    const has = Boolean(db.instances[id].dataVersion);
    if (has) plan.push({ after: 0.3 + index * 2.4, id, silent: true, effect: ['activity', id, 'backing up'] });
    plan.push({ after: (has ? 2.5 : 0.4) + index * 2.4, id, effect: ['backup', id] });
  });
  return accepted(submit('backup.manual', `back up ${who.length} Android(s)`, plan, { targets: who, output: { pruned: [] } }));
});

route('POST', '/api/backups/farm', ({ body }) => {
  const secrets = body.include_secrets == null ? db.config.backup.include_secrets : Boolean(body.include_secrets);
  let entry = backup('farm', null, '', now(), 16_000 + ids().length * 900, '', { includes_secrets: secrets });
  for (let n = 2; db.backups.some((b) => b.name === entry.name); n++) entry = { ...entry, name: `${entry.name.replace(/-\d+$/, '')}-${n}` };
  db.backups.unshift(entry);
  return created({ archive: entry.name, dir: entry.dir, includes_secrets: secrets });
});

route('POST', '/api/backups/verify', ({ body }) => ({ archive: known(body.archive).name, ok: true }));

route('POST', '/api/backups/prune', () => {
  const keep = db.config.backup.keep;
  const removed = [];
  if (keep > 0) {
    const seen = {};
    db.backups = db.backups.filter((b) => {
      if (b.kind !== 'android') return true;
      seen[b.id] = (seen[b.id] || 0) + 1;
      if (seen[b.id] <= keep) return true;
      removed.push(b.name);
      return false;
    });
  }
  return { removed };
});

registerEffect('restore', (name, target, keepNetwork) => {
  const entry = db.backups.find((b) => b.name === name);
  if (!entry) return { ok: false, detail: `no backup named ${name}` };
  if (target != null) {
    const rec = db.instances[target];
    if (!rec) return { ok: false, detail: `there is no Android ${target}` };
    const safety = db.config.backup.auto_before_risky ? addBackup(target, 'before restore') : null;
    rec.dataVersion = entry.android_version || rec.dataVersion;
    rec.phase = 'stopped';
    delete rec.ui;
    return { detail: `restored from ${name}`, output: { id: target, archive: name, new: false, safety_backup: safety ? safety.dir : '' } };
  }
  if (ids().length >= MAX_ANDROIDS) return { ok: false, detail: `the demo holds at most ${MAX_ANDROIDS} Androids` };
  let next = 1;
  while (db.instances[next]) next += 1;
  const original = db.instances[entry.id];
  const spec = newSpec(next, now(), { name: `${entry.instance_name || `Android ${entry.id}`} (restored)`, profile: original ? original.spec.device.profile : undefined });
  if (original) {
    Object.assign(spec, { tags: [...original.spec.tags], gpu_mode: original.spec.gpu_mode, android_version: original.spec.android_version });
    // The copy carries the original's identity, so it comes up with no network unless asked.
    spec.device = { ...clone(original.spec.device) };
    spec.network = keepNetwork ? clone(original.spec.network) : { ...clone(original.spec.network), mode: 'none' };
  } else if (!keepNetwork) spec.network = { ...spec.network, mode: 'none' };
  db.instances[next] = {
    spec, phase: 'stopped', activity: '', lastError: '', lastStart: null, mode: '', dataVersion: entry.android_version || '13', apps: original ? clone(original.apps) : {},
    values: {}, netSince: 0, restarts: 0, publicChecked: false, device: { locale: 'en-US', timezone: '', dark_mode: false, animations: false, font_scale: 1, proxy: '' },
  };
  return { detail: `restored from ${name}`, output: { id: next, archive: name, new: true, safety_backup: '' } };
});

registerEffect('restoreFarm', () => ({ output: { restored: ['config.yaml', 'instances', 'automation'], skipped: [], archive_secrets: false } }));

route('POST', '/api/backups/restore', ({ body }) => {
  const entry = known(body.archive);
  if (entry.kind === 'farm') {
    if (body.id != null) throw bad('a farm backup restores settings, not one Android: leave `id` out');
    return accepted(submit('backup.restore-farm', `restore farm settings from ${entry.name}`, [{ after: 0.6, say: 'reading the archive' }, { after: 1.8, effect: ['restoreFarm'], say: 'restored (the demo keeps its settings as they are)' }]));
  }
  let target = null;
  if (body.id != null) {
    const rec = record(body.id);
    target = rec.spec.id;
    if (UP.has(rec.phase)) throw conflict(`Android ${target} is running; stop it before restoring over it`);
    if (!body.force) throw new ApiError(409, 'needs_force', `restoring over Android ${target} replaces its data; use force (a backup of it is taken first)`);
  }
  const title = `restore ${entry.name} ${target != null ? `over Android ${target}` : 'as a new Android'}`;
  return accepted(submit('backup.restore', title, [{ after: 0.5, say: 'unpacking' }, { after: 3.4, effect: ['restore', entry.name, target, Boolean(body.keep_network)], say: 'restored' }],
    { targets: target != null ? [target] : [], exclusive: true }));
});

route('DELETE', '/api/backups', ({ body, query }) => {
  const entry = known(body.archive || query.archive || '');
  db.backups = db.backups.filter((b) => b !== entry);
  return { deleted: entry.name };
});

// Served by transport.js as a small text file that says what it is.
media('/api/backups/download', 'backup');

