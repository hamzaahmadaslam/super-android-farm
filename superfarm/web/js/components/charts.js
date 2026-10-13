// The dashboard's small charts, drawn as SVG and plain boxes: a ring gauge, a meter
// with a sparkline of its recent values, and a segmented bar. Each is built once and
// then updated in place, so a poll every few seconds moves a bar instead of redrawing
// the card. Every chart states its value as text for screen readers.

import { h, uid } from '../dom.js';
import { svg } from '../icons.js';

const level = (ratio) => (ratio >= 0.95 ? 'fail' : ratio >= 0.8 ? 'warn' : '');

// The last `limit` samples of something that is polled.
export function series(limit = 60) {
  const values = [];
  return {
    values,
    push(value) {
      if (value == null || Number.isNaN(Number(value))) return;
      values.push(Number(value));
      if (values.length > limit) values.shift();
    },
  };
}

const WIDTH = 120;
const HEIGHT = 34;

function sparkPath(values, max) {
  const top = max > 0 ? max : Math.max(1, ...values);
  const list = values.length > 1 ? values : [values[0] ?? 0, values[0] ?? 0];
  const points = list.map((value, i) => [
    (i / (list.length - 1)) * WIDTH,
    HEIGHT - 2 - (Math.min(Math.max(value, 0), top) / top) * (HEIGHT - 4),
  ]);
  const line = points.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`).join(' ');
  return { line, area: `${line} L${WIDTH} ${HEIGHT} L0 ${HEIGHT} Z` };
}

// { el, set(values, max, label) }
export function sparkline() {
  const area = svg('path', { class: 'area' });
  const line = svg('path', { class: 'line' });
  const el = svg('svg', { class: 'spark', viewBox: `0 0 ${WIDTH} ${HEIGHT}`, preserveAspectRatio: 'none', role: 'img' }, area, line);
  return {
    el,
    set(values, max, label, kind = '') {
      const path = sparkPath(values, max);
      area.setAttribute('d', path.area);
      line.setAttribute('d', path.line);
      el.setAttribute('class', `spark ${kind}`.trim());
      el.setAttribute('aria-label', label);
    },
  };
}

// A labelled bar with an optional sparkline beside it. { el, set(value, max, caption, history?) }
export function liveMeter(label, { spark = true, unit = '' } = {}) {
  const fill = h('span', { class: 'fill' });
  const caption = h('span');
  const track = h('div', { class: 'track', role: 'meter', 'aria-label': label, 'aria-valuemin': '0' }, fill);
  const meter = h('div', { class: 'meter' }, h('div', { class: 'meter-head' }, h('span', null, label), caption), track);
  const chart = spark ? sparkline() : null;
  const el = chart ? h('div', { class: 'meter-row' }, meter, chart.el) : meter;
  return {
    el,
    set(value, max, text, history) {
      const ratio = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
      const kind = level(ratio);
      fill.style.width = `${(ratio * 100).toFixed(1)}%`;
      meter.className = `meter ${kind}`;
      caption.textContent = text;
      track.setAttribute('aria-valuemax', String(max));
      track.setAttribute('aria-valuenow', String(value));
      track.setAttribute('aria-valuetext', text);
      if (chart && history) {
        const low = Math.min(...history);
        const high = Math.max(...history);
        chart.set(history, max, history.length > 1
          ? `${label}, recent samples: between ${Math.round(low)}${unit} and ${Math.round(high)}${unit}` : `${label}: not enough samples yet`, kind);
      }
    },
  };
}

const SIZE = 148;
const RADIUS = 62;
const LENGTH = 2 * Math.PI * RADIUS;

// A ring that fills with value / max. { el, set(value, max, sub, text, kind?) }
export function gauge() {
  const id = uid('ring');
  const ring = svg('circle', {
    class: 'ring-value', cx: SIZE / 2, cy: SIZE / 2, r: RADIUS, 'stroke-width': 10, stroke: `url(#${id})`,
    'stroke-dasharray': LENGTH.toFixed(1), 'stroke-dashoffset': LENGTH.toFixed(1),
  });
  const picture = svg('svg', { viewBox: `0 0 ${SIZE} ${SIZE}`, 'aria-hidden': 'true' },
    svg('defs', {}, svg('linearGradient', { id, x1: 0, y1: 0, x2: 1, y2: 1 },
      svg('stop', { offset: '0%', class: 'stop-a' }), svg('stop', { offset: '100%', class: 'stop-b' }))),
    svg('circle', { class: 'ring-track', cx: SIZE / 2, cy: SIZE / 2, r: RADIUS, 'stroke-width': 10 }),
    ring);
  const number = h('span', { class: 'gauge-value' });
  const under = h('span', { class: 'gauge-sub' });
  const el = h('div', { class: 'gauge', role: 'meter', 'aria-valuemin': '0' }, picture, h('div', { class: 'gauge-label' }, number, under));
  return {
    el,
    set(value, max, sub, text, kind) {
      const ratio = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
      ring.setAttribute('stroke-dashoffset', (LENGTH * (1 - ratio)).toFixed(1));
      el.className = `gauge ${kind ?? level(ratio)}`;
      number.textContent = String(value);
      under.textContent = sub;
      el.setAttribute('aria-valuemax', String(Math.max(max, value)));
      el.setAttribute('aria-valuenow', String(value));
      el.setAttribute('aria-valuetext', text);
      el.setAttribute('aria-label', text);
    },
  };
}

