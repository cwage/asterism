// Placing a failed photo by hand: two taps instead of a star match (#85).
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const GUESS = {
  candidates: [
    { name: 'Moon', kind: 'moon', alt_deg: 3 },
    { name: 'Venus', kind: 'planet', alt_deg: 12 },
  ],
};

test('asks for the Moon first, then the brightest other body', () => {
  const { sandbox } = loadPage();
  // The Moon leads because it is the one a person can identify at a glance,
  // and its size is what pins the plate scale.
  // Spread first: the sandbox builds arrays with its own realm's Array, which
  // strict deepEqual counts as a different type.
  assert.deepEqual([...sandbox.anchorTargets(GUESS)], ['Moon', 'Venus']);
});

test('no offer when there is nothing identifiable to point at', () => {
  const { sandbox } = loadPage();
  assert.equal(sandbox.anchorTargets({ candidates: [] }), null);
  assert.equal(sandbox.anchorTargets(null), null);
  // One body alone cannot fix roll or scale.
  assert.equal(sandbox.anchorTargets(
    { candidates: [{ name: 'Moon', kind: 'moon' }] }), null);
  assert.equal(sandbox.anchorTargets(
    { candidates: [{ name: 'Venus', kind: 'planet' }] }), null);
});

test('a tap maps to the photo pixels whatever size it is on screen', () => {
  const { sandbox } = loadPage();
  // A 4000x3000 photo shown 400 px wide: the centre of the element is the
  // centre of the photo, and the scale factor is 10.
  const rect = { left: 20, top: 50, width: 400, height: 300 };
  const natural = { width: 4000, height: 3000 };
  const middle = sandbox.imagePoint(rect, natural, 220, 200);
  assert.equal(Math.round(middle.x), 2000);
  assert.equal(Math.round(middle.y), 1500);

  const corner = sandbox.imagePoint(rect, natural, 20, 50);
  assert.equal(Math.round(corner.x), 0);
  assert.equal(Math.round(corner.y), 0);

  // The offset of the element must be subtracted, or every tap lands low and
  // right by the size of the page header.
  const offsetOnly = sandbox.imagePoint({ left: 0, top: 0, width: 400, height: 300 },
                                        natural, 220, 200);
  assert.notEqual(Math.round(offsetOnly.x), Math.round(middle.x));
});
