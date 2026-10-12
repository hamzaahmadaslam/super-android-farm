// Backups: the safety net. Create, verify, restore as a new Android (never touching the
// original), back up the farm's settings, and schedule it all.

import { del, get, patch, post, query } from '../api.js';
import { badge, button, card, empty, field, loadInstances, option, pageHead } from '../common.js';
import { confirmDialog, openDialog } from '../components/dialog.js';
import { dataTable } from '../components/table.js';
import { finished } from '../components/tasks.js';
import { attempt, fail, ok } from '../components/toast.js';
import { bytes, h, when } from '../dom.js';

export async function render(root, _params, scope) {
  const selector = h('input', { type: 'text', value: 'all', spellcheck: false });
  const secrets = h('input', { type: 'checkbox' });
  const cron = h('input', { type: 'text', placeholder: '0 4 * * *', spellcheck: false });
  const cronSelector = h('input', { type: 'text', spellcheck: false });
  const keep = h('input', { type: 'number', min: 0 });
  const scheduleNote = h('p', { class: 'small muted' });
  const where = h('p', { class: 'small muted' });

  async function restore(backup) {
    const instances = await loadInstances();
    const target = h('select', null, option('', 'As a new Android (the original is not touched)'),
      backup.kind === 'android' ? instances.map((i) => option(String(i.id), `Over Android ${i.id} · ${i.name} (it must be stopped)`)) : null);
    const keepNetwork = h('input', { type: 'checkbox' });
    const force = h('input', { type: 'checkbox' });
    const farm = backup.kind === 'farm';
    await openDialog({
      title: `Restore ${backup.name}`,
      body: farm ? [
        h('p', null, 'Restores the farm’s settings, Android definitions and automation data. Existing Androids are not overwritten unless forced.'),
        field('Overwrite what already exists', force),
      ] : [
        field('Restore', target),
        field('Keep the copy’s network on', keepNetwork, 'Off by default when the original still exists, so one device identity is never online twice.'),
        field('Force (needed to restore over an existing Android; a backup of it is taken first)', force),
      ],
      actions: [{ label: 'Restore', kind: 'primary', run: async () => {
        const body = { archive: backup.name, force: force.checked };
        if (!farm) {
          if (target.value) body.id = Number(target.value);
          body.keep_network = keepNetwork.checked;
        }
        const data = await post('/api/backups/restore', body);
        finished(data.task).then((task) => {
          if (task.state === 'done' && task.output && task.output.id != null) ok(`Restored as Android ${task.output.id}.`, { action: { label: 'Open', run: () => { location.hash = `#/android/${task.output.id}`; } } });
          load().catch(() => {});
        });
      } }],
    });
  }

  const table = dataTable({
    label: 'Backups',
    key: (b) => b.name,
    columns: [
      { title: 'Backup', cell: (b) => h('code', null, b.name) },
      { title: 'Of', cell: (b) => (b.kind === 'farm' ? ['Farm settings', b.includes_secrets ? [' ', badge('warn', 'contains secrets')] : null] : `Android ${b.id}${b.instance_name ? ` · ${b.instance_name}` : ''}`) },
      { title: 'Made', cell: (b) => [when(b.created), b.reason ? h('div', { class: 'small muted' }, b.reason) : null] },
      { title: 'Android', cell: (b) => b.android_version },
      { title: 'Size', class: 'num', cell: (b) => bytes(b.size) },
      { title: 'State', cell: (b) => (!b.present ? badge('fail', 'data file missing') : b.kind === 'farm' || b.consistent ? badge('ok', 'complete') : badge('warn', 'taken while running')) },
      { title: '', class: 'actions', cell: (b) => [
        button('Verify', () => attempt(async () => {
          const result = await post('/api/backups/verify', { archive: b.name });
          if (result.ok) ok(`${b.name}: checksums match.`);
          else fail(`${b.name}: verification FAILED. Do not rely on this backup.`);
        }), 'small'), ' ',
        button('Restore…', () => restore(b), 'small primary'), ' ',
        b.includes_secrets ? null : h('a', { class: 'btn small', href: `/api/backups/download${query({ archive: b.name })}`, download: '' }, 'Download'), ' ',
        button('Delete', async () => {
          if (!await confirmDialog({ title: 'Delete this backup?', message: `${b.name} is removed from the backup folder. This cannot be undone.`, confirm: 'Delete', danger: true })) return;
          await attempt(() => del(`/api/backups${query({ archive: b.name })}`), 'Backup deleted.');
          await load();
        }, 'small danger'),
      ] },
    ],
    empty: empty('No backups yet', 'Back up one or more Androids above. A backup holds the Android’s data, its settings and checksums of every file.'),
  });

  async function load() {
    const data = await get('/api/backups');
    table.update(data.backups);
    where.textContent = `Backups are stored in ${data.directory}.`;
    const s = data.schedule;
    scheduleNote.textContent = s.error ? `The schedule is not valid: ${s.error}`
      : s.schedule ? `Next run: ${when(s.next_run) || 'at the next tick'}. Last run: ${when(s.last_run) || 'never'}.${s.skipped ? ` Skipped ${s.skipped} time(s) because a backup was still running.` : ''}`
        : 'No schedule: backups are made only when asked for.';
    return data;
  }

  root.append(
    pageHead('Backups', 'Restoring as a new Android never touches the original.'),
    h('div', { class: 'stack' },
      h('div', { class: 'grid cols-2' },
        card('Back up Androids',
          h('form', { class: 'stack', onsubmit: (event) => {
            event.preventDefault();
            attempt(async () => {
              const data = await post('/api/backups', { selector: selector.value.trim() });
              await finished(data.task);
              await load();
            });
          } },
          field('Androids (selector)', selector, 'With backup.stop_for_backup on (the default), a running Android is stopped while its data is copied and started again.'),
          h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn primary' }, 'Back up now')))),
        card('Back up the farm’s settings',
          h('p', { class: 'small muted' }, 'Config, every Android’s definition, automation data and the list of VPN profile names. Small and quick.'),
          field('Include secrets (VPN credentials, tokens). Such a backup cannot be downloaded here.', secrets),
          h('div', { class: 'row' }, button('Back up settings', () => attempt(async () => {
            const result = await post('/api/backups/farm', secrets.checked ? { include_secrets: true } : {});
            await load();
            return result;
          }, (result) => `Saved ${result.archive}.`), 'primary')))),
      card('Schedule',
        h('form', { class: 'fields', onsubmit: (event) => {
          event.preventDefault();
          attempt(async () => {
            await patch('/api/config', { patch: { backup: { schedule: cron.value.trim(), schedule_selector: cronSelector.value.trim() || 'all', keep: Number(keep.value) || 0 } } });
            await load();
          }, 'Schedule saved.');
        } },
        field('When (cron line)', cron, 'minute hour day-of-month month day-of-week. "0 4 * * *" is every day at 04:00. Empty switches it off.'),
        field('Which Androids (selector)', cronSelector),
        field('Backups kept per Android', keep, '0 keeps all.'),
        h('div', { class: 'field' }, h('span', null, '\u00a0'), h('button', { type: 'submit', class: 'btn' }, 'Save schedule'))),
        scheduleNote),
      h('section', { class: 'card' },
        h('div', { class: 'card-head' }, h('h2', null, 'Existing backups'), button('Refresh', () => attempt(load), 'small')),
        where, table.el)));

  const [, config] = await Promise.all([load(), get('/api/config')]);
  cron.value = config.config.backup.schedule;
  cronSelector.value = config.config.backup.schedule_selector;
  keep.value = config.config.backup.keep;
  scope.on(window, 'sf:changed', () => load().catch(() => {}));
}
