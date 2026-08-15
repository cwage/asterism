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


test('a failed solve with no guess says which EXIF is missing', () => {
  const { sandbox, els } = loadPage();
  sandbox.renderNoGuess('no_timestamp');
  const text = els.guess.children[0].textContent;
  assert.ok(text.includes('no timestamp'), text);
  // The whole point is that a blank panel reads as the site being broken.
  assert.ok(text.length > 40, 'should explain, not just label');
});

test('an unrecognised reason still says something', () => {
  const { sandbox, els } = loadPage();
  sandbox.renderNoGuess('something_new_from_the_backend');
  assert.ok(els.guess.children[0].textContent.length > 0);
});

test('a GPS-backed guess says so instead of hedging', () => {
  const { sandbox } = loadPage();
  const box = sandbox.document.getElementById('guess');
  sandbox.appendGuessHedges(box, { location_source: 'gps',
                                   time_source: 'exif_offset' });
  const texts = box.children.map((c) => c.textContent);
  assert.equal(texts.length, 1);
  assert.ok(texts[0].includes('GPS'), texts[0]);
  assert.ok(!texts[0].includes('estimated'), 'no hedge for a real fix');
});

test('a timezone guess explains why heights are ranges', () => {
  const { sandbox } = loadPage();
  const box = sandbox.document.getElementById('guess');
  sandbox.appendGuessHedges(box, { location_source: 'timezone_guess',
                                   time_source: 'exif_offset' });
  const text = box.children[0].textContent;
  assert.ok(text.includes('timezone'), text);
  assert.ok(text.includes('range'), 'ties the hedge to the ranges shown');
});
