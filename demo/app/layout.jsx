import Link from 'next/link';
import '../generated/tokens.css';
import './site.css';
import { pageBySlug } from '../lib/docs.mjs';
import { DEMO, NAME, REPOSITORY } from '../lib/site.mjs';

export const metadata = {
  title: { default: `${NAME}: a farm of Android containers on one Linux machine`, template: `%s · ${NAME}` },
  description: 'Super Farm runs many Android instances as Docker containers on one Linux machine and controls them from a web panel, a command line and an HTTP API. This site has the documentation and a demo of the panel with a fake backend.',
  icons: { icon: '/panel/img/logo.svg' },
};

export const viewport = { colorScheme: 'dark light' };

export default function RootLayout({ children }) {
  const policy = ['disclaimer', 'acceptable-use', 'security'].map(pageBySlug).filter(Boolean);
  return (
    <html lang="en">
      <body>
        <a className="skip" href="#content">Skip to content</a>
        <header className="site-head">
          <div className="wrap bar">
            <Link className="brand" href="/"><img src="/panel/img/logo.svg" alt="" width="28" height="28" /><span>{NAME}</span></Link>
            <nav aria-label="Site">
              <a className="pill" href={DEMO}>Live demo</a>
              <Link href="/docs/">Docs</Link>
              <a href={REPOSITORY} rel="noopener noreferrer">GitHub</a>
            </nav>
          </div>
        </header>
        <main id="content">{children}</main>
        <footer className="site-foot">
          <div className="wrap foot">
            <p>
              {NAME} is free software under the Apache License 2.0. Copyright 2026 Hamza Ahmad Aslam.
            </p>
            <nav aria-label="Policies">
              {policy.map((page) => <Link key={page.slug} href={`/docs/${page.slug}/`}>{page.title}</Link>)}
              <a href={`${REPOSITORY}/blob/HEAD/LICENSE`} rel="noopener noreferrer">Licence</a>
              <a href={REPOSITORY} rel="noopener noreferrer">Source</a>
            </nav>
          </div>
        </footer>
      </body>
    </html>
  );
}
