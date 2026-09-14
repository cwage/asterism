// A re-upload of the same bytes gets the earlier job back (#120). The page
// says so on that job's status line, whichever path writes it, or an answer
// that lands before the spinner reads as a glitch.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const NOTE = 'same photo as an earlier upload';
const FAILED_JOB = {
  status: 'failed', error: 'no solution',
  result: { failure: { reason: 'no_match', guess_unavailable: 'no_timestamp' } },
};
const DONE_JOB = {
  status: 'done', solve_seconds: 3.2,
  result: { labels: [{ name: 'Vega', x: 200, y: 200, mag: 0.03, kind: 'star' }] },
};

// Drive the page's own upload handler: POST /jobs answers `posted`, the job
// itself answers `job`. The handler hands off to poll() without waiting, so
// settle() lets the poll's fetches land before asserting.
function upload(sandbox, els, posted, job) {
  sandbox.FormData = class { append() {} };
  sandbox.fetch = async (url, opts) => {
    if (url === '/jobs' && opts && opts.method === 'POST')
      return { ok: true, json: async () => posted };
    if (url === '/feed') return { ok: true, json: async () => ({ jobs: [] }) };
    assert.equal(url, '/jobs/' + posted.id);
    return { ok: true, json: async () => job };
  };
  return els.file.dispatch('change', { target: { files: [{ name: 'sky.jpg' }] } });
}
const settle = () => new Promise((r) => setTimeout(r, 0));

test('a duplicate that failed says so, with the failure', async () => {
  const { sandbox, els } = loadPage();
  await upload(sandbox, els, { id: 'j1', status: 'failed', duplicate: true }, FAILED_JOB);
  await settle();
  assert.ok(els.status.textContent.startsWith(NOTE), els.status.textContent);
  assert.ok(els.status.textContent.includes('solve failed'), els.status.textContent);
  assert.equal(els.failbox.hidden, false);
});

test('a duplicate that solved keeps the note once the photo loads', async () => {
  const { sandbox, els } = loadPage();
  await upload(sandbox, els, { id: 'j2', status: 'done', duplicate: true }, DONE_JOB);
  await settle();
  assert.ok(els.status.textContent.startsWith(NOTE), els.status.textContent);
  // The solved status is written from the photo's load handler, after
  // poll() has already returned; the note must survive that write.
  els.photo.onload();
  assert.ok(els.status.textContent.startsWith(NOTE), els.status.textContent);
  assert.ok(els.status.textContent.includes('solved in 3.2s'), els.status.textContent);
});

test('a first upload gets no such note', async () => {
  const { sandbox, els } = loadPage();
  await upload(sandbox, els, { id: 'j3', status: 'queued' }, FAILED_JOB);
  await settle();
  assert.equal(els.status.textContent, 'solve failed');
});

test('the note follows the job, not the status line', async () => {
  const { sandbox, els } = loadPage();
  await upload(sandbox, els, { id: 'j4', status: 'failed', duplicate: true }, FAILED_JOB);
  await settle();
  // The file input stays live during a poll, so a newer upload can render
  // while an older one is still in flight. Whichever writes last, the
  // note belongs only to the job the server called a duplicate.
  sandbox.renderFailure('j5', FAILED_JOB);
  assert.equal(els.status.textContent, 'solve failed');
  sandbox.renderFailure('j4', FAILED_JOB);
  assert.ok(els.status.textContent.startsWith(NOTE), els.status.textContent);
});
