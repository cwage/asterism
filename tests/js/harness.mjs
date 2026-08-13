// Loads static/index.html's inline script into a vm sandbox with minimal
// DOM/localStorage shims, so the page's pure logic (upload history, label
// layout) runs under `node --test` with no browser and no dependencies.
// Canvas drawing itself is out of scope — getContext returns null.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

export function makeEl() {
  return {
    children: [], className: '', textContent: '', title: '', href: '',
    src: '', alt: '', loading: '', hidden: false, checked: true,
    value: '4.5', onerror: null, onload: null, style: {},
    append(...c) { this.children.push(...c); },
    replaceChildren(...c) { this.children = c; },
    remove() {},
    addEventListener() {},
    getContext() { return null; },
  };
}

export function loadPage() {
  const html = readFileSync(new URL('../../static/index.html', import.meta.url), 'utf8');
  const match = html.match(/<script>([\s\S]*)<\/script>/);
  if (!match) throw new Error('no inline <script> found in index.html');
  const els = {};
  const store = {};
  const sandbox = {
    document: {
      getElementById: (id) => (els[id] ??= makeEl()),
      createElement: () => makeEl(),
      addEventListener() {},
    },
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
    },
    location: { search: '' },
    window: { addEventListener() {} },
    history: { pushState() {} },
    fetch: async () => { throw new Error('unexpected fetch in test'); },
    URLSearchParams,
    setTimeout,
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(match[1], sandbox);
  return { sandbox, els, store };
}
