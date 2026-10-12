// Copies into the demo what has its one source elsewhere in the repository:
//
//   superfarm/web/          -> public/panel/          the real panel, file for file
//   demo/mock/              -> public/panel/js/demo/  the backend that runs in the page
//   docs/*.md, root *.md    -> generated/docs.json    the text of the documentation pages
//   docs/img/               -> public/docs/img/
//   superfarm/web/css/tokens.css -> generated/tokens.css   the colours and sizes of the site
//
// One file of the panel is then replaced: js/transport.js, the module every request,
// socket and media address goes through. In its place goes a line that re-exports the
// demo's transport. Nothing else of the panel is changed, which is why a change to the
// panel reaches the demo at its next build.
//
// Node's standard library only; it runs before `next dev` and `next build`.

import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const DEMO = join(dirname(fileURLToPath(import.meta.url)), '..');
const ROOT = join(DEMO, '..');
const WEB = join(ROOT, 'superfarm', 'web');
const MOCK = join(DEMO, 'mock');
const PANEL = join(DEMO, 'public', 'panel');
const GENERATED = join(DEMO, 'generated');
const REPOSITORY = 'https://github.com/hamzaahmadaslam/super-android-farm';
// Documents kept at the top of the repository that the site shows as pages too.
const ROOT_PAGES = ['README.md', 'DISCLAIMER.md', 'ACCEPTABLE_USE.md', 'SECURITY.md', 'CONTRIBUTING.md', 'CHANGELOG.md', 'AGENTS.md'];

const read = (path) => readFileSync(path, 'utf8');
function fail(message) {
  console.error(`sync-panel: ${message}`);
  process.exit(1);
}

if (!existsSync(join(WEB, 'index.html'))) fail(`the panel was not found at ${relative(ROOT, WEB)}; run this from a full checkout`);

// ------------------------------------------------------------------- panel
rmSync(PANEL, { recursive: true, force: true });
cpSync(WEB, PANEL, { recursive: true });

// The seam is a contract: the demo's transport must offer exactly what the panel's does.
const exportsOf = (source) => [...source.matchAll(/^export (?:async )?(?:const|function) (\w+)/gm)].map((m) => m[1]).sort();
const real = exportsOf(read(join(WEB, 'js', 'transport.js')));
const fake = exportsOf(read(join(MOCK, 'transport.js')));
if (!real.length || real.join() !== fake.join()) {
  fail(`superfarm/web/js/transport.js exports [${real}] but demo/mock/transport.js exports [${fake}]. They must match: `
    + 'the demo replaces that one file. See docs/DEMO.md.');
}
cpSync(MOCK, join(PANEL, 'js', 'demo'), { recursive: true });
writeFileSync(join(PANEL, 'js', 'transport.js'),
  '// Written by demo/scripts/sync-panel.mjs: in the demo the panel talks to a backend inside the page.\n'
  + "export * from './demo/transport.js';\n");

// Every module the copied panel imports must be there. A file left out of the repository
// (an ignore rule that matches too much) would otherwise build fine and fail in the browser.
const importsOf = (source) => [...source.matchAll(/(?:^|\n)\s*(?:import|export)\b[^'"`;]*?\bfrom\s*['"]([^'"]+)['"]|(?:^|\n)\s*import\s*['"]([^'"]+)['"]/g)].map((m) => m[1] || m[2]);
const todo = [join(PANEL, 'js', 'main.js')];
const seen = new Set();
while (todo.length) {
  const file = todo.pop();
  if (seen.has(file)) continue;
  seen.add(file);
  if (!existsSync(file)) fail(`${relative(PANEL, file)} is imported by the panel but is not in the build. Is it committed?`);
  for (const target of importsOf(read(file))) todo.push(join(dirname(file), target));
}

// The demo's own stylesheet, and a note for crawlers: the page is found, not listed.
const index = join(PANEL, 'index.html');
const html = read(index);
if (!html.includes('</head>')) fail('index.html of the panel has no </head> to add the demo stylesheet before');
writeFileSync(index, html.replace('</head>',
  '<link rel="stylesheet" href="js/demo/demo.css">\n<meta name="robots" content="noindex, nofollow">\n</head>'));

// ----------------------------------------------------------- documentation
const pages = [];
const add = (source, slug) => {
  const text = read(join(ROOT, source));
  const title = (/^#\s+(.+)$/m.exec(text) || [null, slug])[1].replace(/`/g, '').trim();
  pages.push({ slug, source, title, text });
};
const docs = join(ROOT, 'docs');
if (existsSync(docs)) {
  for (const name of readdirSync(docs).filter((entry) => entry.endsWith('.md')).sort()) {
    add(`docs/${name}`, name === 'README.md' ? '' : name.slice(0, -3).toLowerCase().replace(/_/g, '-'));
  }
}
for (const name of ROOT_PAGES) {
  if (!existsSync(join(ROOT, name))) continue;
  const slug = name.slice(0, -3).toLowerCase().replace(/_/g, '-');
  if (pages.some((page) => page.slug === slug)) fail(`${name} and docs/${name} would both be the page /docs/${slug}/`);
  add(name, slug);
}

// Every other file a document links to (a source file, a licence) is linked on GitHub;
// the list says which of those targets exist, so a broken link fails the build.
const files = new Set();
for (const page of pages) {
  for (const [, target] of page.text.matchAll(/\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g)) {
    if (/^[a-z][a-z0-9+.-]*:/i.test(target) || target.startsWith('#')) continue;
    const path = join(dirname(page.source), target.split('#')[0]).replace(/\\/g, '/');
    if (!path.startsWith('..') && existsSync(join(ROOT, path))) files.add(statSync(join(ROOT, path)).isDirectory() ? `${path.replace(/\/$/, '')}/` : path);
  }
}

rmSync(GENERATED, { recursive: true, force: true });
mkdirSync(GENERATED, { recursive: true });
writeFileSync(join(GENERATED, 'docs.json'), JSON.stringify({ repository: REPOSITORY, pages, files: [...files].sort() }));
cpSync(join(WEB, 'css', 'tokens.css'), join(GENERATED, 'tokens.css'));

const images = join(DEMO, 'public', 'docs', 'img');
rmSync(join(DEMO, 'public', 'docs'), { recursive: true, force: true });
if (existsSync(join(docs, 'img'))) cpSync(join(docs, 'img'), images, { recursive: true });

// The banner inside the panel links to the disclaimer page when the site has one.
const disclaimer = pages.some((page) => page.slug === 'disclaimer');
writeFileSync(join(PANEL, 'js', 'demo', 'site.js'),
  '// Written by demo/scripts/sync-panel.mjs.\n'
  + `export default ${JSON.stringify({ repository: REPOSITORY, docs: '/docs/', disclaimer: disclaimer ? '/docs/disclaimer/' : `${REPOSITORY}/blob/HEAD/DISCLAIMER.md`, home: '/' }, null, 2)};\n`);

console.log(`sync-panel: panel copied (${readdirSync(join(PANEL, 'js', 'pages')).length} pages), ${pages.length} documents, ${files.size} linked files`);
