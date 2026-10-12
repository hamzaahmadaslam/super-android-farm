# Demo

For anyone who wants to try the panel without installing anything, and for anyone who changes the panel or the demo.

The [live demo](https://androidfarm.hamzaahmadaslam.com) is a static website. It has a landing page, these documents, and the panel under `/panel/`. The panel there is the same set of files a farm serves. What answers it is a fake backend that runs inside your browser tab.

## What it is and is not

Nothing in the demo is real. No Android, VPN or server is connected to it, and no daemon runs behind it. The Androids, apps, addresses, logs and numbers are invented, and the device screens are drawings made in the page. A banner inside the panel says so on every page.

You can create Androids, start and stop them, watch a task progress, tap and type on a drawn screen, upload a VPN profile or an app, add a gateway, run a flow, take a backup, answer a request from a simulated AI assistant and change settings. Each of these changes the pretend farm the way the real action would change a real one, so the demo shows how the panel behaves. It does not show that the software works on your machine. [VERIFICATION.md](VERIFICATION.md) says what was tested for real.

The site has no server code, no database, no analytics and no account. Signing in accepts any text as the token.

## Privacy

Nothing you do in the demo leaves your browser. After the page has loaded, the panel makes no request for its data: every call it would send to a daemon is answered inside the page. This was checked in headless Chrome when the demo was built. Every page of the panel was opened and the actions listed above were carried out, and the browser's network log held requests to the site's own origin and nothing else, none of them to an `/api/` address. The response headers forbid it as well, because the content policy allows connections to the site's own origin only. `tests/unit/test_demo_site.py` keeps the promise in the code: it fails if a file of the fake backend contains a call that could open a connection, keep data beyond the tab or read a file's content.

A file you choose is never opened. The demo uses its name and its size and nothing else, so a real VPN profile or app you pick is not read, stored or sent. A username and a password typed into the credentials form are thrown away at once; the demo remembers only the name they were stored under. The token typed at sign-in is not compared with anything and not kept.

Your changes are kept in `sessionStorage`, which belongs to one browser tab. They survive a reload and moving between pages. They are gone when you close the tab, and a new private window starts with the original pretend farm. "Reset demo" in the banner puts everything back at once.

## How it stays in step with the panel

The demo does not contain a copy of the panel. At build time `demo/scripts/sync-panel.mjs` copies `superfarm/web/` into the site, so a change to the panel is in the demo at its next build.

One file is replaced. The panel reaches the daemon only through `superfarm/web/js/transport.js`, which has four functions: `request` (a fetch), `openSocket` (a WebSocket), `mediaUrl` (the address of a picture or a download) and `streamInto` (a stream of pictures into an image). In the panel they pass straight through to the browser. The build puts a one-line file in its place that re-exports `demo/mock/transport.js`, which answers the same four calls inside the page. A test in `tests/unit/test_web_static.py` keeps every request, socket and media address of the panel behind that file, and the build stops when the two files do not export the same names.

Some of the demo's data comes from the real code so that it cannot drift by hand: the settings schema, the default settings, the list of AI tools with the sentence each one shows in an approval request, the bundled example flows with the real validator's summary of each, and the capacity estimate, which is the real estimator's answer for the demo's invented host. `demo/scripts/generate.py` writes them to `demo/mock/generated/`, and they are committed because the hosting service builds with Node and has no Python environment. Regenerate them after changing the code they come from:

```bash
.venv/bin/python demo/scripts/generate.py
```

Two test files keep the demo in step, and both run in the normal local gate (`scripts/check.sh`). `tests/unit/test_demo_contract.py` fails when the generated files are stale, when the panel calls a route the fake backend does not answer, or when an answer of the fake backend has a different shape from the real API's answer to the same request. `tests/unit/test_demo_site.py` checks the site's own files: the dependencies, the headers in `vercel.json`, and that nothing private is in them.

## Run it locally

You need Node 20.9 or newer. From the repository:

```bash
cd demo
npm ci
npm run build      # copies the panel and the documents, then writes the site to demo/out
npm run preview    # serves demo/out on http://127.0.0.1:18700 with the headers of vercel.json
```

`npm run preview` is a small static server that applies the same response headers as the deployed site, so a page that works there works under the real content policy. Set `PORT` to use another port. For work on the landing page or the documentation pages, `npm run dev` starts the framework's development server; it copies the panel and the documents once at start, so run `npm run sync` again after changing them.

`demo/node_modules`, `demo/out`, `demo/public/panel`, `demo/public/docs` and `demo/generated` are produced by these commands and are not committed.

## How the fake backend is organised

Everything is under `demo/mock/`. `backend.js` holds the route table: a list of entries keyed by method and path pattern, where `{id}` stands for one path segment. `store.js` holds the state, one plain object saved to `sessionStorage` after every change. `farm.js` has the rules of the pretend farm: what an Android's status looks like, how a start becomes `starting`, `running` and `booted`, how tasks progress, and the capacity of the invented host (32 threads, 64 GB of RAM, one integrated and one discrete GPU). `device.js` is what a drawn screen shows and how it reacts to a tap, a key and typed text, and `screen.js` draws it. `seed.js` is the farm a visitor starts with. `chrome.js` adds the banner and the notes beside file and password fields. The files under `routes/` hold the routes, one file per area, as in `superfarm/api/routes/`.

Pending work is not driven by timers that a reload would lose. A task is a list of steps with the time each is due, stored with the state, and the steps whose time has come are applied before every request and twice a second. That is why an Android that was starting when you reloaded the page is still starting, or has booted, afterwards.

Addresses in the demo come from the three ranges reserved for documentation, `192.0.2.0/24`, `198.51.100.0/24` and `203.0.113.0/24`, which are never routed on the internet. The exception is `127.0.0.1`, where the panel shows each Android's ADB port. App names and package names are invented and live under `example.`.

## Add a route

When a new page of the panel calls a route the demo does not have, `tests/unit/test_demo_contract.py` fails and names the method, the path and the file that calls it. Until the route exists the demo answers such a call with status 404 and the code `not_in_demo`, so the rest of the panel keeps working.

To add it, put a `route()` call in the file under `demo/mock/routes/` that fits, or in a new file that you import in `demo/mock/index.js`:

```js
import { route } from '../backend.js';
import { db } from '../store.js';

route('GET', '/api/things/{id}', ({ params, query, body }) => {
  const thing = db.things[params.id];
  return { thing };
});
```

A handler returns the JSON body. `created(body)` and `accepted(task)` from `backend.js` answer with 201 and 202, and throwing one of the errors from `farm.js` (`bad`, `notFound`, `conflict`) answers with the real API's error envelope. Keep what the route needs in `db`, give it a starting value in `seed.js`, and read the real route in `superfarm/api/routes/` for the exact field names. A new `GET` route must also be added to the list `COMPARED` in `tests/unit/test_demo_contract.py`, with the matching request to the real application, or to `NOT_COMPARED` with the reason. The test says so when it is missing.

## Deployment

The site is built for Vercel, and `vercel.json` at the top of the repository carries the whole configuration: the install and build commands (both run in `demo/`), the output directory `demo/out`, the response headers and the rule that decides whether a push needs a build. This has not been run on Vercel by the project's tests; the build and the headers were verified locally with `npm run build` and `npm run preview`.

To set it up, import the repository as a new project in Vercel once and accept the defaults. No setting has to be changed in the dashboard. After that every push to GitHub deploys by itself. A push that touches nothing under `demo/`, `superfarm/web/` or `docs/`, no Markdown file and not `vercel.json` is skipped. The repository has no workflow files, and nothing is built or tested on GitHub; the gates run locally with `scripts/check.sh`.

For your own domain, add the domain to the project in Vercel, then create the DNS record Vercel shows for it at your DNS provider. For a subdomain that is a `CNAME` record pointing to the target Vercel names. Vercel issues the certificate once the record is visible.

The headers set a content policy on every page. Scripts, styles, fonts and connections are allowed from the site's own origin only, and the panel may also show pictures from `blob:` and `data:` addresses, which is how the drawn screens reach the page. No page needs `unsafe-inline` or `unsafe-eval`. The framework's static export writes small inline scripts into each page, which that policy would block; `demo/scripts/postbuild.mjs` moves each of them into a file of its own after the build, so the pages keep working under the strict policy. The cost is a few more small requests per page, three on the landing page, and they are cached like the framework's other files.

## Limits

The demo covers the routes the panel calls. A route that exists only for the command line or for an AI client is not simulated.

Flows that you write are checked by a simplified checker. It knows the layout of a flow file and the real step names, and it does not check a step's arguments or nested steps. The demo says so when it has checked one. The bundled example flows carry the real validator's summary.

The live view shows pictures a few times a second. The H.264 view of the real panel is switched off, because there is no device stream to relay. Native windows need a desktop, and the demo reports that it has none. Listening to a device and the virtual camera keep their state, which Android is listened to and which feed plays where, with no sound or picture behind it.

The Assistant and AI Pilot pages work, with a script in place of a language model. No model is connected and nothing is sent anywhere; the one model profile, `demo-script`, says so when it is tested, and every conversation opens with a notice to the same effect. The Assistant matches keywords in what you type: a question about status or capacity is answered from the demo's own numbers, and a request to start, stop or restart some Androids, to create new ones or to delete one waits for your Yes or No in the Approvals inbox, then really happens to the pretend Androids and is written down in the farm's own record line. Anything else gets a short answer that lists the phrases it understands. A Pilot session plays five to seven fixed actions per run on the drawn screen of each Android you pick, with a stated reason and outcome for each. With three runs or more one run ends unverified so that Confirm can be tried, and with two Androids or more one of them stops once for a person to answer Resume, Skip or Abort. Whether a run counts as verified is part of the script, not a look at the screen. Training the verified runs gives a program that appears in the Automation page and can be run. Conversations and sessions are kept like the rest of the demo's state and go with Reset demo. `demo/mock/routes/ai.js` holds all of it.

The simulated assistant files a request now and then so that the Approvals page has something to answer. At most two requests wait at a time, and its name carries "(simulated)". No model is involved.

The demo holds at most 300 Androids, so that its state fits in one tab's storage.

## Next

[../README.md](../README.md) describes the project itself, and [README.md](README.md) lists every document. [../DISCLAIMER.md](../DISCLAIMER.md) says what you are responsible for when you use the real thing. [API.md](API.md) describes the API the fake backend imitates.
