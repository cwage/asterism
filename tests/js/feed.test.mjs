// Public "recently solved" feed: server-driven strip of successful solves.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const JOBS = [
  { id: 'bbb', created_at: '2026-08-13 22:00:00',
    caption: 'Jupiter beside a waxing Moon' },
  { id: 'aaa', created_at: '2026-08-13 21:00:00' },
];

test('feed renders server order with captioned alt text', async () => {
  const { sandbox, els } = loadPage();
  const urls = [];
  sandbox.fetch = async (url) => {
    urls.push(url);
    return { ok: true, json: async () => ({ jobs: JOBS }) };
  };
  await sandbox.renderFeed();
  assert.deepEqual(urls, ['/feed']);

  const [title, row] = els.feed.children;
  assert.ok(title.textContent.includes('Recently solved'));
  // the strip advertises its Atom twin (#127)
  assert.equal(title.children.length, 1);
  assert.equal(title.children[0].href, '/feed.atom');
  assert.equal(row.children.length, 2);
  assert.ok(row.children[0].href.includes('bbb'), 'server order preserved');
  assert.ok(row.children[0].children[0].src.includes('/jobs/bbb/image'));
  assert.ok(row.children[0].children[0].alt.includes('Jupiter beside a waxing Moon'));
  // captionless entries still get descriptive alt text
  assert.ok(row.children[1].children[0].alt.includes('a solved night-sky photo'));

  // an image error alone doesn't hide the thumb (transient hiccup)...
  sandbox.fetch = async () => { throw new Error('network blip'); };
  await row.children[0].children[0].onerror();
  assert.equal(row.children.length, 2);
  sandbox.fetch = async () => ({ status: 500 });
  await row.children[0].children[0].onerror();
  assert.equal(row.children.length, 2);
  // ...only a confirmed-gone job (reaped mid-view) does
  sandbox.fetch = async (url) => {
    assert.equal(url, '/jobs/bbb');
    return { status: 404 };
  };
  await row.children[0].children[0].onerror();
  assert.equal(row.children.length, 1);
});

test('malformed feed payload leaves the page alone', async () => {
  const { sandbox, els } = loadPage();
  sandbox.fetch = async () => ({ ok: true, json: async () => ({}) });
  await sandbox.renderFeed();
  assert.equal(els.feed.children.length, 0);
});

test('sky chips sit under the photo inside the same result link', async () => {
  const { sandbox, els } = loadPage();
  sandbox.fetch = async () => ({ ok: true, json: async () => ({ jobs: [
    { ...JOBS[0], sky_tags: ['Milky Way core', 'Ptolemy Cluster'] }, JOBS[1],
  ] }) });
  await sandbox.renderFeed();
  const [tagged, legacy] = els.feed.children[1].children;
  assert.ok(tagged.href.includes('bbb'));
  assert.ok(tagged.children[0].alt.includes(JOBS[0].caption), 'caption stays as alt text');
  assert.equal(tagged.children[1].className, 'sky-tags');
  assert.deepEqual(Array.from(tagged.children[1].children, c => c.textContent),
    ['Milky Way core', 'Ptolemy Cluster']);
  assert.equal(legacy.children.length, 1, 'no empty chip container on older payloads');
  sandbox.fetch = async () => ({ status: 410 });
  await tagged.children[0].onerror();
  assert.equal(els.feed.children[1].children.length, 1, 'expiration removes chips too');
});

test('sky chips are bounded plain text and tolerate missing or malformed tags', async () => {
  const { sandbox, els } = loadPage();
  sandbox.fetch = async () => ({ ok: true, json: async () => ({ jobs: [
    { ...JOBS[0], sky_tags: [null, '', 123, '  ', '<img src=x onerror=alert(1)>', 'Orion', 'third'] },
    { ...JOBS[1], sky_tags: 'Orion' },
  ] }) });
  await sandbox.renderFeed();
  const [tagged, malformed] = els.feed.children[1].children;
  const chips = tagged.children[1].children;
  assert.equal(chips.length, 2);
  assert.equal(chips[0].textContent, '<img src=x onerror=alert(1)>');
  assert.equal(chips[0].children.length, 0);
  assert.equal(malformed.children.length, 1);
});

test('empty feed renders nothing', async () => {
  const { sandbox, els } = loadPage();
  sandbox.fetch = async () => ({ ok: true, json: async () => ({ jobs: [] }) });
  await sandbox.renderFeed();
  assert.equal(els.feed.children.length, 0);
});

test('feed failure leaves the page alone', async () => {
  // harness fetch throws; the strip is decoration, not a dependency
  const { sandbox, els } = loadPage();
  await sandbox.renderFeed();
  assert.equal(els.feed.children.length, 0);
});
