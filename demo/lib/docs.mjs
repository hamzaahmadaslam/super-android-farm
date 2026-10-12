// The documentation pages, read at build time from generated/docs.json (written by
// scripts/sync-panel.mjs from the Markdown files of the repository).

import { readFileSync } from 'node:fs';
import { join, posix } from 'node:path';
import { Lexer } from 'marked';

const data = JSON.parse(readFileSync(join(process.cwd(), 'generated', 'docs.json'), 'utf8'));

export const REPOSITORY = data.repository;
export const pages = data.pages;
const bySource = new Map(pages.map((page) => [page.source, page]));
const files = new Set(data.files);

export const pageBySlug = (slug) => pages.find((page) => page.slug === slug);
export const hrefOf = (page) => (page.slug ? `/docs/${page.slug}/` : '/docs/');

// The plain words of a run of inline tokens: what a heading says, without the markup.
export function plain(tokens) {
  return (tokens || []).map((token) => (token.tokens ? plain(token.tokens) : token.type === 'html' ? '' : token.text || '')).join('');
}

// A heading's anchor the way GitHub writes it: lower case, punctuation dropped, spaces to dashes.
const anchor = (text, strict) => text.toLowerCase().trim().replace(strict ? /[^a-z0-9 -]/g : /[^\p{L}\p{N} _-]/gu, '').replace(/ /g, '-');

// Every heading among `tokens` with its id. A second heading with the same words gets "-1".
export function headingsIn(tokens) {
  const seen = new Map();
  const out = [];
  const walk = (list) => {
    for (const token of list) {
      if (token.type === 'heading') {
        const words = plain(token.tokens);
        const base = anchor(words, false);
        const n = seen.get(base) || 0;
        seen.set(base, n + 1);
        out.push({ depth: token.depth, text: words, id: n ? `${base}-${n}` : base, loose: anchor(words, true) });
      } else if (token.type === 'blockquote' || token.type === 'list_item') walk(token.tokens || []);
      else if (token.type === 'list') walk(token.items || []);
    }
  };
  walk(tokens);
  return out;
}

const headingCache = new Map();
export function headingsOf(page) {
  if (!headingCache.has(page.source)) headingCache.set(page.source, headingsIn(Lexer.lex(page.text)));
  return headingCache.get(page.source);
}

// The id a link fragment means on a page. Links are written against GitHub's rule or
// against the simpler one the repository's link check uses; both find the same heading.
function fragment(page, wanted) {
  if (!wanted) return '';
  const headings = headingsOf(page);
  const want = decodeURIComponent(wanted).toLowerCase();
  const found = headings.find((h) => h.id === want) || headings.find((h) => h.loose === want);
  if (!found) throw new Error(`${page.source} has no heading for #${wanted}`);
  return `#${found.id}`;
}

// Where a link written in `from` (a repository path such as docs/GPU.md) leads on the site.
// A link to another document becomes that page; a link to any other file of the repository
// goes to GitHub; anything that resolves to nothing fails the build.
export function resolve(from, href) {
  if (/^[a-z][a-z0-9+.-]*:/i.test(href)) return { href, external: true };
  const [path, hash] = href.split('#');
  const self = bySource.get(from);
  if (!path) return { href: fragment(self, hash) };
  const target = posix.normalize(posix.join(posix.dirname(from), path));
  const page = bySource.get(target);
  if (page) return { href: hrefOf(page) + fragment(page, hash) };
  if (target.startsWith('docs/img/')) return { href: `/${target}` };
  const folder = `${target.replace(/\/$/, '')}/`;
  if (files.has(target) || files.has(folder)) return { href: `${REPOSITORY}/${files.has(target) ? 'blob' : 'tree'}/HEAD/${target.replace(/\/$/, '')}${hash ? `#${hash}` : ''}`, external: true };
  throw new Error(`${from} links to ${href}, which is not in the repository`);
}

// Pages in the order the documentation index lists them; the rest follow by title.
export function ordered() {
  const index = pages.find((page) => page.slug === '');
  const order = index ? [...index.text.matchAll(/\]\(([^)\s#]+\.md)\)/g)].map((m) => posix.normalize(posix.join('docs', m[1]))) : [];
  const rank = (page) => { const at = order.indexOf(page.source); return at < 0 ? order.length : at; };
  return pages.filter((page) => page.slug).sort((a, b) => rank(a) - rank(b) || a.title.localeCompare(b.title));
}
