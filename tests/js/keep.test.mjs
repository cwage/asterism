// Keep this solve on the site (#113): the button follows what the server
// says (on offer, kept), flips it with one POST each way, and reports a
// refusal instead of pretending.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const DONE = {
  solve_seconds: 2.1,
  result: { labels: [{ name: 'Vega', x: 1, y: 1, mag: 0.03, kind: 'star', status: 'matched' }],
            constellations: [] },
};

// The page fetches the feed and the recent-uploads list on its own; only
// the keep traffic is recorded.
function fetchStub(calls, respond) {
  return async (url, opts) => {
    if (/\/(un)?keep$/.test(url)) calls.push({ url, method: opts && opts.method });
    if (/\/(un)?keep$/.test(url)) return respond(url);
    return { ok: true, status: 200, json: async () => ({ jobs: [] }), text: async () => '' };
  };
}

const ok = (body) => ({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) });
const refuse = (status, text) => ({ ok: false, status, text: async () => text });

function keepBtn(els) {
  return els.actions.children.find(c => /keep|kept/.test(c.textContent));
}

test('a fresh solve offers to keep, and one tap keeps it', async () => {
  const calls = [];
  const { sandbox, els } = loadPage({ fetch: fetchStub(calls, () => ok({ kept: true })) });
  sandbox.render('abc', { ...DONE, keep_open: true, kept: false });
  els.photo.onload();
  const btn = keepBtn(els);
  assert.equal(btn.textContent, 'keep this solve on the site');

  await btn.dispatch('click');
  assert.deepEqual(calls, [{ url: '/jobs/abc/keep', method: 'POST' }]);
  assert.equal(btn.textContent, 'kept on the site — undo');
  assert.equal(btn.disabled, false);

  await btn.dispatch('click');
  assert.equal(calls[1].url, '/jobs/abc/unkeep');
  assert.equal(btn.textContent, 'keep this solve on the site');
});

test('a kept solve past the window still shows the undo', () => {
  const { sandbox, els } = loadPage();
  sandbox.render('abc', { ...DONE, keep_open: false, kept: true });
  els.photo.onload();
  assert.equal(keepBtn(els).textContent, 'kept on the site — undo');
});

test('no offer once the window has closed', () => {
  const { sandbox, els } = loadPage();
  sandbox.render('abc', { ...DONE, keep_open: false, kept: false });
  els.photo.onload();
  assert.equal(keepBtn(els), undefined);
  // and a job from before the fields existed gets no button either
  sandbox.render('def', DONE);
  els.photo.onload();
  assert.equal(keepBtn(els), undefined);
});

test('a refusal is shown on the button, and the next tap tries again', async () => {
  const calls = [];
  const { sandbox, els } = loadPage({
    fetch: fetchStub(calls, () => refuse(429, "that's enough kept for one day; try tomorrow")),
  });
  sandbox.render('abc', { ...DONE, keep_open: true, kept: false });
  els.photo.onload();
  const btn = keepBtn(els);
  await btn.dispatch('click');
  assert.equal(btn.textContent, "could not keep: that's enough kept for one day; try tomorrow");
  assert.equal(btn.disabled, false);
  await btn.dispatch('click');
  assert.equal(calls.length, 2);
});

test('the disclosure and the feed heading say the window has an exception', () => {
  const { html, sandbox, els } = loadPage();
  assert.ok(html.includes('deleted after 24 hours unless you keep them'));
  assert.ok(html.includes('unless you choose to keep'));
});
