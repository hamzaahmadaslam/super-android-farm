// Serves demo/out the way the deployed site is served: the same response headers as
// vercel.json, a trailing slash on every page address, and 404.html for anything else.
// For checking a build locally under the real content policy. Standard library only.
//
//   npm run preview            http://127.0.0.1:18700
//   PORT=18750 npm run preview
//
// The `source` patterns in vercel.json are used as regular expressions, anchored at both
// ends. That is right for the patterns the file has (plain text, and groups in
// parentheses); it is not a full reading of every pattern Vercel accepts.

import { createReadStream, existsSync, readFileSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, '..', 'out');
const config = JSON.parse(readFileSync(join(HERE, '..', '..', 'vercel.json'), 'utf8'));
const rules = (config.headers || []).map((rule) => ({ test: new RegExp(`^${rule.source}$`), headers: rule.headers }));
const TYPES = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.ico': 'image/x-icon', '.txt': 'text/plain; charset=utf-8',
  '.woff2': 'font/woff2', '.map': 'application/json', '.xml': 'application/xml',
};

if (!existsSync(join(OUT, 'index.html'))) {
  console.error('serve: demo/out does not exist yet; run `npm run build` first');
  process.exit(1);
}

function send(res, status, path, file) {
  const headers = { 'content-type': TYPES[extname(file)] || 'application/octet-stream' };
  for (const rule of rules) if (rule.test.test(path)) for (const { key, value } of rule.headers) headers[key.toLowerCase()] = value;
  res.writeHead(status, headers);
  createReadStream(file).pipe(res);
}

const server = createServer((req, res) => {
  let path;
  try { path = decodeURIComponent(new URL(req.url, 'http://localhost').pathname); } catch { path = '/'; }
  const query = req.url.includes('?') ? req.url.slice(req.url.indexOf('?')) : '';
  const local = normalize(join(OUT, path));
  if (!local.startsWith(OUT)) { res.writeHead(400).end(); return; }
  const isDir = existsSync(local) && statSync(local).isDirectory();
  if (isDir && !path.endsWith('/')) { res.writeHead(308, { location: `${path}/${query}` }).end(); return; }
  const file = isDir ? join(local, 'index.html') : local;
  if (existsSync(file) && statSync(file).isFile()) send(res, 200, path, file);
  else if (config.trailingSlash && !path.endsWith('/') && !extname(path) && existsSync(join(`${local}`, 'index.html'))) res.writeHead(308, { location: `${path}/${query}` }).end();
  else send(res, 404, path, join(OUT, '404.html'));
});

const port = Number(process.env.PORT) || 18700;
server.listen(port, '127.0.0.1', () => console.log(`serve: demo/out on http://127.0.0.1:${port} with the headers of vercel.json`));
