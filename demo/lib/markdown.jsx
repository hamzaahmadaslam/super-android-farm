// Markdown to React elements, at build time.
//
// The text is split into tokens by `marked` and every token is turned into an element
// here. No HTML string is ever handed to the page: HTML written inside a document is shown
// as the text it is (the one exception is <br>, which becomes a line break), so a document
// cannot bring a script, a style or a frame into the site.

import Link from 'next/link';
import { Lexer } from 'marked';
import { headingsIn, headingsOf, resolve } from './docs.mjs';

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', copy: '©', mdash: '—', ndash: '–', hellip: '…', rarr: '→', larr: '←', times: '×' };
const decode = (text) => String(text).replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (whole, name) => {
  if (name[0] === '#') return String.fromCodePoint(name[1].toLowerCase() === 'x' ? parseInt(name.slice(2), 16) : Number(name.slice(1)));
  return ENTITIES[name.toLowerCase()] ?? whole;
});

function Anchor({ link, title, children }) {
  if (link.external) return <a href={link.href} title={title || undefined} rel="noopener noreferrer">{children}</a>;
  // A plain <a> for a jump inside the page and for files; the router takes the pages.
  if (link.href.startsWith('#') || !link.href.endsWith('/') && !link.href.includes('/#')) return <a href={link.href} title={title || undefined}>{children}</a>;
  return <Link href={link.href} title={title || undefined}>{children}</Link>;
}

function inline(tokens, ctx) {
  return (tokens || []).map((token, index) => {
    const key = index;
    switch (token.type) {
      case 'text': return token.tokens ? <span key={key}>{inline(token.tokens, ctx)}</span> : decode(token.text);
      case 'escape': return decode(token.text);
      case 'codespan': return <code key={key}>{decode(token.text)}</code>;
      case 'strong': return <strong key={key}>{inline(token.tokens, ctx)}</strong>;
      case 'em': return <em key={key}>{inline(token.tokens, ctx)}</em>;
      case 'del': return <del key={key}>{inline(token.tokens, ctx)}</del>;
      case 'br': return <br key={key} />;
      case 'link': return <Anchor key={key} link={resolve(ctx.source, token.href)} title={token.title}>{inline(token.tokens, ctx)}</Anchor>;
      case 'image': {
        const link = resolve(ctx.source, token.href);
        // Pictures come from the repository only; one that points elsewhere is shown as a link.
        if (link.external) return <a key={key} href={link.href} rel="noopener noreferrer">{token.text || link.href}</a>;
        return <img key={key} src={link.href} alt={token.text || ''} title={token.title || undefined} loading="lazy" decoding="async" />;
      }
      case 'html': return /^<br\s*\/?>$/i.test(token.text.trim()) ? <br key={key} /> : /^<!--/.test(token.text) ? null : token.text;
      default: return token.text ? decode(token.text) : null;
    }
  });
}

function blocks(tokens, ctx) {
  return (tokens || []).map((token, index) => {
    const key = index;
    switch (token.type) {
      case 'space': case 'def': return null;
      case 'heading': {
        const Tag = `h${Math.min(6, token.depth)}`;
        const heading = ctx.headings[ctx.next++];
        if (token.depth === 1) return <Tag key={key} id={heading.id}>{inline(token.tokens, ctx)}</Tag>;
        return <Tag key={key} id={heading.id}>{inline(token.tokens, ctx)}<a className="anchor" href={`#${heading.id}`} aria-label={`Link to the section "${heading.text}"`}>#</a></Tag>;
      }
      case 'paragraph': return <p key={key}>{inline(token.tokens, ctx)}</p>;
      case 'text': return <p key={key}>{token.tokens ? inline(token.tokens, ctx) : decode(token.text)}</p>;
      case 'code': return <pre key={key} tabIndex={0} data-lang={token.lang || undefined}><code>{token.text}</code></pre>;
      case 'blockquote': return <blockquote key={key}>{blocks(token.tokens, ctx)}</blockquote>;
      case 'hr': return <hr key={key} />;
      case 'list': {
        const Tag = token.ordered ? 'ol' : 'ul';
        return (
          <Tag key={key} start={token.ordered && token.start !== 1 && token.start !== '' ? token.start : undefined}>
            {token.items.map((item, n) => (
              <li key={n} className={item.task ? 'task' : undefined}>
                {item.task ? <input type="checkbox" checked={Boolean(item.checked)} disabled readOnly aria-label={item.checked ? 'done' : 'not done'} /> : null}
                {item.loose ? blocks(item.tokens.filter((t) => t.type !== 'checkbox'), ctx)
                  : item.tokens.filter((t) => t.type !== 'checkbox').map((t, m) => (t.type === 'text' ? <span key={m}>{t.tokens ? inline(t.tokens, ctx) : decode(t.text)}</span> : blocks([t], ctx)))}
              </li>
            ))}
          </Tag>
        );
      }
      case 'table': return (
        <div key={key} className="table-scroll" tabIndex={0} role="region" aria-label="Table">
          <table>
            <thead><tr>{token.header.map((cell, n) => <th key={n} scope="col" className={cell.align ? `align-${cell.align}` : undefined}>{inline(cell.tokens, ctx)}</th>)}</tr></thead>
            <tbody>{token.rows.map((row, r) => <tr key={r}>{row.map((cell, n) => <td key={n} className={cell.align ? `align-${cell.align}` : undefined}>{inline(cell.tokens, ctx)}</td>)}</tr>)}</tbody>
          </table>
        </div>
      );
      case 'html': return /^<!--/.test(token.text.trim()) ? null : <pre key={key} tabIndex={0}><code>{token.text.trim()}</code></pre>;
      default: return null;
    }
  });
}

// `page` is an entry of generated/docs.json; `skipTitle` leaves out the first heading
// when the page prints its own.
export function Markdown({ page, text, skipTitle = false }) {
  const tokens = Lexer.lex(text ?? page.text);
  const ctx = { source: page.source, headings: text === undefined ? headingsOf(page) : headingsIn(tokens), next: 0 };
  if (skipTitle && tokens[0] && tokens[0].type === 'heading' && tokens[0].depth === 1) { tokens.shift(); ctx.next = 1; }
  return <div className="prose">{blocks(tokens, ctx)}</div>;
}
