// "Photo tips" dialog: the failure panel's retry advice, offered up front.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

const isOpen = (els) => els['tips-overlay'].className.includes('open');

test('never opens on its own, only on request', () => {
  const { sandbox, els } = loadPage();
  // A first visit auto-opens the explainer; the tips are a second dialog
  // and must not pile on top of it.
  assert.ok(!isOpen(els));
  sandbox.openTips();
  assert.ok(isOpen(els));
  sandbox.closeTips();
  assert.ok(!isOpen(els));
});

test('the header button and the close button are wired', async () => {
  const { els } = loadPage();
  await els['tips-open'].dispatch('click', {});
  assert.ok(isOpen(els));
  await els['tips-close'].dispatch('click', {});
  assert.ok(!isOpen(els));
});

test('a backdrop click closes it; a click inside does not', async () => {
  const { els } = loadPage();
  await els['tips-open'].dispatch('click', {});
  await els['tips-overlay'].dispatch('click', { target: els['tips-close'] });
  assert.ok(isOpen(els), 'a click inside the dialog must not dismiss it');
  await els['tips-overlay'].dispatch('click', { target: els['tips-overlay'] });
  assert.ok(!isOpen(els));
});

test('Escape closes it, alongside the other dialogs', async () => {
  const { sandbox, els } = loadPage();
  sandbox.openIntro();
  await els['tips-open'].dispatch('click', {});
  await sandbox.document.dispatch('keydown', { key: 'a' });
  assert.ok(isOpen(els), 'other keys leave it alone');
  await sandbox.document.dispatch('keydown', { key: 'Escape' });
  assert.ok(!isOpen(els));
  assert.ok(!els['intro-overlay'].className.includes('open'));
});

test('closing returns focus to whichever control opened it', async () => {
  const { sandbox, els } = loadPage();
  const focused = [];
  els['tips-open'].focus = () => focused.push('header');
  await els['tips-open'].dispatch('click', {});
  sandbox.closeTips();
  assert.deepEqual(focused, ['header']);

  // Opened from a failure's explanation, a keyboard user must land back on
  // that explanation, not on the header button far above it.
  sandbox.renderFailure('j1', { error: 'no solution', result: { failure: { reason: 'no_match' } } });
  const link = els.advice.children.at(-1).children[0];
  link.focus = () => focused.push('failure link');
  await link.dispatch('click', {});
  assert.ok(isOpen(els));
  sandbox.closeTips();
  assert.deepEqual(focused, ['header', 'failure link']);

  // Closing what is already closed moves focus nowhere.
  sandbox.closeTips();
  assert.deepEqual(focused, ['header', 'failure link']);
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
    assert.ok(isOpen(els), JSON.stringify(result));
  }
});

test('the copy names night mode and holding still', () => {
  const { html } = loadPage();
  const tips = html.slice(html.indexOf('id="tips-overlay"'), html.indexOf('id="tips-close"'));
  for (const phrase of ['Night mode', 'Night Sight', 'tripod', 'car roof', 'original'])
    assert.ok(tips.includes(phrase), phrase);
});
