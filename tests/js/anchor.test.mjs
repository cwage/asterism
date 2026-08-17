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

test('two taps on the frame post both anchors in photo pixels', async () => {
  const { sandbox, els } = loadPage();
  // The whole feature, end to end through the DOM: the button's handler
  // installs a tap listener, two taps land, and the anchors are POSTed.
  // Before this test the harness ignored addEventListener entirely, which
  // is how a listener bound to a covered element shipped twice.
  const calls = [];
  sandbox.fetch = async (url, opts) => {
    calls.push({ url, body: opts && JSON.parse(opts.body) });
    if (url.endsWith('/anchor')) return { ok: true };
    return new Promise(() => {});  // the poll that follows; never settles
  };

  sandbox.startAnchoring('job1', ['Moon', 'Venus']);
  const wrap = els.wrap;
  const photo = els.photo;
  photo.rect = { left: 0, top: 0, width: 500, height: 400 };  // shown at half size
  photo.naturalWidth = 1000;
  photo.naturalHeight = 800;

  await wrap.dispatch('click', { clientX: 100, clientY: 50 });
  await wrap.dispatch('click', { clientX: 400, clientY: 200 });

  const posted = calls.find((c) => c.url.endsWith('/anchor'));
  assert.ok(posted, 'the anchors were never posted — the taps went nowhere');
  assert.deepEqual([...posted.body.anchors].map((a) => a.name), ['Moon', 'Venus']);
  // Displayed at half size, so photo pixels are twice the client offsets.
  assert.deepEqual(
    [...posted.body.anchors].map((a) => [a.x, a.y]), [[200, 100], [800, 400]]);
});

test('the overlay canvas never swallows a tap', () => {
  const { html } = loadPage();
  // This one is CSS, not script. The canvas is absolutely positioned across
  // the whole photo, so without pointer-events: none it is the click target
  // for every pixel of it and tap-to-place silently does nothing.
  const rule = html.match(/#wrap canvas \{[^}]*\}/);
  assert.ok(rule, 'no #wrap canvas rule found');
  assert.match(rule[0], /pointer-events:\s*none/);
});

test('a third tap after both anchors is ignored', async () => {
  const { sandbox, els } = loadPage();
  const calls = [];
  sandbox.fetch = async (url, opts) => {
    calls.push(url);
    if (url.endsWith('/anchor')) return { ok: true };
    return new Promise(() => {});
  };
  sandbox.startAnchoring('job1', ['Moon', 'Venus']);
  for (let i = 0; i < 3; i++)
    await els.wrap.dispatch('click', { clientX: 10 * i, clientY: 10 });
  assert.equal(calls.filter((u) => u.endsWith('/anchor')).length, 1);
});
