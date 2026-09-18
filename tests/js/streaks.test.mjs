// Streaks found in the pixels on the canvas: a solid bracket either side
// of the line, an arrowhead for a meteor, the verdict as its name, the
// layer toggle, and the status-line summary with its hedge.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const METEOR = {
  start: [100, 100], end: [400, 100], length_deg: 5.2,
  kind: 'meteor', confidence: 'medium', shower: null,
  reasons: ['fades in, brightens along its path, and stops'],
};
const SATELLITE = {
  start: [100, 300], end: [400, 300], length_deg: 2.1,
  kind: 'satellite', confidence: 'high',
  satellite: { name: 'Iss (Zarya)', norad_id: '25544' },
};

function job(streaks) {
  return {
    solve_seconds: 3.2,
    result: { labels: [], constellations: [], streaks },
  };
}

function show(sandbox, els, j) {
  sandbox.render('abc', j);
  els.photo.onload();
  return els.overlay.ctx;
}

test('a meteor draws two solid bracket lines beside it and an arrowhead at its end', () => {
  const { sandbox, els } = loadPage();
  const ctx = show(sandbox, els, job({ streaks: [METEOR] }));
  const strokes = ctx.ops.filter(o => o.op === 'stroke');
  assert.equal(strokes.length, 2, 'one bracket line either side');
  for (const s of strokes) {
    assert.ok(!s.dashed, 'pixel-detected: drawn solid');
    // parallel to the streak, offset off it
    assert.equal(s.path[0][2], s.path[1][2]);
    assert.notEqual(s.path[0][2], 100);
  }
  const fills = ctx.ops.filter(o => o.op === 'fill');
  assert.equal(fills.length, 1, 'arrowhead');
  // the arrow's tip lies past the end point, along the streak
  assert.ok(fills[0].path[0][1] > 400 && Math.abs(fills[0].path[0][2] - 100) < 1);
  const name = ctx.ops.find(o => o.op === 'fillText' && o.text === 'meteor');
  assert.ok(name, 'labelled with the verdict');
  assert.ok(Math.abs(name.x - 250) < 100 && Math.abs(name.y - 100) < 60);
});

test('a satellite is named and gets no arrowhead', () => {
  const { sandbox, els } = loadPage();
  const ctx = show(sandbox, els, job({ streaks: [SATELLITE] }));
  assert.equal(ctx.ops.filter(o => o.op === 'fill').length, 0);
  assert.ok(ctx.ops.some(o => o.op === 'fillText' && o.text === 'Iss (Zarya)'));
});

test('a shower meteor and an unknown streak are named accordingly', () => {
  const { sandbox, els } = loadPage();
  const ctx = show(sandbox, els, job({ streaks: [
    { ...METEOR, shower: { name: 'Perseids' } },
    { ...METEOR, start: [100, 500], end: [400, 500], kind: 'unknown', confidence: 'low' },
  ] }));
  const texts = ctx.ops.filter(o => o.op === 'fillText').map(o => o.text);
  assert.ok(texts.includes('Perseids meteor'));
  assert.ok(texts.includes('streak'));
});

test('streaks toggle hides them without touching other layers', () => {
  const { sandbox, els } = loadPage();
  sandbox.document.getElementById('lay-streak').checked = false;
  const ctx = show(sandbox, els, job({ streaks: [METEOR] }));
  assert.equal(ctx.ops.filter(o => o.op === 'stroke').length, 0);
  assert.equal(ctx.ops.filter(o => o.op === 'fill').length, 0);
  assert.ok(!ctx.ops.some(o => o.op === 'fillText' && o.text === 'meteor'));
});

test('absent or failed layer draws nothing and does not throw', () => {
  const { sandbox, els } = loadPage();
  let ctx = show(sandbox, els, job(undefined));
  assert.equal(ctx.ops.filter(o => o.op === 'stroke').length, 0);
  const fresh = loadPage();
  ctx = show(fresh.sandbox, fresh.els, job({ streaks: [], error: 'streak detection failed' }));
  assert.equal(ctx.ops.filter(o => o.op === 'stroke').length, 0);
});

test('status line states the verdict with a hedge sized to the confidence', () => {
  const { sandbox, els } = loadPage();
  show(sandbox, els, job({ streaks: [METEOR] }));
  assert.match(els.status.textContent, /a 5\.2° sporadic meteor \(likely\)/);

  const sat = loadPage();
  show(sat.sandbox, sat.els, job({ streaks: [SATELLITE] }));
  assert.match(sat.els.status.textContent, /a 2\.1° Iss \(Zarya\)(?! \()/);

  const shower = loadPage();
  show(shower.sandbox, shower.els, job({ streaks: [
    { ...METEOR, confidence: 'high', shower: { name: 'Geminids' } }] }));
  assert.match(shower.els.status.textContent, /a 5\.2° Geminids meteor(?! \()/);

  const unknown = loadPage();
  show(unknown.sandbox, unknown.els, job({ streaks: [
    { ...METEOR, kind: 'unknown', confidence: 'low', length_deg: null }] }));
  assert.match(unknown.els.status.textContent, /a streak of unknown origin/);

  const low = loadPage();
  show(low.sandbox, low.els, job({ streaks: [
    { ...SATELLITE, satellite: undefined, confidence: 'low' }] }));
  assert.match(low.els.status.textContent, /a 2\.1° streak \(possibly a satellite\)/);

  const lowMeteor = loadPage();
  show(lowMeteor.sandbox, lowMeteor.els, job({ streaks: [{ ...METEOR, confidence: 'low' }] }));
  assert.match(lowMeteor.els.status.textContent, /a 5\.2° streak \(possibly a sporadic meteor\)/);
});

test('a low-confidence verdict is drawn as a plain streak, arrowhead and all withheld', () => {
  const { sandbox, els } = loadPage();
  const ctx = show(sandbox, els, job({ streaks: [{ ...METEOR, confidence: 'low' }] }));
  assert.equal(ctx.ops.filter(o => o.op === 'stroke').length, 2);
  assert.ok(ctx.ops.some(o => o.op === 'fillText' && o.text === 'streak'));
  assert.ok(!ctx.ops.some(o => o.op === 'fillText' && o.text === 'meteor'));
});
