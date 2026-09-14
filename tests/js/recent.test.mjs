import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const PREFIX = 'asterismRecentUpload:';
const A = 'a'.repeat(32), B = 'b'.repeat(32), C = 'c'.repeat(32);
const entry = (id, at = Date.now(), title = 'night.jpg') => ({ id, at, title });
const keyFor = e => PREFIX + e.id + ':' + e.at;
const seed = entries => Object.fromEntries(entries.map(e => [keyFor(e), JSON.stringify(e)]));
const stored = page => Object.entries(page.store).filter(([key]) => key.startsWith(PREFIX))
  .map(([, value]) => JSON.parse(value)).sort((a, b) => b.at - a.at || a.id.localeCompare(b.id));
const links = page => page.els['recent-list'].children.map(li => li.children[0]);
const states = page => links(page).map(a => a.children[1].textContent);
const settle = () => new Promise(r => setTimeout(r, 0));
const response = job => ({ ok: true, json: async () => job });
const failed = { status: 'failed', error: 'no match', result: { failure: { can_deepen: false } } };

test('empty history stays out of the way and makes no job requests', async () => {
  const urls = [];
  const page = loadPage({ fetch: async url => { urls.push(url); return response({ jobs: [] }); } });
  await settle();
  assert.equal(page.els.recent.hidden, true);
  assert.deepEqual(urls, ['/feed']);
});

test('reload restores bookmarks and fetches current statuses and captions', async () => {
  const store = seed([entry(A, 100), entry(B, 200), entry(C, 300)]);
  const page = loadPage({ store, fetch: async url => {
    if (url === '/feed') return response({ jobs: [] });
    return response(url.endsWith(A) ? { status: 'done', result: { narration: { caption: 'Orion rising' } } }
      : url.endsWith(B) ? failed : { status: 'solving' });
  } });
  await settle();
  assert.equal(page.els.recent.hidden, false);
  assert.deepEqual(links(page).map(a => a.href), [C, B, A].map(id => '/?job=' + id));
  assert.match(states(page)[0], /^Solving/);
  assert.match(states(page)[1], /^Unsolved/);
  assert.match(states(page)[2], /^Solved/);
  assert.equal(links(page)[2].children[0].textContent, 'Orion rising');
  assert.equal(page.els['recent-refresh'].disabled, false);
  // These timestamps predate retention; live/featured jobs must not be age-pruned.
  assert.equal(stored(page).length, 3);
});

test('remembering uploads deduplicates, orders by submission, and stores only bookmark data', () => {
  const page = loadPage();
  const s = page.sandbox;
  s.rememberUpload(A, 'first.jpg', 100, 'queued');
  s.rememberUpload(B, 'second.jpg', 200, 'queued');
  s.rememberUpload(A, 'again.jpg', 300, 'done');
  s.rememberUpload(A, 'late-old-response.jpg', 50, 'queued');
  assert.deepEqual(stored(page), [entry(A, 300, 'again.jpg'), entry(B, 200, 'second.jpg')]);
  assert.match(states(page)[0], /^Solved/);
  s.updateRecentJob(A, { status: 'done', result: { narration: { caption: 'Orion' }, secret: 'not stored' } });
  assert.deepEqual(Object.keys(stored(page)[0]).sort(), ['at', 'id', 'title']);
  assert.ok(!JSON.stringify(page.store).includes('secret'));
});

test('only the newest 24 bookmarks are retained', () => {
  const page = loadPage();
  for (let i = 1; i <= 30; i++) page.sandbox.rememberUpload(i.toString(16).padStart(32, '0'), 'photo', i, 'queued');
  assert.equal(stored(page).length, 24);
  assert.equal(stored(page)[0].at, 30);
  assert.equal(stored(page).at(-1).at, 7);
});

