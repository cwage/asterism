// Feedback dialog (#137): opens with the page's context on show, posts
// the text and that context as JSON, and reports what happened.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const JOB = {
  status: 'done', solve_seconds: 2.1,
  result: { labels: [{ name: 'Vega', x: 1, y: 1, mag: 0.03, kind: 'star', status: 'matched' }],
            constellations: [], match: { logodds: 214.5, nmatch: 42, ndistract: 8 },
            fov_bounds: [38.4, 65.9] },
};

function stub(calls, respond) {
  return async (url, opts) => {
    if (url !== '/feedback') return { ok: true, status: 200, json: async () => ({ jobs: [] }), text: async () => '' };
    calls.push({ url, opts });
    return respond();
  };
}

test('opening shows the context the report will carry', () => {
  const { sandbox, els } = loadPage({ fetch: stub([], () => ({ ok: true })) });
  // set after load: a ?job= search at load time starts the page polling
  // the job, and a stub that never says "done" keeps the loop alive
  sandbox.location.search = '?job=abc123';
  sandbox.render('abc123', JOB);
  els.photo.onload();
  sandbox.openFeedback();
  assert.ok(els['feedback-overlay'].className.includes('open'));
  const ctx = JSON.parse(els['feedback-context'].textContent);
  assert.equal(ctx.job, 'abc123');
  assert.equal(ctx.status, 'done');
  assert.deepEqual(ctx.match, JOB.result.match);
  assert.deepEqual(ctx.fov_bounds, JOB.result.fov_bounds);
  sandbox.closeFeedback();
  assert.ok(!els['feedback-overlay'].className.includes('open'));
});

test('sending posts the text and the context, and links the issue', async () => {
  const calls = [];
  const { sandbox, els } = loadPage({
    fetch: stub(calls, () => ({ ok: true, status: 200,
      json: async () => ({ number: 7, url: 'https://github.com/cwage/asterism/issues/7' }) })),
  });
  sandbox.openFeedback();
  // the harness creates elements on first lookup; the textarea is only
  // read on send, so fetch it the way the page does
  const text = sandbox.document.getElementById('feedback-text');
  text.value = '  the labels are off  ';
  await sandbox.sendFeedback();
  assert.equal(calls.length, 1);
  const body = JSON.parse(calls[0].opts.body);
  assert.equal(body.description, 'the labels are off');
  assert.equal(body.context.job, null);
  assert.equal(calls[0].opts.method, 'POST');
  const status = els['feedback-status'];
  assert.equal(status.children[0], 'Thanks — filed as ');
  assert.equal(status.children[1].textContent, '#7');
  assert.equal(status.children[1].href, 'https://github.com/cwage/asterism/issues/7');
  assert.equal(text.value, '');
});

test('an empty report never leaves the page', async () => {
  const calls = [];
  const { sandbox, els } = loadPage({ fetch: stub(calls, () => ({ ok: true })) });
  sandbox.openFeedback();
  sandbox.document.getElementById('feedback-text').value = '   ';
  await sandbox.sendFeedback();
  assert.equal(calls.length, 0);
  assert.equal(els['feedback-status'].textContent, 'Say what happened first.');
});

test('a refusal is shown in words and the button comes back', async () => {
  const calls = [];
  const { sandbox, els } = loadPage({
    fetch: stub(calls, () => ({ ok: false, status: 429,
      text: async () => JSON.stringify({ detail: 'one report a minute; try again shortly' }) })),
  });
  sandbox.openFeedback();
  const text = sandbox.document.getElementById('feedback-text');
  text.value = 'again';
  await sandbox.sendFeedback();
  assert.equal(els['feedback-status'].textContent, 'could not send: one report a minute; try again shortly');
  assert.equal(els['feedback-send'].disabled, false);
  assert.equal(text.value, 'again');
});
