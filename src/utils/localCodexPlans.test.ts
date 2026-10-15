import assert from 'node:assert/strict';
import test from 'node:test';
import {
  creditsToUsd,
  formatCredits,
  formatUsdRange,
  getCodexPlanInfo,
  normalizeCodexPlanType,
  remainingWeeklyUsd,
} from './localCodexPlans';

const BEFORE_CUT = new Date(2026, 9, 2, 12).getTime();
const AFTER_CUT = new Date(2026, 9, 30, 0, 0, 1).getTime();

test('official plan values map to readable tiers', () => {
  assert.equal(getCodexPlanInfo('prolite').label, 'Pro 5x');
  assert.equal(getCodexPlanInfo('pro', BEFORE_CUT).label, 'Pro 20x');
  assert.equal(getCodexPlanInfo('plus').label, 'Plus');
  assert.equal(getCodexPlanInfo('enterprise_cbp_usage_based').weeklyUsd, null);
  assert.equal(getCodexPlanInfo('self_serve_business_usage_based').label, 'Business 按量');
});

test('aliases and casing normalise to the official keys', () => {
  assert.equal(normalizeCodexPlanType('PRO-LITE'), 'prolite');
  assert.equal(normalizeCodexPlanType('pro_20x'), 'pro');
  assert.equal(normalizeCodexPlanType(' Plus '), 'plus');
  assert.equal(normalizeCodexPlanType(undefined), 'unknown');
});

test('weekly dollar estimate scales the Plus range by the official multiplier', () => {
  assert.deepEqual(getCodexPlanInfo('plus').weeklyUsd, [55, 135]);
  assert.deepEqual(getCodexPlanInfo('prolite').weeklyUsd, [275, 675]);
  assert.deepEqual(getCodexPlanInfo('pro', BEFORE_CUT).weeklyUsd, [1100, 2700]);
  assert.equal(formatUsdRange([1100, 2700]), '$1,100–2,700');
});

test('Pro $200 becomes 10x from 2026-10-30', () => {
  const after = getCodexPlanInfo('pro', AFTER_CUT);
  assert.equal(after.label, 'Pro 10x');
  assert.equal(after.multiplier, 10);
  assert.deepEqual(after.weeklyUsd, [550, 1350]);
  assert.equal(getCodexPlanInfo('pro', AFTER_CUT - 2000).label, 'Pro 20x');
  assert.equal(getCodexPlanInfo('prolite', AFTER_CUT).label, 'Pro 5x');
});

test('credits convert at the official $40 per 1,000', () => {
  assert.equal(creditsToUsd(62_500), 2500);
  assert.equal(creditsToUsd(1_000), 40);
  assert.equal(creditsToUsd(12.5), 0.5);
  assert.equal(formatCredits(62_500), '62,500');
});

test('remaining dollars follow the remaining weekly percentage', () => {
  assert.deepEqual(remainingWeeklyUsd(getCodexPlanInfo('prolite'), 40), [110, 270]);
  assert.equal(remainingWeeklyUsd(getCodexPlanInfo('prolite'), null), null);
  assert.equal(remainingWeeklyUsd(getCodexPlanInfo('something_new'), 50), null);
});