test('malformed storage and invalid IDs are ignored', async () => {
  for (const raw of ['not json', '{}', 'null', JSON.stringify([null, {}, entry('../jobs/x'), entry(A, 'nope')])]) {
    const page = loadPage({ store: { [keyFor(entry(A, 100))]: raw } });
    await settle();
    assert.equal(page.els.recent.hidden, true);
    page.sandbox.rememberUpload(A, 'valid.jpg', 100, 'queued');
    assert.equal(stored(page).length, 1);
  }
});

test('filenames and fetched captions render as bounded text, not HTML', () => {
  const page = loadPage();
  page.sandbox.rememberUpload(A, '<img src=x onerror=alert(1)>', 100, 'queued');
  let title = links(page)[0].children[0];
  assert.equal(title.textContent, '<img src=x onerror=alert(1)>');
  assert.equal(title.children.length, 0);
  page.sandbox.updateRecentJob(A, { status: 'done', result: { narration: { caption: '<script>'.repeat(50) } } });
  title = links(page)[0].children[0];
  assert.equal(title.textContent.length, 160);
  assert.equal(title.children.length, 0);
});

test('confirmed expiry removes entries but network and server failures do not', async () => {
  const page = loadPage();
  for (const id of [A, B, C]) page.sandbox.rememberUpload(id, 'night.jpg', 100, 'queued');
  page.sandbox.fetch = async url => {
    if (url.endsWith(A)) return { status: 404 };
    if (url.endsWith(B)) return { status: 503, ok: false };
    throw new Error('offline');
  };
  await page.sandbox.refreshRecentUploads();
  assert.deepEqual(stored(page).map(e => e.id).sort(), [B, C]);
  assert.ok(states(page).every(text => text.startsWith('Status unavailable')));
  page.sandbox.fetch = async () => ({ status: 410 });
  await page.sandbox.refreshRecentUploads();
  assert.deepEqual(stored(page), []);
  assert.equal(page.els.recent.hidden, true);
});

test('refresh button and returning to the tab fetch fresh statuses', async () => {
  const page = loadPage();
  page.sandbox.rememberUpload(A, 'night.jpg', 100, 'queued');
  page.sandbox.fetch = async () => response({ status: 'solving' });
  await page.els['recent-refresh'].dispatch('click');
  assert.match(states(page)[0], /^Solving/);
  page.sandbox.fetch = async () => response(failed);
  await page.sandbox.window.dispatch('focus');
  assert.match(states(page)[0], /^Unsolved/);
});

test('storage refusal preserves a tab-local list and explains its lifetime', async () => {
  const page = loadPage();
  page.sandbox.localStorage.getItem = () => { throw new Error('disabled'); };
  page.sandbox.localStorage.setItem = () => { throw new Error('disabled'); };
  page.sandbox.rememberUpload(A, 'first.jpg', 100, 'queued');
  page.sandbox.rememberUpload(B, 'second.jpg', 200, 'queued');
  page.sandbox.fetch = async () => response(failed);
  await page.sandbox.refreshRecentUploads();
  assert.equal(links(page).length, 2);
  assert.match(page.els['recent-help'].textContent, /only in this tab/);
});

test('another tab additions are preserved and storage clear updates this tab', async () => {
  const page = loadPage();
  Object.assign(page.store, seed([entry(A, 100)]));
  page.sandbox.rememberUpload(B, 'other.jpg', 200, 'queued');
  assert.deepEqual(stored(page).map(e => e.id), [B, A]);
  for (const key of Object.keys(page.store)) delete page.store[key];
  await page.sandbox.window.dispatch('storage', { key: null });
  assert.equal(page.els.recent.hidden, true);
});

test('a delayed refresh body cannot overwrite a newer poll or deepen', async () => {
  const page = loadPage();
  page.sandbox.rememberUpload(A, 'night.jpg', 100, 'failed');
  let release;
  page.sandbox.fetch = async () => ({ ok: true, json: () => new Promise(r => { release = r; }) });
  const pending = page.sandbox.refreshRecentUploads();
  await settle();
  page.sandbox.updateRecentJob(A, { status: 'queued' });
  release(failed);
  await pending;
  assert.match(states(page)[0], /^Queued/);
});

