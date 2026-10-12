// The panel's icons: small line drawings on a 24x24 grid, written by hand as path data
// and built as DOM nodes (no icon font, no sprite request, nothing parsed as HTML).
// Every icon is decoration: the text or the aria-label next to it carries the meaning.

const SVG = 'http://www.w3.org/2000/svg';

// A circle as path data, so that an icon is always one <path>.
const ring = (cx, cy, r) => `M${cx - r} ${cy}a${r} ${r} 0 1 0 ${2 * r} 0a${r} ${r} 0 1 0 ${-2 * r} 0`;
const SPEAKER = 'M4 9.5v5h3.5l4.5 4v-13l-4.5 4z';
const TRAY = 'M4.5 15.5v3A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5v-3';

const PATHS = {
  // navigation
  dashboard: 'M4 4h7v9H4z M13 4h7v5h-7z M13 11h7v9h-7z M4 15h7v5H4z',
  device: 'M8 2.5h8a2 2 0 0 1 2 2v15a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2v-15a2 2 0 0 1 2-2z M10.5 18.5h3',
  devices: 'M5 4h7a1.5 1.5 0 0 1 1.5 1.5v13A1.5 1.5 0 0 1 12 20H5a1.5 1.5 0 0 1-1.5-1.5v-13A1.5 1.5 0 0 1 5 4z M16.5 7H19a1.5 1.5 0 0 1 1.5 1.5v10A1.5 1.5 0 0 1 19 20h-2.5',
  control: `${ring(12, 12, 1.5)} M7.8 16.2a6 6 0 0 1 0-8.4 M16.2 7.8a6 6 0 0 1 0 8.4 M4.9 19.1a10 10 0 0 1 0-14.2 M19.1 4.9a10 10 0 0 1 0 14.2`,
  apps: 'M12 2.8l8 4.4v9.6l-8 4.4-8-4.4V7.2z M4.3 7.4L12 11.6l7.7-4.2 M12 11.6v9.4',
  network: `${ring(12, 12, 9)} M3 12h18 M12 3c2.6 2.5 3.9 5.5 3.9 9s-1.3 6.5-3.9 9c-2.6-2.5-3.9-5.5-3.9-9S9.4 5.5 12 3z`,
  automation: 'M13 2.5L4.5 13.5H11l-1 8 8.5-11H12z',
  media: 'M3.5 7.5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-8a2 2 0 0 1-2-2z M15.5 10.5l5-3v9l-5-3',
  backups: 'M3.5 4.5h17v4h-17z M5 8.5v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-9 M9.5 12.5h5',
  system: 'M7 7h10v10H7z M10 10h4v4h-4z M9.5 3.5V7 M14.5 3.5V7 M9.5 17v3.5 M14.5 17v3.5 M3.5 9.5H7 M3.5 14.5H7 M17 9.5h3.5 M17 14.5h3.5',
  settings: `M4 7h10 M18 7h2 ${ring(16, 7, 2)} M4 12h2 M10 12h10 ${ring(8, 12, 2)} M4 17h8 M16 17h4 ${ring(14, 17, 2)}`,
  // shell
  search: `${ring(10.5, 10.5, 6.5)} M15.3 15.3L20 20`,
  tasks: 'M9.5 6.5H20 M9.5 12H20 M9.5 17.5H20 M4 6.5l1.2 1.2 2-2.2 M4 12l1.2 1.2 2-2.2 M4 17.5l1.2 1.2 2-2.2',
  sun: `${ring(12, 12, 4)} M12 2.5v2 M12 19.5v2 M2.5 12h2 M19.5 12h2 M5.3 5.3l1.4 1.4 M17.3 17.3l1.4 1.4 M5.3 18.7l1.4-1.4 M17.3 6.7l1.4-1.4`,
  moon: 'M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5z',
  monitor: 'M3.5 5h17v11h-17z M9 20h6 M12 16v4',
  logout: 'M10 4.5H6A1.5 1.5 0 0 0 4.5 6v12A1.5 1.5 0 0 0 6 19.5h4 M14.5 8l4 4-4 4 M18.5 12H9.5',
  menu: 'M4 6.5h16 M4 12h16 M4 17.5h16',
  collapse: 'M12 6l-6 6 6 6 M18.5 6l-6 6 6 6',
  'chevron-left': 'M14.5 6l-6 6 6 6',
  'chevron-right': 'M9.5 6l6 6-6 6',
  'chevron-down': 'M6 9.5l6 6 6-6',
  keyboard: 'M3.5 6.5h17v11h-17z M7 10h.1 M10.3 10h.1 M13.6 10h.1 M17 10h.1 M7 14h10',
  // actions
  plus: 'M12 5v14 M5 12h14',
  play: 'M7.5 4.8v14.4a.6.6 0 0 0 .9.5l11.6-7.2a.6.6 0 0 0 0-1L8.4 4.3a.6.6 0 0 0-.9.5z',
  stop: 'M8 6.5h8A1.5 1.5 0 0 1 17.5 8v8a1.5 1.5 0 0 1-1.5 1.5H8A1.5 1.5 0 0 1 6.5 16V8A1.5 1.5 0 0 1 8 6.5z',
  pause: 'M8 5.5v13 M16 5.5v13',
  restart: 'M19.5 12a7.5 7.5 0 1 1-2.6-5.7 M19.5 4.5V9H15',
  recreate: 'M4.5 11a7.5 7.5 0 0 1 13.2-4 M19.5 4.5v3.8h-3.8 M19.5 13a7.5 7.5 0 0 1-13.2 4 M4.5 19.5v-3.8h3.8',
  trash: 'M4.5 7h15 M9.5 7V4.5h5V7 M6.5 7l.8 11.6A1.5 1.5 0 0 0 8.8 20h6.4a1.5 1.5 0 0 0 1.5-1.4L17.5 7 M10 11v5.5 M14 11v5.5',
  more: `${ring(6, 12, 1)} ${ring(12, 12, 1)} ${ring(18, 12, 1)}`,
  copy: 'M9 9h10.5v10.5H9z M15 6V4.5H4.5V15H6',
  upload: `M12 15.5v-11 M7.5 9L12 4.5 16.5 9 ${TRAY}`,
  download: `M12 4.5v11 M7.5 11l4.5 4.5 4.5-4.5 ${TRAY}`,
  open: 'M13.5 4.5h6v6 M19.5 4.5l-8 8 M17.5 14v4a1.5 1.5 0 0 1-1.5 1.5H6A1.5 1.5 0 0 1 4.5 18V8A1.5 1.5 0 0 1 6 6.5h4',
  grid: 'M4.5 4.5h6v6h-6z M13.5 4.5h6v6h-6z M4.5 13.5h6v6h-6z M13.5 13.5h6v6h-6z',
  rows: 'M4 6h16 M4 10h16 M4 14h16 M4 18h16',
  filter: 'M4 5.5h16l-6 7V18l-4 2v-7.5z',
  tag: 'M4 4h7l8.8 8.8a1.2 1.2 0 0 1 0 1.7l-5.3 5.3a1.2 1.2 0 0 1-1.7 0L4 11z M7.8 7.8v.1',
  camera: `M4 8h3l1.5-2.5h7L17 8h3v11H4z ${ring(12, 13.5, 3)}`,
  terminal: 'M3.5 5h17v14h-17z M7 9.5l3 2.5-3 2.5 M12.5 15H16',
  folder: 'M3.5 6.5h6l2 2.5h9v10h-17z',
  flag: 'M5.5 21V4 M5.5 4.5h11l-2 4 2 4h-11',
  record: `${ring(12, 12, 8.5)} ${ring(12, 12, 3)}`,
  // state
  check: 'M5 12.5l4.5 4.5L19 7.5',
  x: 'M6 6l12 12 M18 6L6 18',
  alert: 'M12 4.2L21 19.5H3z M12 10v4.5 M12 17.2v.1',
  info: `${ring(12, 12, 9)} M12 11v5.5 M12 7.6v.1`,
  help: `${ring(12, 12, 9)} M9.4 9.5a2.7 2.7 0 1 1 3.9 2.4c-.8.5-1.3 1-1.3 2 M12 16.9v.1`,
  clock: `${ring(12, 12, 9)} M12 7v5l3.2 2`,
  spinner: 'M12 3.5a8.5 8.5 0 1 0 8.5 8.5',
  dot: ring(12, 12, 3.5),
  shield: 'M12 3l7.5 3v5.5c0 4.6-3 8-7.5 9.5-4.5-1.5-7.5-4.9-7.5-9.5V6z',
  lock: 'M6 11h12v9H6z M8.5 11V8a3.5 3.5 0 0 1 7 0v3',
  approval: 'M12 3l7.5 3v5.5c0 4.6-3 8-7.5 9.5-4.5-1.5-7.5-4.9-7.5-9.5V6z M8.7 11.8l2.4 2.4 4.3-4.6',
  plug: 'M9 3v5 M15 3v5 M6.5 8h11v3.5a5.5 5.5 0 0 1-11 0z M12 17v4',
  layers: 'M12 3.5l8.5 4.5-8.5 4.5L3.5 8z M3.5 12l8.5 4.5 8.5-4.5 M3.5 16l8.5 4.5 8.5-4.5',
  gauge: 'M4.5 16.5a8 8 0 1 1 15 0 M12 13l3.5-4.5',
  memory: 'M4 8h16v8H4z M8 8v8 M12 8v8 M16 8v8 M6.5 16v2.5 M17.5 16v2.5',
  inbox: 'M3.5 13.5l3-8h11l3 8V18a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 18z M3.5 13.5h5a3.5 3.5 0 0 0 7 0h5',
  // device buttons
  back: 'M15.5 5.5L7 12l8.5 6.5z',
  home: ring(12, 12, 6.5),
  recents: 'M8 6.5h8A1.5 1.5 0 0 1 17.5 8v8a1.5 1.5 0 0 1-1.5 1.5H8A1.5 1.5 0 0 1 6.5 16V8A1.5 1.5 0 0 1 8 6.5z',
  'volume-down': `${SPEAKER} M16 12h5`,
  'volume-up': `${SPEAKER} M16 12h5 M18.5 9.5v5`,
  power: 'M12 3.5v8 M7 6.6a7.5 7.5 0 1 0 10 0',
};

export const hasIcon = (name) => Object.hasOwn(PATHS, name);

// An <svg> for `name`. `extra` adds classes ("lg", "spin").
export function icon(name, extra = '') {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', `icon ${extra}`.trim());
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.75');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const path = document.createElementNS(SVG, 'path');
  path.setAttribute('d', PATHS[name] || PATHS.dot);
  svg.append(path);
  return svg;
}

// Any other SVG element, for the charts.
export function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value != null && value !== false) el.setAttribute(key, String(value));
  }
  el.append(...children.flat().filter(Boolean));
  return el;
}
