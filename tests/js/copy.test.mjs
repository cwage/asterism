// Copy controls (#124): the narration as a caption or alt text, and the
// label list, one tap each. The text builders are checked on their own,
// then the buttons are clicked the way a browser would and the clipboard
// shim in the harness records what landed.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const JOB = {
  solve_seconds: 2.1,
  result: {
    labels: [
      { name: 'Vega', x: 200, y: 200, mag: 0.03, kind: 'star', status: 'matched' },
      { name: 'Albireo', x: 500, y: 300, mag: 3.05, kind: 'star', status: 'hidden' },
      { name: 'Sulafat', x: 300, y: 250, mag: 3.25, kind: 'star', status: 'matched' },
      { name: 'Saturn', x: 400, y: 350, mag: 0.7, kind: 'planet' },
      { name: 'Ring Nebula (M57)', x: 650, y: 350, mag: 8.8, kind: 'dso' },
    ],
    constellations: [
      { name: 'Lyra', abbr: 'Lyr', segments: [[100, 500, 300, 550]] },
      { name: 'Cygnus', abbr: 'Cyg', segments: [[600, 500, 800, 550]] },
    ],
    narration: { caption: 'Saturn beside Vega', text: 'Saturn sits below Vega tonight.', model: 'm' },
  },
};

function show(sandbox, els, job = JOB) {
  sandbox.render('abc', job);
  els.photo.onload();
  return els.actions.children;
}

test('describeText is the caption and the narration, blank line between', () => {
  const { sandbox } = loadPage();
  assert.equal(sandbox.describeText(JOB),
               'Saturn beside Vega\n\nSaturn sits below Vega tonight.');
  // a caption alone, or text alone, is still worth copying
  assert.equal(sandbox.describeText({ result: { narration: { text: 'Just text.' } } }),
               'Just text.');
  assert.equal(sandbox.describeText({ result: { labels: [] } }), '');
});

test('starListText groups by kind, skips cloud-hidden labels, ends with constellations', () => {
  const { sandbox } = loadPage();
  assert.equal(sandbox.starListText(JOB), [
    'Solar system: Saturn',
    'Deep-sky: Ring Nebula (M57)',
    'Stars: Vega, Sulafat',
    'Constellations: Lyra, Cygnus',
  ].join('\n'));
  // labels without a kind are stars, the way the card treats them
  assert.equal(sandbox.starListText({ result: { labels: [{ name: 'Deneb' }] } }),
               'Stars: Deneb');
  assert.equal(sandbox.starListText({ result: { labels: [] } }), '');
});

test('a solved result gets both copy buttons beside the card link', async () => {
  const { sandbox, els, clipboard } = loadPage();
  const [card, describe, stars] = show(sandbox, els);
  assert.ok(card.href.includes('/jobs/abc/card'));
  assert.equal(describe.textContent, 'copy description');
  assert.equal(stars.textContent, 'copy star list');

  await describe.dispatch('click');
  assert.deepEqual(clipboard, [sandbox.describeText(JOB)]);
  assert.equal(describe.textContent, 'copied');

  await stars.dispatch('click');
  assert.deepEqual(clipboard[1], sandbox.starListText(JOB));
  assert.equal(stars.textContent, 'copied');

  // the label comes back once the moment has passed
  await new Promise(r => setTimeout(r, 1600));
  assert.equal(describe.textContent, 'copy description');
  assert.equal(stars.textContent, 'copy star list');
});

test('no narration means no description button, the star list stays', () => {
  const { sandbox, els } = loadPage();
  const job = { ...JOB, result: { ...JOB.result, narration: undefined } };
  const children = show(sandbox, els, job);
  assert.equal(children.length, 2);
  assert.equal(children[1].textContent, 'copy star list');
});

test('a refused clipboard write says so instead of doing nothing', async () => {
  const { sandbox, els, clipboard } = loadPage();
  sandbox.navigator.clipboard.writeText = async () => { throw new Error('denied'); };
  const [, describe] = show(sandbox, els);
  await describe.dispatch('click');
  assert.equal(clipboard.length, 0);
  assert.equal(describe.textContent, 'copy failed');
});
