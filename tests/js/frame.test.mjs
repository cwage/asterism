// On a phone the photo's frame takes its width from a viewport-height cap
// and the photo's aspect ratio, and on any screen it stops at the photo's
// own pixel width (#wrap in the stylesheet); both reach the frame through
// the photo's load event. Other tests call the per-path onload handlers by
// hand; this one delivers the event the way a browser would, since a
// ratio that never lands leaves the frame square with the overlay drawn
// over the wrong pixels, and a width that never lands blows a small
// upload up to the column.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

test('the photo load event puts its ratio and pixel width on the frame', async () => {
  const { els } = loadPage();
  els.photo.naturalWidth = 3000;
  els.photo.naturalHeight = 4000;
  await els.photo.dispatch('load');
  assert.equal(els.wrap.style['--ar'], 0.75);
  assert.equal(els.wrap.style['--nw'], 3000);
});

test('the next photo replaces the ratio of the last', async () => {
  const { els } = loadPage();
  await els.photo.dispatch('load');  // the harness photo is 1000 by 800
  assert.equal(els.wrap.style['--ar'], 1.25);
  els.photo.naturalWidth = 800;
  els.photo.naturalHeight = 1000;
  await els.photo.dispatch('load');
  assert.equal(els.wrap.style['--ar'], 0.8);
  assert.equal(els.wrap.style['--nw'], 800);
});