test('a removed bookmark is not resurrected by a late refresh', async () => {
  const page = loadPage();
  page.sandbox.rememberUpload(A, 'night.jpg', 100, 'queued');
  let release;
  page.sandbox.fetch = () => new Promise(r => { release = r; });
  const pending = page.sandbox.refreshRecentUploads();
  page.sandbox.forgetRecentUpload(A);
  release(response(failed));
  await pending;
  assert.deepEqual(stored(page), []);
  assert.equal(page.els.recent.hidden, true);
});

test('a stale 404 cannot remove a newly remembered submission of the same job', async () => {
  const page = loadPage();
  page.sandbox.rememberUpload(A, 'night.jpg', 100, 'queued');
  let release;
  page.sandbox.fetch = () => new Promise(r => { release = r; });
  const pending = page.sandbox.refreshRecentUploads();
  page.sandbox.rememberUpload(A, 'night-again.jpg', 200, 'done');
  release({ status: 404 });
  await pending;
  assert.deepEqual(stored(page), [entry(A, 200, 'night-again.jpg')]);
  assert.match(states(page)[0], /^Solved/);
});

test('overlapping refreshes share one bounded request sequence', async () => {
  const page = loadPage();
  page.sandbox.rememberUpload(A, 'night.jpg', 100, 'queued');
  let release, requests = 0;
  page.sandbox.fetch = () => { requests++; return new Promise(r => { release = r; }); };
  const pending = page.sandbox.refreshRecentUploads();
  await page.sandbox.refreshRecentUploads();
  assert.equal(requests, 1);
  release(response(failed));
  await pending;
});

test('an unresponsive status request is aborted without losing the bookmark', async () => {
  const page = loadPage();
  page.sandbox.rememberUpload(A, 'night.jpg', 100, 'queued');
  page.sandbox.setTimeout = callback => setTimeout(callback, 0);
  page.sandbox.fetch = (url, { signal }) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(new Error('aborted')));
  });
  await page.sandbox.refreshRecentUploads();
  assert.equal(stored(page).length, 1);
  assert.match(states(page)[0], /^Status unavailable/);
  assert.equal(page.els['recent-refresh'].disabled, false);
});

test('opening a shared result never creates a personal bookmark', async () => {
  const page = loadPage({ search: '?job=' + A, fetch: async url => response(url === '/feed' ? { jobs: [] } : failed) });
  await settle();
  assert.deepEqual(stored(page), []);
  assert.equal(page.els.recent.hidden, true);
});

for (const [timing, secondAt, order] of [
  ['distinct timestamps', 1001, [B, A]],
  ['tied timestamps', 1000, [A, B]],
]) {
  test(`superseded uploads survive without replacing the current photo: ${timing}`, async () => {
    const page = loadPage();
    const { sandbox: s, els } = page;
    // An event-loop turn does not guarantee Date.now() advances. Control
    // time explicitly, and check the job-ID tie-break separately.
    let now = 1000;
    s.Date = class extends Date { static now() { return now; } };
    s.FormData = class { append() {} };
    let releaseFirst, posts = 0;
    s.fetch = async (url, opts) => {
      if (opts?.method === 'POST') {
        if (++posts === 1) return new Promise(r => { releaseFirst = r; });
        return response({ id: B, status: 'queued' });
      }
      return response(failed);
    };
    const first = els.file.dispatch('change', { target: { files: [{ name: 'first.jpg' }] } });
    await settle();
    now = secondAt;
    await els.file.dispatch('change', { target: { files: [{ name: 'second.jpg' }] } });
    await settle();
    releaseFirst(response({ id: A, status: 'queued' }));
    await first;
    assert.deepEqual(stored(page).map(e => e.id), order);
    assert.deepEqual(links(page).map(a => a.href), order.map(id => '/?job=' + id));
    assert.equal(els.photo.src, '/jobs/' + B + '/image');
    const current = links(page).find(a => a.href === '/?job=' + B);
    assert.match(current.children[1].textContent, /^Unsolved/);
  });
}

