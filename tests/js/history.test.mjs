// Upload-history strip (#43): localStorage lifecycle and dead-job semantics.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const KEY = 'asterism-history';

test('remember, order, outcome, dedupe', () => {
  const { sandbox, els } = loadPage();
  sandbox.rememberJob('aaa');
  sandbox.rememberJob('bbb');
  let [, row] = els.history.children;
  assert.equal(row.children.length, 2);
  assert.ok(row.children[0].href.includes('bbb'), 'newest first');
  assert.ok(row.children[0].children[0].src.includes('/jobs/bbb/image'));
  assert.ok(row.children[0].children[0].alt.startsWith('uploaded '),
    'descriptive alt text');

  sandbox.markJob('aaa', 'failed');
  [, row] = els.history.children;
  const failed = row.children.find(a => a.href.includes('aaa'));
  assert.equal(failed.className, 'failed');
  assert.ok(failed.children[0].alt.startsWith('solve failed'));

  sandbox.rememberJob('aaa');  // re-upload dedupes
  [, row] = els.history.children;
  assert.equal(row.children.length, 2);
});

test('24h prune and entry cap', () => {
  const { sandbox, els, store } = loadPage();
  sandbox.rememberJob('old');
  sandbox.rememberJob('fresh');
  const all = JSON.parse(store[KEY]);
  all.find(h => h.id === 'old').t = Date.now() - 25 * 3600 * 1000;
  store[KEY] = JSON.stringify(all);
  sandbox.renderHistory();
  const [, row] = els.history.children;
  assert.equal(row.children.length, 1, 'stale entry pruned');
  assert.ok(row.children[0].href.includes('fresh'));

  for (let i = 0; i < 30; i++) sandbox.rememberJob('job' + i);
  assert.equal(JSON.parse(store[KEY]).length, 24, 'capped at 24');
});

test('corrupt storage degrades to empty, not a crash', () => {
  const { sandbox, els, store } = loadPage();
  store[KEY] = '{not json';
  sandbox.renderHistory();
  assert.equal(els.history.children.length, 0);
});

test('image error forgets the job only on a definitive 404', async () => {
  const { sandbox, els, store } = loadPage();
  store[KEY] = JSON.stringify(
    [{ id: 'dead', t: Date.now() }, { id: 'flaky', t: Date.now() }]);
  sandbox.renderHistory();
  const [, row] = els.history.children;
  const dead = row.children.find(a => a.href.includes('dead'));
  const flaky = row.children.find(a => a.href.includes('flaky'));

  sandbox.fetch = async () => ({ status: 404 });
  await dead.children[0].onerror();
  assert.ok(!JSON.parse(store[KEY]).some(h => h.id === 'dead'),
    'dead job dropped from storage on 404');

  sandbox.fetch = async () => { throw new Error('network'); };
  await flaky.children[0].onerror();
  assert.ok(JSON.parse(store[KEY]).some(h => h.id === 'flaky'),
    'live job kept on transient error');
});
