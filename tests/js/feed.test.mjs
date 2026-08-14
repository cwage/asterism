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
  assert.equal(row.children.length, 2);
  assert.ok(row.children[0].href.includes('bbb'), 'server order preserved');
  assert.ok(row.children[0].children[0].src.includes('/jobs/bbb/image'));
  assert.ok(row.children[0].children[0].alt.includes('Jupiter beside a waxing Moon'));
  // captionless entries still get descriptive alt text
  assert.ok(row.children[1].children[0].alt.includes('a solved night-sky photo'));

  // a thumb whose image 404s (job reaped mid-view) removes itself
  row.children[0].children[0].onerror();
  assert.equal(row.children.length, 1);
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
