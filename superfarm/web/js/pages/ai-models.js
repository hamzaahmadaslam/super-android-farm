// Model profiles: where a language model lives and what it can do. The Assistant and the
// Pilot use them; with none configured nothing on the farm ever calls a model.
//
// A profile is part of the config (ai.models.<name>), so adding, editing and removing
// one is a PATCH of the config, with the form generated from the schema the daemon
// publishes. An API key is never typed here: keys do not cross the API. The page shows
// the command that stores one on the farm's own machine.

import { get, patch, post } from '../api.js';
import { badge, button, emptyState, field, skeleton } from '../common.js';
import { confirmDialog, openDialog } from '../components/dialog.js';
import { schemaForm } from '../components/form.js';
import { attempt, fail, ok } from '../components/toast.js';
import { copyText, h, replace } from '../dom.js';
import { icon } from '../icons.js';

const CHECKS = { reachable: 'Server answers', model: 'Model is there', reply: 'Model replies', tools: 'Tool call there and back', vision: 'Reads a picture' };
const NAME = /^[a-z0-9][a-z0-9_-]{0,31}$/;

const keyCommand = (name) => `sf ai models key ${name}`;

function command(text) {
  return h('span', { class: 'row' }, h('code', null, text),
    button('Copy', () => copyText(text).then(() => ok('Command copied.'), () => fail(new Error('Copying is not available in this browser; select the text instead.'))), 'small', 'copy'));
}

function keyState(model) {
  if (model.key_set == null) return badge('', 'no key needed');
  return model.key_set ? badge('ok', 'key stored') : badge('fail', 'key missing');
}

// The result of POST /api/ai/models/{name}/test, check by check.
function probeView(probe) {
  return h('div', { class: 'probe', role: 'status' },
    h('div', { class: 'row' }, badge(probe.ok ? 'ok' : 'fail', probe.ok ? 'every check passed' : 'a check failed'),
      h('span', { class: 'small muted' }, `${probe.provider} at ${probe.base_url}${probe.model ? `, model ${probe.model}` : ''} · ${probe.seconds}s in all`)),
    h('ul', { class: 'probe-list' }, probe.checks.map((check) => h('li', null,
      badge(check.ok ? 'ok' : 'fail', check.ok ? 'ok' : 'failed'),
      h('div', { class: 'grow' }, h('strong', null, CHECKS[check.id] || check.id), ' ', h('span', { class: 'num small muted' }, `${(check.ms / 1000).toFixed(1)}s`),
        h('div', { class: check.ok ? 'small muted' : 'small' }, check.detail))))),
    (probe.available || []).length > 0 && h('p', { class: 'small muted' }, `The server lists: ${probe.available.slice(0, 12).join(', ')}${probe.available.length > 12 ? '…' : ''}`));
}

