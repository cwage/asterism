// A re-upload of the same bytes gets the earlier job back (#120). The page
// says so, or an answer that lands before the spinner reads as a glitch.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const FAILED_JOB = {
  status: 'failed', error: 'no solution',
  result: { failure: { reason: 'no_match', guess_unavailable: 'no_timestamp' } },
};

function upload(sandbox, els, postResponse) {
  sandbox.FormData = class { append() {} };
  sandbox.fetch = async (url, opts) => {
    if (url === '/jobs' && opts && opts.method === 'POST')
      return { ok: true, json: async () => postResponse };
    if (url === '/feed') return { ok: true, json: async () => ({ jobs: [] }) };
    assert.equal(url, '/jobs/' + postResponse.id);
    return { ok: true, json: async () => FAILED_JOB };
  };
  return els.file.dispatch('change', { target: { files: [{ name: 'sky.jpg' }] } });
}

test('a duplicate upload says it is one, after the result is up', async () => {
  const { sandbox, els } = loadPage();
  await upload(sandbox, els, { id: 'j1', status: 'failed', duplicate: true });
  assert.ok(els.status.textContent.startsWith('same photo as an earlier upload'),
            els.status.textContent);
  // The result itself is still there behind the note.
  assert.ok(els.status.textContent.includes('solve failed'), els.status.textContent);
  assert.equal(els.failbox.hidden, false);
});

test('a first upload gets no such note', async () => {
  const { sandbox, els } = loadPage();
  await upload(sandbox, els, { id: 'j2', status: 'queued' });
  assert.equal(els.status.textContent, 'solve failed');
});
