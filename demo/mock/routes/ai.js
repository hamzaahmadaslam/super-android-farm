// Model profiles, the Assistant and the Pilot.
//
// The demo has no language model and makes no connection, so these routes behave like a
// farm on which no model answers. Profiles can be added, edited and listed (they are
// settings). Testing one reports that nothing was contacted. A conversation or a Pilot
// session cannot start; the answer says why. The lists are real lists that stay empty.
//
// To make these pages do more, replace the refusals below with a canned conversation
// (events as superfarm/ai/assistant.py writes them) or a scripted Pilot session.

import { route } from '../backend.js';
import { ApiError, notFound } from '../farm.js';
import { db } from '../store.js';

const A = '/api/ai';
const WHY = 'The demo is not connected to any language model, so the Assistant and the Pilot cannot run here. On a real farm, add a model profile first (see docs/AI.md).';
const noModel = () => new ApiError(400, 'model_not_configured', WHY);

function models() {
  const ai = db.config.ai;
  const names = Object.keys(ai.models || {}).sort();
  const chosen = ai.default_model && ai.models[ai.default_model] ? ai.default_model : names.length === 1 ? names[0] : '';
  return names.map((name) => {
    const profile = ai.models[name];
    return {
      name, provider: profile.provider, base_url: profile.base_url, model: profile.model, vision: profile.vision, tools: profile.tools, timeout: profile.timeout,
      default: name === chosen, implicit: false, api_key_secret: profile.api_key_secret, key_set: profile.api_key_secret ? false : null,
    };
  });
}

route('GET', `${A}/models`, () => ({ models: models(), default: db.config.ai.default_model }));
route('POST', `${A}/models/{name}/test`, ({ params }) => {
  const model = models().find((entry) => entry.name === params.name);
  if (!model) throw new ApiError(400, 'model_not_configured', `no model profile named '${params.name.slice(0, 32)}'`);
  return {
    probe: {
      ok: false, provider: model.provider, base_url: model.base_url, model: model.model, seconds: 0, available: [],
      checks: [{ id: 'reach', ok: false, kind: 'unreachable', ms: 0, detail: 'The demo never opens a connection, so this server was not contacted. On a real farm this check calls the address above.' }],
    },
  };
});

// --------------------------------------------------------------- assistant
route('GET', `${A}/conversations`, () => ({ conversations: [] }));
route('POST', `${A}/conversations`, () => { throw noModel(); });
route('GET', `${A}/conversations/{id}`, () => { throw notFound('no such conversation'); });
route('POST', `${A}/conversations/{id}/messages`, () => { throw noModel(); });
route('POST', `${A}/conversations/{id}/cancel`, () => ({ ok: true, cancelled: false }));
route('DELETE', `${A}/conversations/{id}`, () => { throw notFound('no such conversation'); });

// ------------------------------------------------------------------- pilot
route('POST', `${A}/pilot`, () => { throw noModel(); });
route('GET', `${A}/pilot/sessions`, () => ({ sessions: [] }));
route('GET', `${A}/pilot/sessions/{id}`, ({ params }) => { throw notFound(`no pilot session '${params.id.slice(0, 40)}'`); });
route('POST', `${A}/pilot/sessions/{id}/cancel`, () => ({ ok: true, cancelled: false }));
route('POST', `${A}/pilot/sessions/{id}/devices/{instance}/resolve`, ({ params }) => { throw new ApiError(409, 'conflict', `Android ${params.instance} is not waiting for an answer in pilot session ${params.id.slice(0, 40)}`); });
route('POST', `${A}/pilot/sessions/{id}/confirm`, () => { throw notFound('no such pilot session'); });
route('POST', `${A}/pilot/{dataset}/train`, () => { throw noModel(); });
route('GET', `${A}/pilot/{dataset}/summary`, ({ params }) => { throw notFound(`no pilot recordings for '${params.dataset.slice(0, 64)}'`); });
