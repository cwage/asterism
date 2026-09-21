// Bright objects just outside the frame (#118): an arrow at the edge with
// the name and distance, gated by its own toggle and by kind, and last in
// line for space so a pointer never steps on a label for something in
// the shot.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const SATURN = { name: 'Saturn', kind: 'planet', mag: 0.7,
                 edge_x: 1000, edge_y: 400, ux: 1, uy: 0, deg: 8.3, side: 'right' };
const PLEIADES = { name: 'Pleiades (M45)', kind: 'dso', mag: 1.6,
                   edge_x: 500, edge_y: 0, ux: 0, uy: -1, deg: 11.6, side: 'above' };
const SIRIUS = { name: 'Sirius', kind: 'star', mag: -1.44,
                 edge_x: 0, edge_y: 600, ux: -1, uy: 0, deg: 3.1, side: 'left' };

function job(extra = {}) {
  return { solve_seconds: 2.0, result: {
    labels: [{ name: 'Vega', x: 200, y: 200, mag: 0.03, kind: 'star' }],
    constellations: [], beyond: [SATURN, PLEIADES, SIRIUS], ...extra } };
}

function show(sandbox, els, j) {
  sandbox.render('abc', j);
  els.photo.onload();
  return els.overlay.ctx;
}
const texts = ctx => ctx.ops.filter(o => o.op === 'fillText').map(o => o.text);
const reaches = (ctx, x, y) => ctx.ops.filter(o => o.op === 'stroke')
  .find(s => s.path.some(seg => seg[0] === 'lineTo' && seg[1] === x && seg[2] === y));

test('a pointer is an arrow to the edge crossing plus the name and distance', () => {
  const { sandbox, els } = loadPage();
  const ctx = show(sandbox, els, job());
  const t = texts(ctx);
  assert.ok(t.includes('Saturn 8°'));
  assert.ok(t.includes('Pleiades (M45) 12°'));
  assert.ok(t.includes('Sirius 3°'));
  const arrow = reaches(ctx, 1000, 400);
  assert.ok(arrow, 'an arrow should reach the edge at (1000, 400)');
  // one colour of their own, not the planet's: the point is "not in the shot"
  assert.equal(arrow.strokeStyle, 'rgba(255, 140, 110, 0.95)');
  assert.equal(reaches(ctx, 0, 600).strokeStyle, arrow.strokeStyle, 'same for a star');
  const label = ctx.ops.find(o => o.op === 'fillText' && o.text === 'Saturn 8°');
  assert.equal(label.fillStyle, 'rgba(255, 140, 110, 0.95)');
  assert.ok(els.status.textContent.includes('3 just outside the frame'));
});

test('the layer toggle and the kind toggles both gate pointers', async () => {
  const { sandbox, els } = loadPage();
  const ctx = show(sandbox, els, job());
  const solar = sandbox.document.getElementById('lay-solar');
  solar.checked = false;
  await solar.dispatch('change');
  let t = texts(ctx);
  assert.ok(!t.includes('Saturn 8°'), 'a planet pointer follows the solar toggle');
  assert.ok(t.includes('Pleiades (M45) 12°') && t.includes('Sirius 3°'));
  solar.checked = true;
  const layer = sandbox.document.getElementById('lay-beyond');
  layer.checked = false;
  await layer.dispatch('change');
  t = texts(ctx);
  assert.ok(!t.some(s => s.endsWith('°')), 'the layer toggle clears them all');
  assert.ok(t.includes('Vega'), 'the in-frame label is untouched');
});

test('a pointer whose arrow would land on a marker is dropped whole', () => {
  const { sandbox, els } = loadPage();
  // a star marker right where Saturn's arrow meets the edge
  const ctx = show(sandbox, els, job({
    labels: [{ name: 'Edge', x: 990, y: 400, mag: 1.0, kind: 'star' }] }));
  const t = texts(ctx);
  assert.ok(t.includes('Edge'), 'the real label wins');
  assert.ok(!t.includes('Saturn 8°'));
  assert.ok(!reaches(ctx, 1000, 400), 'no arrow either');
  assert.ok(t.includes('Pleiades (M45) 12°'), 'the others still draw');
});

test('a long pointer label moves to another row around a star', () => {
  const { sandbox, els } = loadPage();
  const pointer = { ...SIRIUS, name: 'Andromeda Galaxy (M31)', kind: 'dso',
                    edge_y: 400, deg: 7.8 };
  const ctx = sandbox.document.getElementById('overlay').getContext('2d');
  // Font widths vary across browsers. Here the full name reaches a star
  // beside the arrow, as on the 0d0fb65e solve with its Matar label.
  ctx.measureText = text => ({ width: text.startsWith('Andromeda') ? 270 : text.length * 7 });
  show(sandbox, els, job({ beyond: [pointer],
    labels: [{ name: 'Star', x: 300, y: 400, mag: 1, kind: 'star' }] }));
  assert.ok(texts(ctx).includes('Andromeda Galaxy (M31) 8°'));
  assert.ok(texts(ctx).includes('Star'));
  assert.ok(reaches(ctx, 0, 400));
});

test('an unplaceable pointer leaves no arrow or reservation for the next pointer', () => {
  const { sandbox, els } = loadPage();
  const tooWide = { ...SIRIUS, name: 'Andromeda Galaxy (M31)', kind: 'dso' };
  const ctx = sandbox.document.getElementById('overlay').getContext('2d');
  ctx.measureText = text => ({ width: text.startsWith('Andromeda') ? 2000 : text.length * 7 });
  show(sandbox, els, job({ labels: [], beyond: [tooWide] }));
  assert.ok(!reaches(ctx, 0, 600), 'no anonymous arrow when the text cannot fit');
  show(sandbox, els, job({ labels: [], beyond: [tooWide, SIRIUS] }));
  assert.deepEqual(texts(ctx), ['Sirius 3°']);
  assert.ok(reaches(ctx, 0, 600), 'the rejected pointer must leave the edge available');
});

test('the star list names them with distance and side', () => {
  const { sandbox } = loadPage();
  assert.ok(sandbox.starListText(job()).endsWith(
    'Just outside the frame: Saturn (8° right), Pleiades (M45) (12° above), Sirius (3° left)'));
});

test('distances keep a tenth under one degree and round half-up above', () => {
  const { sandbox } = loadPage();
  assert.equal(sandbox.formatDeg(0.4), '0.4°');   // just past the edge, not "0°"
  assert.equal(sandbox.formatDeg(8.5), '9°');     // the same rule as the server side
  assert.equal(sandbox.formatDeg(11.6), '12°');
  assert.equal(sandbox.formatDeg(1.0), '1°');
});

test('a result without pointers draws and reads as before', () => {
  const { sandbox, els } = loadPage();
  const j = job();
  delete j.result.beyond;
  const ctx = show(sandbox, els, j);
  assert.deepEqual(texts(ctx), ['Vega']);
  assert.ok(!els.status.textContent.includes('outside'));
  assert.ok(!sandbox.starListText(j).includes('outside'));
});
