// After `next build`: move every inline <script> of the exported pages into a file.
//
// The static export writes small inline scripts into each page (the data the framework
// reads when it starts in the browser). The site's content policy allows scripts from its
// own origin only and no inline script, so each of them is written to
// out/_next/static/inline/<hash>.js and the page loads that file instead. The scripts run
// in the same order as before: a plain <script src> without `async` blocks the parser
// exactly where the inline one stood.
//
// The check at the end fails the build if any page still has an inline script or an
// inline event handler, so the policy in vercel.json and the pages cannot drift apart.

import { createHash } from 'node:crypto';
import { mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const OUT = join(dirname(fileURLToPath(import.meta.url)), '..', 'out');
const INLINE = join(OUT, '_next', 'static', 'inline');
// Every <script ...>...</script>; the attributes decide whether it is inline.
const SCRIPT = /<script\b([^>]*)>([\s\S]*?)<\/script>/g;

function* htmlFiles(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      if (relative(OUT, path) !== 'panel') yield* htmlFiles(path);        // the panel has no inline script to begin with
    } else if (name.endsWith('.html')) yield path;
  }
}

mkdirSync(INLINE, { recursive: true });
const written = new Set();
let pages = 0;
let moved = 0;
for (const path of htmlFiles(OUT)) {
  const html = readFileSync(path, 'utf8');
  const next = html.replace(SCRIPT, (whole, attrs, body) => {
    if (/\bsrc\s*=/.test(attrs) || !body.trim()) return whole;
    // Data blocks (type="application/json" and the like) are not executed and may stay.
    const type = /\btype\s*=\s*["']?([^"'\s>]+)/.exec(attrs);
    if (type && !/^(module|text\/javascript|application\/javascript)$/i.test(type[1])) return whole;
    const name = `${createHash('sha256').update(body).digest('hex').slice(0, 20)}.js`;
    if (!written.has(name)) { writeFileSync(join(INLINE, name), body); written.add(name); }
    moved += 1;
    return `<script${attrs.replace(/\s*\bnonce\s*=\s*("[^"]*"|'[^']*'|\S+)/, '')} src="/_next/static/inline/${name}"></script>`;
  });
  if (next !== html) writeFileSync(path, next);
  pages += 1;
  const left = [...next.matchAll(SCRIPT)].filter(([, attrs, body]) => !/\bsrc\s*=/.test(attrs) && body.trim() && !/\btype\s*=\s*["']?application\/(ld\+)?json/i.test(attrs));
  const handlers = next.match(/<[a-z][^>]*\son[a-z]+\s*=/i);
  const styles = next.match(/<style\b|<[a-z][^>]*\sstyle\s*=/i);
  if (left.length || handlers || styles) {
    console.error(`postbuild: ${relative(OUT, path)} still needs a looser content policy: `
      + [left.length && 'an inline script', handlers && 'an inline event handler', styles && 'an inline style'].filter(Boolean).join(', '));
    process.exit(1);
  }
}
console.log(`postbuild: ${pages} pages, ${moved} inline scripts moved into ${written.size} files`);
