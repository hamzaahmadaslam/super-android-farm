// A form generated from a JSON schema (the one the daemon publishes for its config),
// so every setting shows up with its description and nothing is listed twice.
//
// Supported: nested objects ($ref / properties), enums, booleans, integers and numbers
// (with their bounds), strings, lists of strings (one per line), and "anything else"
// as JSON text. Validation proper is the server's job; this only shapes the input.

import { h, uid } from '../dom.js';

function resolve(schema, root) {
  let node = schema;
  for (let guard = 0; node && guard < 10; guard++) {
    if (node.$ref) node = { ...node.$ref.replace(/^#\//, '').split('/').reduce((at, part) => at && at[part], root), ...without(node, '$ref') };
    else if (node.allOf && node.allOf.length === 1) node = { ...node.allOf[0], ...without(node, 'allOf') };
    else break;
  }
  // "X or null" (an optional value): edit it as X, an empty input means null.
  const options = node && node.anyOf ? node.anyOf.filter((option) => option.type !== 'null') : null;
  if (options && options.length === 1) {
    return { ...resolve({ ...options[0], ...without(node, 'anyOf') }, root), nullable: true };
  }
  return node || {};
}

function without(object, key) {
  const { [key]: _dropped, ...rest } = object;
  return rest;
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// Only what differs from `before`, nested the way PATCH /api/config expects.
export function diff(before, after) {
  if (after && before && typeof after === 'object' && typeof before === 'object' && !Array.isArray(after) && !Array.isArray(before)) {
    const out = {};
    for (const key of Object.keys(after)) {
      const part = diff(before[key], after[key]);
      if (part !== undefined) out[key] = part;
    }
    // The API takes a JSON merge patch: null is how an entry of a map is removed.
    for (const key of Object.keys(before)) {
      if (!(key in after)) out[key] = null;
    }
    return Object.keys(out).length ? out : undefined;
  }
  return same(before, after) ? undefined : after;
}

function leaf(schema, value, name, path) {
  const id = uid('fld');
  const helpId = `${id}-help`;
  const label = h('label', { for: id }, h('code', null, name));
  const help = schema.description && h('span', { class: 'help', id: helpId }, schema.description);
  const common = { id, name: path, 'aria-describedby': help ? helpId : null };
  const wrap = (control, read, extra = '') => ({
    el: h('div', { class: `field ${extra}`, dataset: { path } }, extra === 'check' ? [control, label] : [label, control], help),
    read,
  });

  if (schema.type === 'boolean') {
    const box = h('input', { ...common, type: 'checkbox', checked: Boolean(value) });
    return wrap(box, () => box.checked, 'check');
  }
  if (schema.enum) {
    const select = h('select', { ...common, value: value ?? '' },
      schema.nullable && h('option', { value: '' }, '(not set)'),
      schema.enum.map((option) => h('option', { value: option }, String(option))));
    return wrap(select, () => (select.value === '' && schema.nullable ? null : select.value));
  }
  if (schema.type === 'integer' || schema.type === 'number') {
    const input = h('input', {
      ...common, type: 'number', value: value ?? '',
      min: schema.minimum ?? schema.exclusiveMinimum, max: schema.maximum ?? schema.exclusiveMaximum,
      step: schema.type === 'integer' ? 1 : 'any',
    });
    // An emptied box on a required number means "leave it": the server would only reject null.
    return wrap(input, () => (input.value === '' ? (schema.nullable ? null : value) : Number(input.value)));
  }
  if (schema.type === 'string') {
    const input = h('input', { ...common, type: 'text', value: value ?? '', maxLength: schema.maxLength, spellcheck: false });
    return wrap(input, () => (input.value === '' && schema.nullable ? null : input.value));
  }
  if (schema.type === 'array' && schema.items && schema.items.type === 'string') {
    const area = h('textarea', { ...common, rows: 3, value: (value || []).join('\n'), placeholder: 'one per line' });
    return wrap(area, () => area.value.split('\n').map((line) => line.trim()).filter(Boolean), 'wide');
  }
  // Maps and anything unusual: edited as JSON, checked for syntax before it is sent.
  const area = h('textarea', { ...common, rows: 4, class: 'mono', value: JSON.stringify(value ?? null, null, 2) });
  return wrap(area, () => {
    try {
      return JSON.parse(area.value);
    } catch (error) {
      throw new Error(`${path} is not valid JSON: ${error.message}`);
    }
  }, 'wide');
}

function group(schema, value, root, path, paths) {
  const parts = [];
  const leaves = h('div', { class: 'fields' });
  const nested = [];
  for (const [name, raw] of Object.entries(schema.properties || {})) {
    const child = resolve(raw, root);
    const childPath = path ? `${path}.${name}` : name;
    const current = value ? value[name] : undefined;
    if (child.properties) {
      const inner = group(child, current, root, childPath, paths);
      nested.push(h('fieldset', null, h('legend', null, h('code', null, childPath)), inner.el));
      parts.push([name, inner.read]);
    } else {
      const field = leaf(child, current, name, childPath);
      paths.push(childPath);
      leaves.append(field.el);
      parts.push([name, field.read]);
    }
  }
  return {
    el: h('div', { class: 'stack' }, leaves.children.length ? leaves : null, nested),
    read: () => Object.fromEntries(parts.map(([name, read]) => [name, read()])),
  };
}

// Returns { sections: [{ name, title, el }], paths, changes() }. changes() is the patch
// of everything edited (undefined when nothing was) and throws on unparsable JSON;
// paths lists every field as a dotted name ("android.fps").
export function schemaForm(schema, value) {
  const sections = [];
  const readers = [];
  const paths = [];
  const top = { properties: {} };
  for (const [name, raw] of Object.entries(schema.properties || {})) {
    const child = resolve(raw, schema);
    if (!child.properties) { top.properties[name] = raw; continue; }
    const inner = group(child, value[name], schema, name, paths);
    // The schema's titles are class names ("ServerConfig"); the key is what config.yaml uses.
    sections.push({ name, title: name[0].toUpperCase() + name.slice(1), description: child.description || '', el: inner.el });
    readers.push([name, inner.read]);
  }
  if (Object.keys(top.properties).length) {
    const inner = group(top, value, schema, '', paths);
    sections.unshift({ name: 'general', title: 'General', description: '', el: inner.el });
    readers.push([null, inner.read]);
  }
  const read = () => {
    const out = {};
    for (const [name, reader] of readers) {
      if (name) out[name] = reader();
      else Object.assign(out, reader());
    }
    return out;
  };
  return { sections, paths, changes: () => diff(value, read()), read };
}
