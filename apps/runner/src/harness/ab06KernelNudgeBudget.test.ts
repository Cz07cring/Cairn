/**
 * 第三百五十一批：Kernel hydrate 后进程内 registry 可模拟重启恢复；≠ DONE。
 */
import {afterEach, describe, expect, test} from 'vitest';
import {createDefaultActivationGuards} from './activationGuards.js';
import {createHeartbeatLinkedAdmissionGate} from './brokerToolHttpPorts.js';
import {
  argsDigestOfCanonicalPayload,
  createNoProgressGuard,
} from './noProgressGuard.js';
import {
  hydrateNudgeBudgetScopeFromKernel,
  resetNudgeBudgetScopeRegistryForTests,
} from './nudgeBudgetScope.js';

const argsA = argsDigestOfCanonicalPayload('{"path":"fail.ts"}');

function failedObs(callId: string) {
  return {
    callId,
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'FAILED' as const,
    signals: [{kind: 'effect_terminal' as const, value: 'FAILED'}],
  };
}

afterEach(() => {
  resetNudgeBudgetScopeRegistryForTests();
});

describe('AB06 Kernel hydrate Nudge 预算', () => {
  test('hydrate consumed=1 后新 guard 不得再 Nudge；≠DONE', async () => {
    const goalId = 'goal-ab06-kernel-1';
    let durable = 0;
    await hydrateNudgeBudgetScopeFromKernel({
      goalId,
      max: 1,
      fetchConsumed: async () => durable,
      persistConsume: async (n) => {
        durable = n;
      },
    });

    const gate1 = createHeartbeatLinkedAdmissionGate();
    const first = createDefaultActivationGuards(gate1, {
      goalId,
      maxNudgeBudget: 1,
    }).progressGuard;
    expect(first.observe(failedObs('a0')).action).toBe('continue');
    expect(first.observe(failedObs('a1')).action).toBe('nudge');
    expect(first.metrics().nudgeBudgetConsumed).toBe(1);

    // 模拟进程重启：清空 registry，再从 durable hydrate
    resetNudgeBudgetScopeRegistryForTests();
    await hydrateNudgeBudgetScopeFromKernel({
      goalId,
      max: 1,
      fetchConsumed: async () => durable,
      persistConsume: async (n) => {
        durable = n;
      },
    });
    const gate2 = createHeartbeatLinkedAdmissionGate();
    const second = createDefaultActivationGuards(gate2, {
      goalId,
      maxNudgeBudget: 1,
    }).progressGuard;
    expect(second.metrics().nudgeBudgetConsumed).toBe(1);
    const verdicts: string[] = [];
    for (let i = 0; i < 4; i += 1) {
      const v = second.observe(failedObs(`b${i}`));
      verdicts.push(v.action);
      expect(v.marksGoalDone).toBe(false);
    }
    expect(verdicts.filter((a) => a === 'nudge')).toHaveLength(0);
    expect(verdicts).toContain('force_stop');
  });

  test('createNoProgressGuard 显式 scope 仍 ≠DONE', () => {
    const scope = {
      consumed: 1,
      max: 1,
      tryConsume: () => false,
      isExhausted: () => true,
    };
    const guard = createNoProgressGuard({
      maxNudgeBudget: 1,
      nudgeBudgetScope: scope,
    });
    guard.observe(failedObs('z0'));
    const v = guard.observe(failedObs('z1'));
    expect(v.action).toBe('force_stop');
    expect(v.marksGoalDone).toBe(false);
  });
});
