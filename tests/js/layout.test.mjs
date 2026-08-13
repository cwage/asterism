// Label layout (#18): overlap geometry, candidate fallback, layer/mag
// gating, and text priority — the pure half of draw().
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

test('rectsOverlap: overlap yes, disjoint no, edge-adjacency no', () => {
  const { sandbox: s } = loadPage();
  assert.ok(s.rectsOverlap({ x: 0, y: 0, w: 10, h: 10 }, { x: 5, y: 5, w: 10, h: 10 }));
  assert.ok(!s.rectsOverlap({ x: 0, y: 0, w: 10, h: 10 }, { x: 20, y: 0, w: 10, h: 10 }));
  assert.ok(!s.rectsOverlap({ x: 0, y: 0, w: 10, h: 10 }, { x: 10, y: 0, w: 10, h: 10 }));
});

test('placeText: first fit wins, collisions fall through, off-frame rejected', () => {
  const { sandbox: s } = loadPage();
  const placed = [];
  // Spread vm-created objects into host-realm literals: deepEqual (strict)
  // also compares prototypes, which differ across vm contexts.
  assert.deepEqual({ ...s.placeText(placed, [{ x: 10, y: 10 }, { x: 50, y: 50 }], 20, 10, 200, 200) },
    { x: 10, y: 10, w: 20, h: 10 });
  assert.deepEqual({ ...s.placeText(placed, [{ x: 15, y: 12 }, { x: 50, y: 50 }], 20, 10, 200, 200) },
    { x: 50, y: 50, w: 20, h: 10 }, 'collision falls through to next candidate');
  assert.equal(s.placeText(placed, [{ x: -5, y: 0 }, { x: 190, y: 0 }, { x: 100, y: 195 }],
    20, 10, 200, 200), null, 'off-frame candidates rejected');
  const before = placed.length;
  assert.equal(s.placeText(placed, [{ x: 12, y: 11 }], 20, 10, 200, 200), null);
  assert.equal(placed.length, before, 'failed placement leaves no residue');
});

test('crowded field: four directions fill, fifth yields', () => {
  const { sandbox: s } = loadPage();
  const placed = [];
  let landed = 0;
  for (let i = 0; i < 5; i++) {
    const cands = [{ x: 100, y: 95 }, { x: 60, y: 95 }, { x: 80, y: 75 }, { x: 80, y: 115 }];
    if (s.placeText(placed, cands, 30, 12, 200, 200)) landed++;
  }
  assert.equal(landed, 4);
});

test('labelVisible: mag slider gates stars only, toggles gate kinds', () => {
  const { sandbox: s } = loadPage();
  const st = { stars: true, solar: true, dso: true, constellations: true, magLimit: 3 };
  assert.ok(s.labelVisible({ name: 'Vega', mag: 0.03 }, st));
  assert.ok(!s.labelVisible({ name: 'faint', mag: 4.4 }, st));
  assert.ok(s.labelVisible({ name: 'M31', kind: 'dso', mag: 3.6 }, st));
  assert.ok(s.labelVisible({ name: 'Moon', kind: 'moon', mag: null }, st));
  assert.ok(!s.labelVisible({ name: 'Vega', mag: 0.03 }, { ...st, stars: false }));
  assert.ok(!s.labelVisible({ name: 'M31', kind: 'dso' }, { ...st, dso: false }));
  assert.ok(!s.labelVisible({ name: 'Mars', kind: 'planet' }, { ...st, solar: false }));
});

test('labelPriority: moon, planets, DSOs, then stars brightest-first', () => {
  const { sandbox: s } = loadPage();
  const order = [
    { name: 'faint', mag: 4.4 },
    { name: 'Moon', kind: 'moon', mag: null },
    { name: 'M31', kind: 'dso', mag: 3.6 },
    { name: 'Vega', mag: 0.03 },
    { name: 'Mars', kind: 'planet', mag: 1.2 },
  ].sort((a, b) => s.labelPriority(a) - s.labelPriority(b)).map(l => l.name);
  assert.deepEqual(order, ['Moon', 'Mars', 'M31', 'Vega', 'faint']);
});