// parts: [{ kind: ok|info|warn|fail|idle, value, label }]. Empty parts take no room.
export function segmentedBar(parts, label) {
  const shown = parts.filter((part) => part.value > 0);
  const bar = h('div', {
    class: 'segbar', role: 'img',
    'aria-label': `${label}: ${shown.map((part) => `${part.value} ${part.label}`).join(', ') || 'nothing yet'}`,
  }, shown.map((part) => {
    const segment = h('span', { class: `seg-${part.kind}`, title: `${part.value} ${part.label}` });
    segment.style.flexGrow = String(part.value);
    return segment;
  }));
  return bar;
}

const DONUT = 132;
const DONUT_R = 52;
const DONUT_LEN = 2 * Math.PI * DONUT_R;

// A ring cut into parts, with a number in the middle. parts: [{ kind, value, label }];
// kinds are ok | info | warn | fail | idle. { el, set(parts, centre, sub) }
export function donut() {
  const segments = svg('g');
  const picture = svg('svg', { viewBox: `0 0 ${DONUT} ${DONUT}`, 'aria-hidden': 'true' },
    svg('circle', { class: 'donut-track', cx: DONUT / 2, cy: DONUT / 2, r: DONUT_R }), segments);
  const number = h('span', { class: 'gauge-value' });
  const under = h('span', { class: 'gauge-sub' });
  const el = h('div', { class: 'donut', role: 'img' }, picture, h('div', { class: 'gauge-label' }, number, under));
  return {
    el,
    set(parts, centre, sub) {
      const shown = parts.filter((part) => part.value > 0);
      const total = shown.reduce((sum, part) => sum + part.value, 0);
      // A small gap between parts, none when one part is the whole ring.
      const gap = shown.length > 1 ? 3 : 0;
      let offset = 0;
      segments.replaceChildren(...shown.map((part) => {
        const length = (part.value / total) * DONUT_LEN;
        const drawn = Math.max(1, length - gap);
        const arc = svg('circle', {
          class: `donut-part part-${part.kind}`, cx: DONUT / 2, cy: DONUT / 2, r: DONUT_R,
          'stroke-dasharray': `${drawn.toFixed(1)} ${(DONUT_LEN - drawn).toFixed(1)}`, 'stroke-dashoffset': (-offset).toFixed(1),
        });
        offset += length;
        return arc;
      }));
      number.textContent = String(centre);
      under.textContent = sub;
      el.setAttribute('aria-label', `${centre} ${sub}: ${shown.map((part) => `${part.value} ${part.label}`).join(', ') || 'nothing yet'}`);
    },
  };
}
