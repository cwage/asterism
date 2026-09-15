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

test('the place line reads as one more night line, on the page and in the copy', () => {
  const { sandbox, els } = loadPage();
  const line = 'The phone recorded its tilt, so sky geometry puts this near 36°N, 74°E: northern Pakistan.';
  const job = { ...JOB, result: { ...JOB.result, place: { source: 'tilt', line } } };
  show(sandbox, els, job);
  assert.deepEqual(els.night.children.map(p => p.textContent), [...LINES, line]);
  assert.equal(sandbox.describeText(job), [...LINES, line].join(' '));
  // a place without a line, or none at all, adds nothing
  show(sandbox, els, { ...JOB, result: { ...JOB.result, place: { source: 'tilt', line: null } } });
  assert.equal(els.night.children.length, LINES.length);
});

test('lore renders one paragraph per constellation, name in bold, and clears', () => {
  const { sandbox, els } = loadPage();
  const lore = [{ abbr: 'Aql', name: 'Aquila', line: "Aquila is the eagle that carried Zeus's thunderbolts; Altair is its eye." }];
  show(sandbox, els, { ...JOB, result: { ...JOB.result, lore } });
  assert.equal(els.lore.children.length, 1);
  const [b, rest] = els.lore.children[0].children;
  assert.equal(b.textContent, 'Aquila');
  assert.equal(rest, " — is the eagle that carried Zeus's thunderbolts; Altair is its eye.");
  sandbox.clearResult();
  assert.equal(els.lore.children.length, 0);
  show(sandbox, els, JOB);
  assert.equal(els.lore.children.length, 0);
});
