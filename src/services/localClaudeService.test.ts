import assert from 'node:assert/strict';
import test from 'node:test';
import { enrichSavedClaudeOverview, claudeDependencyMessage } from './localClaudeService';
import type { ClaudeOverview } from '../types/localClaude';

const saved: ClaudeOverview = {
  profiles: [{ id: 'test-profile', label: 'Test', kind: 'managed', configDir: '/test/cli', desktopDir: '/test/desktop',
    createdAt: 1, selected: true, collectorInstalled: false, autoRefresh: false, renewalDate: null,
    auth: { status: 'unknown', email: null, plan: null, checkedAt: null },
    usage: { fiveHour: null, sevenDay: null, extraUsage: null, observedAt: null, source: 'none', error: null } }],
  selectedProfileId: 'test-profile', desktopRunning: false, codeProcessCount: 0, checkedAt: 1, warning: null,
};

test('a failed follow-up read preserves the successfully created profile and does not expose its error', async () => {
  let reads = 0;
  const result = await enrichSavedClaudeOverview(saved, async () => { reads++; throw new Error('private diagnostic text'); });
  assert.equal(reads, 1);
  assert.deepEqual(result.profiles, saved.profiles);
  assert.equal(result.selectedProfileId, saved.selectedProfileId);
  assert.match(result.warning ?? '', /已保存/);
  assert.doesNotMatch(result.warning ?? '', /private diagnostic/);
});

test('successful follow-up reads use the newest snapshot while retaining a mutation warning', async () => {
  const result = await enrichSavedClaudeOverview({ ...saved, warning: 'saved warning' }, async () => ({ ...saved, checkedAt: 2 }));
  assert.equal(result.checkedAt, 2);
  assert.equal(result.warning, 'saved warning');
});

test('only a fixed dependency error is displayed to the user', () => {
  assert.match(claudeDependencyMessage('CLAUDE_PYTHON_MISSING') ?? '', /Python 3/);
  assert.match(claudeDependencyMessage(new Error('CLAUDE_PYTHON_MISSING')) ?? '', /Python 3/);
  assert.equal(claudeDependencyMessage('private diagnostic text'), null);
});