export async function modelsTab(panelEl) {
  const box = h('div', { class: 'stack' }, skeleton(4));
  let schema = null;
  let models = [];
  const results = new Map();           // profile name -> node showing its last test

  const profileSchema = () => ({ properties: schema.$defs.ModelProfile.properties, $defs: schema.$defs });
  const save = (models_patch, done, extra = {}) => patch('/api/config', { patch: { ai: { models: models_patch, ...extra } } }).then(() => { ok(done); return load(); });

  async function edit(model) {
    const adding = !model;
    // A new profile starts from the defaults the schema states (tools on, timeouts and so on).
    const defaults = Object.fromEntries(Object.entries(schema.$defs.ModelProfile.properties)
      .filter(([, property]) => 'default' in property).map(([key, property]) => [key, property.default]));
    const current = adding ? { ...defaults, provider: 'ollama' } : (await get('/api/config')).config.ai.models[model.name];
    const form = schemaForm(profileSchema(), current);
    const name = h('input', { type: 'text', spellcheck: false, autocomplete: 'off', maxLength: 32, placeholder: 'local', value: adding ? '' : model.name, disabled: !adding });
    const makeDefault = h('input', { type: 'checkbox', checked: adding && models.filter((m) => !m.implicit).length === 0 });
    await openDialog({
      title: adding ? 'Add a model profile' : `Edit the profile "${model.name}"`,
      icon: 'plug',
      wide: true,
      body: [
        h('p', { class: 'muted' }, 'A profile says where a model server is and what the model can do. The farm ships no model and downloads none.'),
        h('div', { class: 'fields' },
          field('Profile name', name, 'Lower-case letters, digits, - and _. "automation" is taken.', { path: 'name' }),
          field('Use it when no profile is named (default)', makeDefault)),
        form.sections.map((section) => section.el),
        h('p', { class: 'small muted' }, 'API keys are not entered here and never cross the API. Name the secret in ', h('code', null, 'api_key_secret'),
          ', then store the key on the farm’s machine with the command shown in the list.'),
      ],
      actions: [{
        label: adding ? 'Add profile' : 'Save', kind: 'primary',
        run: async () => {
          const wanted = name.value.trim();
          if (adding && !NAME.test(wanted)) throw Object.assign(new Error('Give the profile a name: lower-case letters, digits, - and _.'), { detail: [{ field: 'name', message: 'Lower-case letters, digits, - and _ (at most 32).' }] });
          const body = adding ? form.read() : form.changes();
          if (!adding && !body && !makeDefault.checked) throw new Error('Nothing was changed.');
          await save({ [wanted]: body || {} }, adding ? `Profile "${wanted}" added.` : `Profile "${wanted}" saved.`,
            makeDefault.checked ? { default_model: wanted } : {});
        },
      }],
    });
  }

  async function remove(model) {
    if (!await confirmDialog({
      title: `Remove the profile "${model.name}"?`, danger: true, confirm: 'Remove this profile',
      message: `ai.models.${model.name} is deleted from the config. Conversations that use it can no longer be continued, and a stored key stays in the farm’s secrets until removed there.`,
    })) return;
    await attempt(() => save({ [model.name]: null }, `Profile "${model.name}" removed.`, model.default ? { default_model: '' } : {}));
  }

  // Really test one profile. It can take a minute while a model loads: the button is busy
  // for all of it (the shared guard) and the row says so.
  async function test(model) {
    const out = results.get(model.name);
    replace(out, h('p', { class: 'small', role: 'status' }, icon('spinner', 'spin'), ' Testing with real requests. The first answer can take up to a minute while the model loads…'));
    try {
      const data = await post(`/api/ai/models/${encodeURIComponent(model.name)}/test`);
      replace(out, probeView(data.probe));
    } catch (error) {
      replace(out, h('div', { class: 'banner fail', role: 'alert' }, icon('alert'), h('div', { class: 'grow' }, h('strong', null, 'The test could not run. '), error.message)));
    }
  }

  function row(model) {
    if (!results.has(model.name)) results.set(model.name, h('div'));
    return h('article', { class: 'model card', 'aria-label': `Model profile ${model.name}` },
      h('div', { class: 'card-head' },
        h('h3', null, icon('plug'), h('code', null, model.name), model.default && badge('ok', 'default'), model.implicit && badge('', 'from automation.llm')),
        h('button', { type: 'button', class: 'btn', dataset: { action: `model-test:${model.name}` }, onclick: () => test(model) }, icon('play'), 'Test'),
        !model.default && button('Make default', () => attempt(() => patch('/api/config', { patch: { ai: { default_model: model.name } } }).then(load), `"${model.name}" is now the default model.`), '', 'check'),
        !model.implicit && button('Edit…', () => edit(model), '', 'settings'),
        !model.implicit && button('Remove…', () => remove(model), 'danger', 'trash')),
      h('dl', { class: 'kv' },
        h('dt', null, 'Server'), h('dd', null, h('code', null, model.base_url), ' ', h('span', { class: 'muted' }, `(${model.provider})`)),
        h('dt', null, 'Model'), h('dd', null, model.model ? h('code', null, model.model) : h('span', { class: 'muted' }, 'the first one the server lists')),
        h('dt', null, 'Can'), h('dd', null, h('span', { class: 'row' },
          badge(model.tools ? 'ok' : '', model.tools ? 'call tools' : 'no tool calls'), badge(model.vision ? 'ok' : '', model.vision ? 'read pictures' : 'no pictures'),
          !model.tools && h('span', { class: 'small muted' }, 'The Assistant needs tool calls; the Pilot works without.'))),
        h('dt', null, 'API key'), h('dd', null, h('div', { class: 'stack tight' },
          h('span', { class: 'row' }, keyState(model), model.api_key_secret && h('span', { class: 'small muted' }, 'secret ', h('code', null, model.api_key_secret))),
          model.key_set === false && h('div', { class: 'small' }, 'Store it on the machine that runs the farm (it is asked for without being shown): ', command(keyCommand(model.name))),
          model.key_set === true && h('div', { class: 'small muted' }, 'To replace it: ', command(keyCommand(model.name)))))),
      results.get(model.name));
  }

  async function load() {
    const data = await get('/api/ai/models');
    models = data.models;
    replace(box,
      h('div', { class: 'row between' },
        h('p', { class: 'muted grow' }, 'Named profiles the Assistant and the Pilot can use. "Test" makes real requests: the server answers, it has the model, the model replies, and a tool call goes there and back.'),
        button('Add a profile', () => edit(null), 'primary', 'plus')),
      models.length === 0 ? emptyState('plug', 'No model is configured',
        'Add a profile for a model server you run (an Ollama server, or anything that speaks the OpenAI chat-completions API). Until then the Assistant and the Pilot stay off and nothing on the farm calls a model.',
        button('Add a profile', () => edit(null), 'primary', 'plus'))
        : models.map(row),
      h('p', { class: 'small muted' }, icon('lock'), ' Keys never go in config.yaml and never cross the API, so this page cannot store one. On the farm’s machine: ', h('code', null, 'sf ai models key <profile>'), '.'));
  }

  panelEl.append(box);
  schema = await get('/api/config/schema');
  await load();
}

// For pages that only need the list (the Assistant's and the Pilot's pickers).
export const loadModels = () => get('/api/ai/models');
