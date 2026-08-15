// First-visit "What is this?" explainer (#74).
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

test('opens on a first visit and remembers being dismissed', async () => {
  const { sandbox, els, store } = loadPage();
  assert.equal(sandbox.shouldAutoOpenIntro(''), true, 'nothing stored yet');

  sandbox.openIntro();
  assert.ok(els['intro-overlay'].className.includes('open'));

  sandbox.closeIntro();
  assert.ok(!els['intro-overlay'].className.includes('open'));
  assert.ok(store.asterismIntroSeen, 'dismissal is remembered');
  assert.equal(sandbox.shouldAutoOpenIntro(''), false, 'stays shut next time');
});

test('a shared result link is its own explanation', async () => {
  const { sandbox } = loadPage();
  // The photo and its labels answer "what is this" better than a modal, and
  // covering someone's shared result with one is the worse first impression.
  assert.equal(sandbox.shouldAutoOpenIntro('?job=abc123'), false);
  assert.equal(sandbox.shouldAutoOpenIntro('?job=abc123&x=1'), false);
  assert.equal(sandbox.shouldAutoOpenIntro('?other=1'), true);
});

test('unavailable storage suppresses the dialog rather than repeating it', () => {
  const { sandbox } = loadPage();
  sandbox.localStorage.getItem = () => { throw new Error('denied'); };
  // Safari private mode and friends: showing it every single visit would be
  // worse than not showing it at all.
  assert.equal(sandbox.shouldAutoOpenIntro(''), false);
});

test('dismissing survives storage that refuses writes', () => {
  const { sandbox, els } = loadPage();
  sandbox.localStorage.setItem = () => { throw new Error('denied'); };
  sandbox.openIntro();
  sandbox.closeIntro();
  assert.ok(!els['intro-overlay'].className.includes('open'),
            'the dialog still closes even if the visit cannot be recorded');
});
