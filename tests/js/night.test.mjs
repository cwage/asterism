// What the night was like and how faint the photo reached (#121, #122):
// the worker's sentences land on the page one paragraph each, join the
// copied description, and leave with the result they belong to.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const LINES = [
  'Taken in twilight, about 40 minutes before the sky was fully dark.',
  'The Moon was new, so it added no light to the sky.',
];
const JOB = {
  solve_seconds: 2.1,
  result: {
    labels: [{ name: 'Vega', x: 200, y: 200, mag: 0.03, kind: 'star', status: 'matched' }],
    constellations: [],
    night: { lines: LINES },
  },
};

function show(sandbox, els, job) {
  sandbox.render('abc', job);
  els.photo.onload();
}

test('night lines render one paragraph each, in the worker\'s order', () => {
  const { sandbox, els } = loadPage();
  show(sandbox, els, JOB);
  assert.deepEqual(els.night.children.map(p => p.textContent), LINES);
});

test('the copied description carries the night lines after the narration', () => {
  const { sandbox } = loadPage();
  const job = { ...JOB, result: { ...JOB.result,
    narration: { caption: 'Vega overhead', text: 'Vega rides high tonight.' } } };
  assert.equal(sandbox.describeText(job),
               'Vega overhead\n\nVega rides high tonight.\n\n' + LINES.join(' '));
  // the lines alone are still a description worth copying
  assert.equal(sandbox.describeText(JOB), LINES.join(' '));
});

test('no night context leaves the panel empty; a new upload clears it', () => {
  const { sandbox, els } = loadPage();
  show(sandbox, els, JOB);
  assert.equal(els.night.children.length, 2);
  sandbox.clearResult();
  assert.equal(els.night.children.length, 0);
  // a result from before the feature, or one whose night pass failed
  show(sandbox, els, { ...JOB, result: { ...JOB.result, night: null } });
  assert.equal(els.night.children.length, 0);
  show(sandbox, els, { ...JOB, result: { ...JOB.result, night: undefined } });
  assert.equal(els.night.children.length, 0);
});
