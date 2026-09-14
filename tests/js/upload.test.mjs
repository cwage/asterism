// A new upload takes the previous solve off the screen at once and shows
// the chosen photo, un-annotated, while it solves (#133). Before this the
// old photo kept its labels up under "solving…".
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const DONE_A = { status: 'done', solve_seconds: 3.2,
  result: { labels: [{ name: 'Vega', x: 200, y: 200, mag: 0.03, kind: 'star' }] } };
const DONE_B = { status: 'done', solve_seconds: 1.1,
  result: { labels: [{ name: 'Deneb', x: 300, y: 300, mag: 1.25, kind: 'star' }] } };
const FAILED_B = { status: 'failed', error: 'no solution',
  result: { failure: { reason: 'no_match', guess_unavailable: 'no_timestamp' } } };

const drew = (ctx, text) => ctx.ops.some(o => o.op === 'fillText' && o.text === text);
const settle = () => new Promise(r => setTimeout(r, 0));

// Show solve A the way a deep link would, then choose a new file. The
// POST is held until the test releases it, so the waiting state can be
// inspected before the new job answers.
function showThenUpload(sandbox, els, job) {
  sandbox.render('a', DONE_A);
  els.photo.onload();
  const ctx = els.overlay.ctx;
  assert.ok(drew(ctx, 'Vega'), 'solve A is on screen to begin with');
  assert.equal(els.controls.hidden, false);
  let release;
  const posted = new Promise(r => { release = r; });
  sandbox.FormData = class { append() {} };
  sandbox.fetch = async (url, opts) => {
    if (url === '/jobs' && opts && opts.method === 'POST') return posted;
    if (url === '/feed') return { ok: true, json: async () => ({ jobs: [] }) };
    assert.equal(url, '/jobs/b');
    return { ok: true, json: async () => job };
  };
  const done = els.file.dispatch('change', { target: { files: [{ name: 'new.jpg' }] } });
  return { ctx, release, done };
}

test('the old solve leaves the screen the moment a new file is chosen', async () => {
  const { sandbox, els, revoked } = loadPage();
  const { ctx, release, done } = showThenUpload(sandbox, els, DONE_B);
  await settle();
  assert.equal(els.status.textContent, 'uploading…');
  assert.equal(els.photo.src, 'blob:new.jpg', 'the chosen photo, not the old one');
  assert.equal(els.photo.hidden, false);
  assert.equal(ctx.ops.length, 0, 'the old labels are gone at once');
  assert.equal(els.controls.hidden, true);
  assert.equal(els.actions.children.length, 0);
  // a layer toggle during the wait must not bring the old labels back
  const box = sandbox.document.getElementById('lay-stars');
  box.checked = false;
  await box.dispatch('change');
  assert.equal(ctx.ops.length, 0);
  box.checked = true;
  // the preview's own load sizes the overlay to it and keeps it clear
  els.photo.naturalWidth = 640;
  els.photo.naturalHeight = 480;
  els.photo.onload();
  assert.equal(els.overlay.width, 640);
  assert.equal(ctx.ops.length, 0);
  assert.deepEqual(revoked, [], 'the preview is still in use');

  release({ ok: true, json: async () => ({ id: 'b', status: 'queued' }) });
  await done;
  await settle();
  assert.equal(els.photo.src, '/jobs/b/image', 'the served copy takes over');
  assert.deepEqual(revoked, ['blob:new.jpg']);
  els.photo.onload();
  assert.ok(drew(ctx, 'Deneb'));
  assert.ok(!drew(ctx, 'Vega'));
});

test('a solve that fails still replaces the preview with the served frame', async () => {
  const { sandbox, els, revoked } = loadPage();
  const { ctx, release, done } = showThenUpload(sandbox, els, FAILED_B);
  await settle();
  release({ ok: true, json: async () => ({ id: 'b', status: 'queued' }) });
  await done;
  await settle();
  assert.equal(els.failbox.hidden, false);
  assert.equal(els.photo.src, '/jobs/b/image');
  assert.deepEqual(revoked, ['blob:new.jpg']);
  els.photo.onload();
  assert.equal(ctx.ops.length, 0, 'no labels for a failed solve');
});

test('an upload the server refuses leaves the chosen photo up, not the old solve', async () => {
  const { sandbox, els, revoked } = loadPage();
  const { ctx, release, done } = showThenUpload(sandbox, els, DONE_B);
  await settle();
  release({ ok: false, text: async () => 'image too large' });
  await done;
  assert.equal(els.status.textContent, 'upload failed: image too large');
  assert.equal(els.photo.src, 'blob:new.jpg');
  assert.equal(ctx.ops.length, 0);
  assert.deepEqual(revoked, []);
});

test('a preview the browser cannot decode hides the frame until the served copy', async () => {
  const { sandbox, els } = loadPage();
  const { release, done } = showThenUpload(sandbox, els, DONE_B);
  await settle();
  els.photo.onerror();
  assert.equal(els.photo.hidden, true, 'no broken-image icon');
  release({ ok: true, json: async () => ({ id: 'b', status: 'queued' }) });
  await done;
  await settle();
  assert.equal(els.photo.hidden, false);
  assert.equal(els.photo.onerror, null, 'the preview handler does not outlive it');
});

test('choosing a second file while the first is still uploading swaps the preview', async () => {
  const { sandbox, els, revoked } = loadPage();
  const { release, done } = showThenUpload(sandbox, els, DONE_B);
  await settle();
  const again = els.file.dispatch('change', { target: { files: [{ name: 'newer.jpg' }] } });
  await settle();
  assert.equal(els.photo.src, 'blob:newer.jpg');
  assert.deepEqual(revoked, ['blob:new.jpg'], 'the superseded preview is released');
  release({ ok: true, json: async () => ({ id: 'b', status: 'queued' }) });
  await Promise.all([done, again]);
});
