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
  // The Moon leads because it is the one a person can identify at a glance.
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

test('two planets are offered when no Moon is up', () => {
  const { sandbox } = loadPage();
  // A person pointing at their own photo does not need the Moon to know what
  // they are looking at — that requirement belongs to automatic
  // identification, where the Moon is the only recognisable blob (#91).
  const noMoon = { candidates: [
    { name: 'Venus', kind: 'planet' },
    { name: 'Saturn', kind: 'planet' },
  ] };
  assert.deepEqual([...sandbox.anchorTargets(noMoon)], ['Venus', 'Saturn']);
});

test('the Moon leads when it is up, whatever order it arrives in', () => {
  const { sandbox } = loadPage();
  const moonSecond = { candidates: [
    { name: 'Venus', kind: 'planet' },
    { name: 'Moon', kind: 'moon' },
    { name: 'Saturn', kind: 'planet' },
  ] };
  assert.deepEqual([...sandbox.anchorTargets(moonSecond)], ['Moon', 'Venus']);
});

test('only the Moon takes an article in the prompts', () => {
  const { sandbox } = loadPage();
  // "Point out the Venus and Saturn instead" is how you tell a person the
  // button was written for a different sky than the one they photographed.
  assert.equal(sandbox.bodyLabel('Moon'), 'the Moon');
  assert.equal(sandbox.bodyLabel('Venus'), 'Venus');
  assert.equal(sandbox.bodyLabel('Saturn'), 'Saturn');
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

test('the deepen button never removes the tap-to-place one', () => {
  const { sandbox, els } = loadPage();
  // Both are offered on the same failure. Before #90 the deepen button was
  // installed with replaceChildren, which deleted the tap offer — steering
  // people from a one-second answer into a twenty-minute one by removing
  // the fast option from the page.
  sandbox.renderFailure('abc', {
    error: 'ran out of solve time',
    result: { failure: { reason: 'timeout', can_deepen: true, guess: GUESS } },
  });
  const labels = els.actions.children.map((c) => c.textContent);
  assert.equal(labels.length, 2);
  assert.match(labels[0], /^Point out the Moon and Venus/);
  assert.match(labels[1], /Dig deeper/);
});

test('the no-stars button does not promise a time it cannot keep', () => {
  const { sandbox, els } = loadPage();
  sandbox.renderFailure('abc', {
    error: 'only 4 star-like sources detected',
    result: { failure: { reason: 'no_stars', can_deepen: true } },
  });
  const [btn] = els.actions.children;
  // A budget in CPU seconds runs several times longer in wall clock on a
  // shared vCPU; the old "~2 minutes" was measured at twenty (#90).
  assert.doesNotMatch(btn.textContent, /minute/);
});
