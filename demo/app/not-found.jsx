import Link from 'next/link';
import { DEMO } from '../lib/site.mjs';

export const metadata = { title: 'Page not found' };

export default function NotFound() {
  return (
    <section className="wrap block lost">
      <p className="eyebrow">404</p>
      <h1>This page does not exist</h1>
      <p className="lead">The address may be mistyped, or the document was renamed.</p>
      <div className="actions">
        <Link className="button primary" href="/">Home</Link>
        <Link className="button" href="/docs/">Documentation</Link>
        <a className="button" href={DEMO}>Live demo</a>
      </div>
    </section>
  );
}
