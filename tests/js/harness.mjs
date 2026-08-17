// Loads static/index.html's inline script into a vm sandbox with minimal
// DOM/localStorage shims, so the page's logic — feed rendering, label layout,
// draw(), control wiring — runs under `node --test` with no browser and no
// dependencies. getContext() hands back the recording context below rather
// than a real canvas, so tests assert what was drawn instead of pixels.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// Recording 2D context: draw() is the biggest chunk of the page's logic,
// and it only needs a handful of canvas calls. Every operation is logged
// with the state that was current when it ran, so tests can assert what
// was drawn (and how) without a real canvas.
export function makeCtx() {
  const ctx = {
    ops: [],
    strokeStyle: '', fillStyle: '', lineWidth: 1, font: '', globalAlpha: 1,
    _dash: [], _path: null,
    // draw() clears before every render; forget prior ops with it, or a
    // second draw (a layer toggle, say) would be read against stale strokes.
    clearRect() { ctx.ops.length = 0; },
    setLineDash(d) { ctx._dash = d; },
    beginPath() { ctx._path = []; },
    moveTo(x, y) { ctx._path.push(['moveTo', x, y]); },
    lineTo(x, y) { ctx._path.push(['lineTo', x, y]); },
    arc(x, y, r) { ctx._path.push(['arc', x, y, r]); },
    stroke() {
      ctx.ops.push({ op: 'stroke', path: ctx._path || [],
                     strokeStyle: ctx.strokeStyle, dashed: ctx._dash.length > 0,
                     alpha: ctx.globalAlpha });
    },
    measureText(text) { return { width: text.length * 7 }; },
    fillText(text, x, y) {
      ctx.ops.push({ op: 'fillText', text, x, y, fillStyle: ctx.fillStyle });
    },
  };
  return ctx;
}

export function makeEl() {
  const el = {
    children: [], parent: null, className: '', textContent: '', title: '',
    href: '', src: '', alt: '', loading: '', hidden: false, checked: true,
    value: '4.5', onerror: null, onload: null, style: {},
    naturalWidth: 1000, naturalHeight: 800, width: 0, height: 0,
    rect: { left: 0, top: 0, width: 1000, height: 800 },
    getBoundingClientRect() { return el.rect; },
    append(...c) {
      for (const child of c) {
        if (child && typeof child === 'object') child.parent = el;
        el.children.push(child);
      }
    },
    replaceChildren(...c) {
      for (const old of el.children)
        if (old && typeof old === 'object' && old.parent === el) old.parent = null;
      el.children = [];
      el.append(...c);
    },
    // Faithful to DOM Element.remove(): detach from the parent's children,
    // so tests can observe what the page's a.remove() actually does.
    remove() {
      if (!el.parent) return;
      const i = el.parent.children.indexOf(el);
      if (i >= 0) el.parent.children.splice(i, 1);
      el.parent = null;
    },
    // Recorded, not ignored: which element a handler is bound to is the
    // difference between a control working and silently doing nothing, and
    // a no-op stub cannot tell those apart. dispatch() lets a test deliver
    // an event the way the browser would — see controls.test.mjs.
    listeners: {},
    addEventListener(type, fn) { (el.listeners[type] ??= []).push(fn); },
    removeEventListener(type, fn) {
      el.listeners[type] = (el.listeners[type] || []).filter((f) => f !== fn);
    },
    dispatch(type, event) {
      return Promise.all((el.listeners[type] || []).map((fn) => fn(event)));
    },
    getContext() { return (el.ctx ??= makeCtx()); },
  };
  // Minimal classList, kept in sync with className so tests can assert on
  // either one.
  el.classList = {
    add(name) {
      const names = new Set(el.className.split(/\s+/).filter(Boolean));
      names.add(name);
      el.className = [...names].join(' ');
    },
    remove(name) {
      el.className = el.className.split(/\s+/)
        .filter((n) => n && n !== name).join(' ');
    },
    contains: (name) => el.className.split(/\s+/).includes(name),
    toggle(name, force) {
      const has = el.classList.contains(name);
      const want = force === undefined ? !has : force;
      if (want) el.classList.add(name); else el.classList.remove(name);
      return want;
    },
  };
  return el;
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
  // `html` so a test can assert on the stylesheet: some of this page's
  // behaviour is CSS (whether the overlay swallows taps), and that is not
  // reachable from the script alone.
  return { sandbox, els, store, html };
}
