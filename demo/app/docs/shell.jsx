import Link from 'next/link';
import { headingsOf, hrefOf, ordered, pages, REPOSITORY } from '../../lib/docs.mjs';
import { Markdown } from '../../lib/markdown.jsx';

// One documentation page: the list of documents, the text, and where its source is.
export function DocPage({ page }) {
  const index = pages.find((entry) => entry.slug === '');
  const sections = headingsOf(page).filter((heading) => heading.depth === 2);
  return (
    <div className="wrap docs">
      <aside className="docs-nav">
        <details open>
          <summary>Documents</summary>
          <nav aria-label="Documents">
            <ul>
              {index ? <li><Link href="/docs/" aria-current={page.slug === '' ? 'page' : undefined}>Index</Link></li> : null}
              {ordered().map((entry) => (
                <li key={entry.slug}><Link href={hrefOf(entry)} aria-current={entry.slug === page.slug ? 'page' : undefined}>{entry.title}</Link></li>
              ))}
            </ul>
          </nav>
        </details>
      </aside>
      <article>
        <nav className="crumbs" aria-label="Breadcrumb">
          <Link href="/">Home</Link><span aria-hidden="true">/</span>
          {page.slug ? <><Link href="/docs/">Docs</Link><span aria-hidden="true">/</span><span>{page.title}</span></> : <span>Docs</span>}
        </nav>
        {sections.length > 2 ? (
          <details className="toc">
            <summary>On this page</summary>
            <ul>{sections.map((heading) => <li key={heading.id}><a href={`#${heading.id}`}>{heading.text}</a></li>)}</ul>
          </details>
        ) : null}
        <Markdown page={page} />
        <p className="source">
          This page is <a href={`${REPOSITORY}/blob/HEAD/${page.source}`} rel="noopener noreferrer"><code>{page.source}</code></a> in the repository.
        </p>
      </article>
    </div>
  );
}
