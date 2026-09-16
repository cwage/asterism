// "Photo tips" dialog: the failure panel's retry advice, offered up front.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { loadPage } from './harness.mjs';

test('never opens on its own, only on request', () => {
  const { sandbox, els } = loadPage();
  // A first visit auto-opens the explainer; the tips are a second dialog
  // and must not pile on top of it.
  assert.ok(!els['tips-overlay'].className.includes('open'));
  sandbox.openTips();
  assert.ok(els['tips-overlay'].className.includes('open'));
  sandbox.closeTips();
  assert.ok(!els['tips-overlay'].className.includes('open'));
});

test('the header button and the close button are wired', async () => {
  const { els } = loadPage();
  await els['tips-open'].dispatch('click', {});
  assert.ok(els['tips-overlay'].className.includes('open'));
  await els['tips-close'].dispatch('click', {});
  assert.ok(!els['tips-overlay'].className.includes('open'));
});

test('every failure links to the tips, whatever the diagnosis', async () => {
  for (const result of [
    { failure: { reason: 'no_stars', advice: 'short_exposure' } },
    { failure: { reason: 'no_stars', advice: 'short_exposure' },
      narration: { text: 'A quick snap of a dark sky.' } },
    { failure: { reason: 'no_match' } },
  ]) {
    const { sandbox, els } = loadPage();
    sandbox.renderFailure('j1', { error: 'no solution', result });
    const link = els.advice.children.at(-1);
    assert.equal(link.className, 'tips-link', JSON.stringify(result));
    await link.children[0].dispatch('click', {});
    assert.ok(els['tips-overlay'].className.includes('open'),
              JSON.stringify(result));
  }
});

test('the copy names night mode and holding still', () => {
  const html = readFileSync(new URL('../../static/index.html', import.meta.url), 'utf8');
  const tips = html.slice(html.indexOf('id="tips-overlay"'), html.indexOf('id="tips-close"'));
  for (const phrase of ['Night mode', 'Night Sight', 'tripod', 'car roof', 'original'])
    assert.ok(tips.includes(phrase), phrase);
});
