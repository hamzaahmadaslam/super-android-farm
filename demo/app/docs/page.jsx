import Link from 'next/link';
import { hrefOf, ordered, pages } from '../../lib/docs.mjs';
import { DocPage } from './shell.jsx';

export const metadata = { title: 'Documentation' };

export default function DocsIndex() {
  const index = pages.find((page) => page.slug === '');
  if (index) return <DocPage page={index} />;
  // No docs/README.md: a plain list of what there is.
  return (
    <div className="wrap block">
      <h1>Documentation</h1>
      <ul>{ordered().map((page) => <li key={page.slug}><Link href={hrefOf(page)}>{page.title}</Link></li>)}</ul>
    </div>
  );
}
