import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ensureIntentService, failureAdvice, streamFailure } from '../intent/resolver.ts';

test('healthy service is not restarted', async () => {
  let starts = 0;
  await ensureIntentService('http://127.0.0.1:8011', async () => true, async () => { starts++; });
  assert.equal(starts, 0);
});
test('missing local router starts through hub and verifies readiness', async () => {
  let starts = 0, probes = 0;
  await ensureIntentService('http://127.0.0.1:8011', async () => ++probes > 1, async () => { starts++; });
  assert.equal(starts, 1);
  assert.equal(probes, 2);
});
test('failed verification is not called recovered', async () => {
  await assert.rejects(ensureIntentService('http://127.0.0.1:8011', async () => false, async () => {}), /unavailable/);
});
test('custom remote endpoint never starts a local service', async () => {
  let starts = 0;
  await assert.rejects(ensureIntentService('https://remote.invalid', async () => false, async () => { starts++; }), /custom/);
  assert.equal(starts, 0);
});
test('URL and confirmation guards remain blocked', () => {
  assert.equal(failureAdvice('URL_PROVENANCE_GUARD').code, 'URL_PROVENANCE');
  assert.equal(failureAdvice('NEEDS_CONFIRM').code, 'USER_CONFIRMATION');
  assert.equal(failureAdvice('CAPTCHA').code, 'ACCESS_GATE');
});
test('common failures have bounded safe guidance', () => {
  for (const text of ['LOOP_GUARD', 'NO_MATCH', 'AMBIGUOUS', 'ERR_NAME_NOT_RESOLVED', 'ERR_CERT_AUTHORITY_INVALID', 'ECONNREFUSED', 'TimeoutError', 'PICK_EXPIRED', '429', 'TAB_LIMIT', 'this Pi session has no open page']) {
    assert.notEqual(failureAdvice(text).code, 'UNKNOWN', text);
  }
});
test('lost stream never authorizes replay', () => {
  const r = streamFailure(new Error('connection reset'));
  assert.equal(r.status, 'EXECUTION_UNCERTAIN');
  assert.equal(r.retry_original, false);
  assert.match(r.hint, /resolve/);
});
test('busy planner gets a safe-retry hint, not the may-have-executed timeout hint', () => {
  const advice = failureAdvice('ERROR RuntimeError: planner model at 127.0.0.1:8001 did not answer within 30s (server busy or down); no page action was taken for this step');
  assert.equal(advice.code, 'PLANNER_BUSY');
});
