// What the demo adds to the panel's page: the banner that says it is a demo, a line on
// the sign-in card, a hint next to every file and password field, and "Reset demo".
// Built with DOM calls only, so the page needs no looser content policy than the panel.

import site from './site.js';
import { storageFull } from './store.js';

const BANNER = 'No Androids, VPNs or servers are connected, and your changes stay in this browser tab.';
const FILE_HINT = 'Demo: the file is not opened, uploaded or kept. Only its name and size are used.';
const SECRET_HINT = 'Demo: what you type here is thrown away. Only the name is remembered, as "set".';

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (name === 'class') node.className = value;
    else if (name === 'text') node.textContent = value;
    else node.setAttribute(name, value);
  }
  node.append(...children);
  return node;
}

const link = (label, href, outside) => el('a', outside ? { href, target: '_blank', rel: 'noopener' } : { href, target: '_blank' }, label);

export function installChrome({ subscribe, signedIn, reset }) {
  const sheet = el('link', { rel: 'stylesheet', href: new URL('./demo.css', import.meta.url).pathname });
  document.head.append(sheet);
  document.documentElement.dataset.demo = '1';

  // ------------------------------------------------------------- banner
  const status = el('span', { class: 'demo-status', role: 'status', 'aria-live': 'polite' });
  const again = el('button', { type: 'button', class: 'btn small', text: 'Reset demo', title: 'Put the demo back to how it was when you opened it' });
  let armed = 0;
  again.addEventListener('click', () => {
    if (Date.now() - armed > 5000) {
      armed = Date.now();
      again.textContent = 'Reset everything? Press again';
      again.classList.add('danger');
      setTimeout(() => { again.textContent = 'Reset demo'; again.classList.remove('danger'); }, 5000);
      return;
    }
    reset();
    location.hash = '#/dashboard';
    location.reload();
  });
  const banner = el('div', { id: 'demo-banner', class: 'demo-banner', role: 'note', 'aria-label': 'About this demo' },
    el('span', { class: 'demo-text' }, el('strong', { text: 'Demo: nothing here is real.' }), ` ${BANNER}`),
    el('span', { class: 'demo-links' }, link('Repository', site.repository, true), link('Docs', site.docs), link('Disclaimer', site.disclaimer, site.disclaimer.startsWith('http')), again),
    status);
  const app = document.getElementById('app');
  const main = document.getElementById('main');
  if (app && main) app.insertBefore(banner, main);

  let clear = 0;
  const say = (text) => {
    status.textContent = text;
    clearTimeout(clear);
    clear = setTimeout(() => { status.textContent = ''; }, 12000);
  };
  subscribe((message) => {
    if (message.type === 'note') say(message.text);
    else if (storageFull()) say('This tab’s storage is full: newer changes last until you reload. "Reset demo" makes room.');
  });

  // ------------------------------------------------------------ sign-in
  const form = document.getElementById('login-form');
  const token = document.getElementById('login-token');
  if (form && token) {
    const label = form.querySelector('label');
    form.insertBefore(el('p', { class: 'demo-login', role: 'note' },
      el('strong', { text: 'This is the demo: nothing here is real.' }),
      ` ${BANNER} Any text works as the token: it is not checked, kept or sent anywhere. Press "Sign in".`), label);
    const fill = () => { if (!form.closest('[hidden]') && !token.value) token.value = 'demo'; };
    new MutationObserver(fill).observe(document.getElementById('login'), { attributes: true, attributeFilter: ['hidden'] });
    token.addEventListener('blur', fill);
    fill();
  }
  const farm = document.getElementById('farm-name');
  if (farm) farm.title = 'An invented farm that runs inside this page';

  // -------------------------------------------------------------- hints
  // Every place a visitor could hand over a file or a secret says what happens to it.
  const hint = (input, text) => {
    if (input.dataset.demoHint || input.id === 'login-token') return;
    input.dataset.demoHint = '1';
    const id = `demo-hint-${Math.random().toString(36).slice(2, 9)}`;
    input.after(el('span', { class: 'demo-hint', id, text }));
    input.setAttribute('aria-describedby', [input.getAttribute('aria-describedby'), id].filter(Boolean).join(' '));
  };
  const scan = (root) => {
    if (!(root instanceof Element)) return;
    for (const input of root.matches('input') ? [root] : root.querySelectorAll('input[type="file"], input[type="password"]')) {
      if (input.type === 'file') hint(input, FILE_HINT);
      else if (input.type === 'password') hint(input, SECRET_HINT);
    }
  };
  new MutationObserver((changes) => {
    for (const change of changes) for (const node of change.addedNodes) scan(node);
  }).observe(document.body, { childList: true, subtree: true });
  scan(document.body);

  return { say, signedIn };
}
