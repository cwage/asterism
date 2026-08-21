// A failed solve still shows the un-annotated photo: next to the guess's
// "Venus was low in the west", the frame itself is how "wow, that IS
// Venus" happens.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const FAILED_JOB = {
  error: 'no solution',
  result: { failure: { reason: 'no_match',
                       guess_unavailable: 'no_timestamp' } },
};

test('a failed solve shows the photo', () => {
  const { sandbox, els } = loadPage();
  sandbox.renderFailure('j9', FAILED_JOB);
  assert.equal(els.photo.hidden, false);
  assert.ok(els.photo.src.includes('/jobs/j9/image'), els.photo.src);
  assert.equal(els.wrap.style.display, 'inline-block');
});

test('a failure narration lands in the narration panel', () => {
  const { sandbox, els } = loadPage();
  sandbox.renderFailure('j9', {
    error: 'only 3 star-like sources detected',
    result: { failure: { reason: 'no_stars' },
              narration: { text: 'That appears to be a sandwich.' } },
  });
  assert.equal(els.narration.textContent, 'That appears to be a sandwich.');
});

test('no narration leaves the panel empty', () => {
  const { sandbox, els } = loadPage();
  sandbox.renderFailure('j9', FAILED_JOB);
  assert.equal(els.narration.textContent, '');
});

test('labels from a previously viewed solve are cleared off the overlay', () => {
  const { sandbox, els } = loadPage();
  const recording = sandbox.document.getElementById('overlay').getContext();
  recording.ops.push({ op: 'stroke' }); // a prior solve's constellation line
  sandbox.renderFailure('j9', FAILED_JOB);
  els.photo.onload(); // the harness img never loads on its own
  assert.equal(recording.ops.length, 0, 'stale labels must not survive');
  assert.equal(els.overlay.width, els.photo.naturalWidth);
  assert.equal(els.overlay.height, els.photo.naturalHeight);
});
