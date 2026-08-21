// Failed-solve retry advice: the worker's diagnosis code becomes copy that
// says what happened and what to do differently.
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage } from './harness.mjs';

test('a short-exposure failure explains night mode and holding still', () => {
  const { sandbox, els } = loadPage();
  sandbox.renderFailure('j1', {
    error: 'only 0 star-like sources detected',
    result: { failure: { reason: 'no_stars', advice: 'short_exposure',
                         guess_unavailable: 'no_timestamp',
                         can_deepen: true } },
  });
  const text = els.advice.children[0].textContent;
  assert.ok(text.includes('night'), text);
  assert.ok(text.includes('still'), text);
  // Advice complements the no-guess explainer; it must not replace it.
  assert.ok(els.guess.children.length > 0, 'guess panel still renders');
});

test('every worker advice code has real copy', () => {
  const { sandbox, els } = loadPage();
  for (const code of ['daylight', 'twilight', 'short_exposure',
                      'dark_but_empty']) {
    sandbox.renderAdvice(code);
    assert.ok(els.advice.children[0].textContent.length > 60,
              `should explain, not just label: ${code}`);
  }
});

// The harness creates elements lazily on first getElementById, so an
// untouched panel shows up as `undefined` — as empty as empty gets.
const panelEmpty = (el) => !el || el.children.length === 0;

test('an unknown advice code renders nothing rather than guessing', () => {
  const { sandbox, els } = loadPage();
  sandbox.renderAdvice('something_new_from_the_backend');
  assert.ok(panelEmpty(els.advice));
});

test('a failure without advice leaves the panel empty', () => {
  const { sandbox, els } = loadPage();
  sandbox.renderFailure('j1', {
    error: 'no solution',
    result: { failure: { reason: 'no_match',
                         guess_unavailable: 'no_timestamp' } },
  });
  assert.ok(panelEmpty(els.advice));
});
