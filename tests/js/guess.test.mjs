// How the no-solve guess describes height above the horizon (#79, #80).
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

test('a known location quotes a single altitude', () => {
  const { sandbox } = loadPage();
  assert.equal(sandbox.altPhrase({ alt_deg: 12 }), '12° up');
});

test('a timezone guess quotes the span it actually knows', () => {
  const { sandbox } = loadPage();
  // Without GPS the location is known to a zone, which near the horizon is
  // worth several degrees either way.
  assert.equal(sandbox.altPhrase({ alt_deg: 8, alt_range_deg: [3, 14] }),
               '3–14° up');
});

test('a span that crosses the horizon never prints a negative altitude', () => {
  const { sandbox } = loadPage();
  // The case from the dusk walk: the band centre puts the Moon just below the
  // horizon while the western end of the zone puts it clearly above. Saying
  // "-1° up" about something we just listed as visible is nonsense.
  const phrase = sandbox.altPhrase({ alt_deg: -1, alt_range_deg: [-7, 5] });
  assert.equal(phrase, 'right around the horizon');
  assert.ok(!phrase.includes('-'), 'no negative altitude in the copy');
});

test('a degenerate range falls back to the single figure', () => {
  const { sandbox } = loadPage();
  assert.equal(sandbox.altPhrase({ alt_deg: 5, alt_range_deg: [5, 5] }), '5° up');
});