test('refused uploads never enter the list', async () => {
  const page = loadPage();
  page.sandbox.FormData = class { append() {} };
  page.sandbox.fetch = async () => ({ ok: false, text: async () => 'too large' });
  await page.els.file.dispatch('change', { target: { files: [{ name: 'bad.jpg' }] } });
  assert.deepEqual(stored(page), []);
});

test('a successful deepen immediately updates the saved job to queued', async () => {
  const page = loadPage();
  const s = page.sandbox;
  s.rememberUpload(A, 'night.jpg', 100, 'failed');
  s.renderFailure(A, { ...failed, result: { failure: { can_deepen: true } } });
  s.fetch = async () => response({ status: 'queued' });
  let polled;
  s.poll = id => { polled = id; };
  await page.els.actions.children[0].onclick();
  assert.equal(polled, A);
  assert.match(states(page)[0], /^Queued/);
});

test('concurrent tab writes cannot replace a different tab bookmark', () => {
  const page = loadPage();
  const other = loadPage({ store: page.store });
  const write = page.sandbox.localStorage.setItem;
  let raced = false;
  page.sandbox.localStorage.setItem = (key, value) => {
    if (!raced) {
      raced = true;
      other.sandbox.rememberUpload(B, 'other-tab.jpg', 200, 'queued');
    }
    write(key, value);
  };
  page.sandbox.rememberUpload(A, 'this-tab.jpg', 100, 'queued');
  assert.deepEqual(stored(page).map(e => e.id).sort(), [A, B]);
});

test('expiry cannot erase another tab bookmark written during removal', () => {
  const page = loadPage();
  page.sandbox.rememberUpload(A, 'expired.jpg', 100, 'queued');
  const other = loadPage({ store: page.store });
  let raced = false;
  for (const method of ['setItem', 'removeItem']) {
    const original = page.sandbox.localStorage[method];
    page.sandbox.localStorage[method] = (...args) => {
      if (!raced) {
        raced = true;
        other.sandbox.rememberUpload(B, 'other-tab.jpg', 200, 'queued');
      }
      original(...args);
    };
  }
  page.sandbox.forgetRecentUpload(A);
  assert.deepEqual(stored(page).map(e => e.id), [B]);
});

test('a full list shares one deadline instead of 24 serial timeouts', async () => {
  const page = loadPage();
  for (let i = 1; i <= 24; i++) page.sandbox.rememberUpload(i.toString(16).padStart(32, '0'), 'night.jpg', i, 'queued');
  let requests = 0, timers = 0;
  page.sandbox.setTimeout = callback => { timers++; return setTimeout(callback, 0); };
  page.sandbox.fetch = (url, { signal }) => {
    requests++;
    return new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('aborted'))));
  };
  await page.sandbox.refreshRecentUploads();
  assert.equal(timers, 1);
  assert.equal(requests, 1);
  assert.equal(stored(page).length, 24);
  assert.equal(page.els['recent-refresh'].disabled, false);
});

for (const status of [404, 410, 503]) {
  test(`deepen HTTP ${status} removes only confirmed-missing bookmarks`, async () => {
    const page = loadPage();
    page.sandbox.rememberUpload(A, 'night.jpg', 100, 'failed');
    page.sandbox.renderFailure(A, { ...failed, result: { failure: { can_deepen: true } } });
    page.sandbox.fetch = async () => ({ ok: false, status, text: async () => 'unavailable' });
    await page.els.actions.children[0].onclick();
    assert.equal(stored(page).length, status === 503 ? 1 : 0);
    assert.match(page.els.status.textContent, /could not restart job/);
  });
}
