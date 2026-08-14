// Satellite crossings (#11) on the canvas: dashed computed tracks, names,
// layer toggle, and the status-line count. Uses the harness's recording
// 2D context, so this exercises draw() itself rather than a stand-in.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const CROSSING = {
  name: 'Iss (Zarya)', norad_id: '25544',
  points: [[100, 100], [200, 150], [300, 200]],
  t_enter_s: 0.0, t_exit_s: 16.0,
};

function job(satellites) {
  return {
    solve_seconds: 3.2,
    result: { labels: [], constellations: [], satellites },
  };
}

function show(sandbox, els, j) {
  sandbox.render('abc', j);
  els.photo.onload();
  return els.overlay.ctx;
}

test('crossing draws a dashed polyline through its points, plus a name', () => {
  const { sandbox, els } = loadPage();
  const ctx = show(sandbox, els, job({ crossings: [CROSSING] }));

  const strokes = ctx.ops.filter(o => o.op === 'stroke');
  assert.equal(strokes.length, 1, 'one track drawn');
  const track = strokes[0];
  assert.ok(track.dashed, 'computed, not pixel-verified: drawn dashed');
  assert.deepEqual(track.path, [
    ['moveTo', 100, 100], ['lineTo', 200, 150], ['lineTo', 300, 200],
  ]);
  // the name lands somewhere, anchored near the track's midpoint
  const name = ctx.ops.find(o => o.op === 'fillText' && o.text === 'Iss (Zarya)');
  assert.ok(name, 'track is labeled');
  assert.ok(Math.abs(name.x - 200) < 200 && Math.abs(name.y - 150) < 200);
});

test('satellites toggle hides tracks without touching other layers', () => {
  const { sandbox, els } = loadPage();
  // elements are created on first getElementById; ask for it, then flip it
  sandbox.document.getElementById('lay-sat').checked = false;
  const ctx = show(sandbox, els, job({ crossings: [CROSSING] }));
  assert.equal(ctx.ops.filter(o => o.op === 'stroke').length, 0);
  assert.ok(!ctx.ops.some(o => o.op === 'fillText' && o.text === 'Iss (Zarya)'));
});

test('degenerate and absent crossings draw nothing', () => {
  const { sandbox, els } = loadPage();
  // a single-point track has no line to draw
  let ctx = show(sandbox, els, job({ crossings: [{ ...CROSSING, points: [[10, 10]] }] }));
  assert.equal(ctx.ops.filter(o => o.op === 'stroke').length, 0);

  // skipped layer (no GPS, no credentials) must not throw
  const fresh = loadPage();
  ctx = show(fresh.sandbox, fresh.els, job({ skipped: 'no_gps' }));
  assert.equal(ctx.ops.filter(o => o.op === 'stroke').length, 0);
});

test('redrawing after a toggle does not leave the old track behind', () => {
  const { sandbox, els } = loadPage();
  const ctx = show(sandbox, els, job({ crossings: [CROSSING] }));
  assert.equal(ctx.ops.filter(o => o.op === 'stroke').length, 1);

  sandbox.document.getElementById('lay-sat').checked = false;
  sandbox.draw();
  assert.equal(ctx.ops.filter(o => o.op === 'stroke').length, 0,
    'the cleared canvas must not still show the previous render');
});

test('status line counts crossings', () => {
  const { sandbox, els } = loadPage();
  show(sandbox, els, job({ crossings: [CROSSING] }));
  assert.match(els.status.textContent, /1 satellite crossing\b/);

  const two = loadPage();
  show(two.sandbox, two.els, job({ crossings: [CROSSING, CROSSING] }));
  assert.match(two.els.status.textContent, /2 satellite crossings/);

  const none = loadPage();
  show(none.sandbox, none.els, job({ skipped: 'no_gps' }));
  assert.doesNotMatch(none.els.status.textContent, /satellite/);
});
