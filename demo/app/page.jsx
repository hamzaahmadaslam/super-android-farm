import Link from 'next/link';
import { pageBySlug } from '../lib/docs.mjs';
import { Markdown } from '../lib/markdown.jsx';
import { DEMO, DEMO_NOTE, NAME, REPOSITORY } from '../lib/site.mjs';

// The words of this page are the README's own, read at build time, so that the site
// cannot say more than the project does. The sentences below are used only when the
// README is missing or has lost a section.
const FALLBACK = `${NAME} runs many Android instances as Docker containers on one Linux machine and controls them from a web panel, a command line (\`sf\`) and an HTTP API.`;

function section(text, title) {
  const start = text.search(new RegExp(`^## ${title}\\s*$`, 'm'));
  if (start < 0) return '';
  const rest = text.slice(start).replace(/^## .*\n/, '');
  const end = rest.search(/^## /m);
  return (end < 0 ? rest : rest.slice(0, end)).trim();
}

export default function Home() {
  const readme = pageBySlug('readme');
  const text = readme ? readme.text : '';
  const intro = (/^# .*\n+([^\n#!\[|][^\n]*)/m.exec(text) || [null, FALLBACK])[1];
  const shots = [...text.matchAll(/!\[([^\]]*)\]\((docs\/img\/[^)\s]+)\)/g)].map((m) => ({ alt: m[1], src: `/${m[2]}` }));
  const features = section(text, 'Features');
  const status = section(text, 'Status');
  const has = (slug) => Boolean(pageBySlug(slug));
  return (
    <>
      <section className="hero">
        <div className="wrap">
          <p className="eyebrow">Open source, Apache-2.0</p>
          <h1>{NAME}</h1>
          {readme ? <Markdown page={readme} text={intro} /> : <p className="lead">{FALLBACK}</p>}
          <div className="actions">
            <a className="button primary" href={DEMO}>Open the live demo</a>
            <Link className="button" href="/docs/">Documentation</Link>
            <a className="button" href={REPOSITORY} rel="noopener noreferrer">GitHub</a>
            {has('disclaimer') ? <Link className="button quiet" href="/docs/disclaimer/">Disclaimer</Link> : null}
          </div>
          <p className="demo-note" role="note">
            <strong>About the demo.</strong> It is the project&rsquo;s real panel over a fake backend that runs in your browser. {DEMO_NOTE.replace('Demo: nothing here is real. ', '')}{' '}
            {has('demo') ? <Link href="/docs/demo/">How the demo works</Link> : null}
          </p>
        </div>
      </section>

      {shots.length ? (
        <section className="wrap block" aria-labelledby="shots">
          <h2 id="shots">The panel</h2>
          <p className="sub">Screenshots of the real panel on the test machine. The demo shows the same pages with invented data.</p>
          <ul className="shots">
            {shots.map((shot) => (
              <li key={shot.src}>
                <a href={shot.src}><img src={shot.src} alt={shot.alt} loading="lazy" decoding="async" /></a>
                <span>{shot.alt}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {features ? (
        <section className="wrap block" aria-labelledby="features">
          <h2 id="features">What it does</h2>
          <Markdown page={readme} text={features} />
        </section>
      ) : null}

      {status ? (
        <section className="wrap block" aria-labelledby="status">
          <h2 id="status">What has been tested</h2>
          <Markdown page={readme} text={status} />
        </section>
      ) : null}

      <section className="wrap block next" aria-labelledby="next">
        <h2 id="next">Where to go next</h2>
        <ul className="cards">
          <li><a href={DEMO}><strong>Try the demo</strong><span>Create pretend Androids, start them, tap their screens, run a flow. Nothing leaves your browser.</span></a></li>
          {has('install') ? <li><Link href="/docs/install/"><strong>Install it</strong><span>Requirements, the installer and host setup for a real machine.</span></Link></li> : null}
          {has('quickstart') ? <li><Link href="/docs/quickstart/"><strong>Quick start</strong><span>From a running daemon to a first Android with an app, a flow and a backup.</span></Link></li> : null}
          {has('verification') ? <li><Link href="/docs/verification/"><strong>What was verified</strong><span>What was tested, on what, and what was not.</span></Link></li> : null}
          <li><Link href="/docs/"><strong>All documentation</strong><span>Guides, the reference and how it works.</span></Link></li>
        </ul>
      </section>
    </>
  );
}
