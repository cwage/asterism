// The link from a solve to the fly-around page (/fly): the same stars at
// their real distances, seen from outside. That page recovers the camera
// from the solve's named stars and needs three, so the link is offered
// only when there are three.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const star = (name, x, y) => ({ name, x, y, mag: 1, kind: 'star', status: 'matched' });
const job = (labels, extra = {}) => ({ solve_seconds: 2.0, result: { labels, constellations: [] }, ...extra });

function actions(labels, extra) {
  const { sandbox, els } = loadPage();
  sandbox.render('0123456789abcdef0123456789abcdef', job(labels, extra));
  els.photo.onload();
  return els.actions.children;
}
const flyLinks = (children) => children.filter((c) => String(c.href).startsWith('/fly'));

test('a solve with three named stars is offered the view from outside', () => {
  const links = flyLinks(actions([star('Vega', 200, 200), star('Deneb', 500, 300), star('Altair', 400, 700)]));
  assert.equal(links.length, 1);
  assert.equal(links[0].href, '/fly?job=0123456789abcdef0123456789abcdef');
  assert.match(links[0].textContent, /from outside/);
});

test('planets, the Moon and deep-sky objects do not count toward the three', () => {
  const labels = [
    star('Vega', 200, 200), star('Deneb', 500, 300),
    { name: 'Saturn', x: 400, y: 350, mag: 0.7, kind: 'planet' },
    { name: 'Moon', x: 150, y: 150, mag: null, kind: 'moon', phase: 0.4 },
    { name: 'Ring Nebula (M57)', x: 650, y: 350, mag: 8.8, kind: 'dso' },
  ];
  assert.equal(flyLinks(actions(labels)).length, 0);
});

test('a star hidden behind cloud still counts: it is where the camera was pointed', () => {
  const labels = [star('Vega', 200, 200), star('Deneb', 500, 300),
                  { ...star('Altair', 400, 700), status: 'hidden' }];
  assert.equal(flyLinks(actions(labels)).length, 1);
});

test('the link sits with the other things to do, ahead of keeping', () => {
  const children = actions([star('Vega', 200, 200), star('Deneb', 500, 300), star('Altair', 400, 700)],
                           { keep_open: true });
  const fly = children.findIndex((c) => String(c.href).startsWith('/fly'));
  const keep = children.findIndex((c) => /keep/.test(c.textContent));
  assert.ok(fly > 0, 'after the share card');
  assert.ok(keep > fly, 'before keep');
});

test('a browser with no WebGL 2 is not offered a page it cannot draw', () => {
  const { sandbox, els } = loadPage();
  // The harness's elements hand back a 2D recorder for any context; this
  // one has no WebGL to give, as a browser with acceleration off has not.
  const make = sandbox.document.createElement;
  sandbox.document.createElement = () => Object.assign(make(), { getContext: () => null });
  sandbox.render('0123456789abcdef0123456789abcdef',
    job([star('Vega', 200, 200), star('Deneb', 500, 300), star('Altair', 400, 700)]));
  els.photo.onload();
  assert.equal(flyLinks(els.actions.children).length, 0);
});
