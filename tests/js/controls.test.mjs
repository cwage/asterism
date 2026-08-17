// Layer toggles and the magnitude slider are wired to draw(). The gating
// itself is covered in layout.test.mjs; what this file guards is that
// flipping a control actually triggers a render. Those are separate
// failures: the satellite toggle gated draw() correctly for weeks while
// never causing one, so the checkbox moved and the canvas did not. Tests
// that call draw() by hand cannot see that — these dispatch the events a
// browser would.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

// One label per layer, spread far enough apart that text placement never
// has to fall back or yield — a missing name then means the layer is gone,
// not that it lost a fight for space.
const JOB = {
  solve_seconds: 3.2,
  result: {
    labels: [
      { name: 'Vega', x: 200, y: 200, mag: 0.03, kind: 'star' },
      { name: 'Faint', x: 800, y: 200, mag: 4.4, kind: 'star' },
      { name: 'Mars', x: 400, y: 350, mag: 1.2, kind: 'planet' },
      { name: 'M31', x: 650, y: 350, mag: 3.6, kind: 'dso' },
    ],
    constellations: [{ name: 'Orion', abbr: 'Ori', segments: [[100, 500, 300, 550]] }],
    satellites: {
      crossings: [{
        name: 'Iss (Zarya)', norad_id: '25544',
        points: [[100, 700], [300, 720], [500, 740]],
      }],
    },
  },
};

// (checkbox id, the label only that layer draws)
const LAYERS = [
  ['lay-stars', 'Vega'],
  ['lay-solar', 'Mars'],
  ['lay-dso', 'M31'],
  ['lay-con', 'Orion'],
  ['lay-sat', 'Iss (Zarya)'],
];

function show(sandbox, els) {
  sandbox.render('abc', JOB);
  els.photo.onload();
  return els.overlay.ctx;
}

const drew = (ctx, text) => ctx.ops.some(o => o.op === 'fillText' && o.text === text);

for (const [layId, text] of LAYERS) {
  test(`${layId} redraws the canvas when it changes`, async () => {
    const { sandbox, els } = loadPage();
    const ctx = show(sandbox, els);
    assert.ok(drew(ctx, text), `${text} should be on the canvas before toggling`);

    const box = sandbox.document.getElementById(layId);
    box.checked = false;
    await box.dispatch('change');

    assert.ok(!drew(ctx, text),
      `unchecking ${layId} must trigger a redraw that drops ${text}`);
  });
}

test('the magnitude slider redraws and reports its value', async () => {
  const { sandbox, els } = loadPage();
  const ctx = show(sandbox, els);
  assert.ok(drew(ctx, 'Faint'), 'mag 4.4 is visible at the default limit');

  const slider = sandbox.document.getElementById('mag-limit');
  slider.value = '1';
  // 'input', not 'change': a range slider fires change only on release, so
  // the label would lag the drag by a mouse-up.
  await slider.dispatch('input');

  assert.ok(!drew(ctx, 'Faint'), 'mag 4.4 drops below a limit of 1');
  assert.ok(drew(ctx, 'Vega'), 'mag 0.03 survives it');
  assert.equal(els['mag-val'].textContent, '1');
});
