/**
 * 第三百五十二批：Kernel hydrate 软禁键后进程重启仍挡同参；≠ DONE。
 */
import {afterEach, describe, expect, test} from 'vitest';
import {createDefaultActivationGuards} from './activationGuards.js';
import {
  hydrateBanCallKeyScopeFromKernel,
  resetBanCallKeyScopeRegistryForTests,
} from './banCallKeyScope.js';
import {createHeartbeatLinkedAdmissionGate} from './brokerToolHttpPorts.js';
import {argsDigestOfCanonicalPayload} from './noProgressGuard.js';
import {resetNudgeBudgetScopeRegistryForTests} from './nudgeBudgetScope.js';

const argsA = argsDigestOfCanonicalPayload('{"path":"fail.ts"}');
const banKey = `read_file|${argsA}`;

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
  resetBanCallKeyScopeRegistryForTests();
  resetNudgeBudgetScopeRegistryForTests();
});

describe('AB06 Kernel hydrate 软禁同参', () => {
  test('hydrate 已禁键后新 guard beforeAdmit 仍挡；≠DONE', async () => {
    const goalId = 'goal-ab06-ban-kernel-1';
    const durable = new Set<string>();

    await hydrateBanCallKeyScopeFromKernel({
      goalId,
      fetchKeys: async () => [...durable],
      persistAdd: async (k) => {
        durable.add(k);
      },
    });

    const gate1 = createHeartbeatLinkedAdmissionGate();
    const first = createDefaultActivationGuards(gate1, {
      goalId,
      maxNudgeBudget: 1,
    }).progressGuard;

    expect(first.observe(failedObs('a0')).action).toBe('continue');
    expect(first.observe(failedObs('a1')).action).toBe('nudge');
    const soft = first.observe(failedObs('a2'));
    expect(soft.action).toBe('force_stop');
    if (soft.action === 'force_stop') {
      expect(soft.closeToolAdmission).toBe(false);
      expect(soft.marksGoalDone).toBe(false);
    }
    expect(durable.has(banKey)).toBe(true);

    // 模拟进程重启
    resetBanCallKeyScopeRegistryForTests();
    resetNudgeBudgetScopeRegistryForTests();
    await hydrateBanCallKeyScopeFromKernel({
      goalId,
      fetchKeys: async () => [...durable],
      persistAdd: async (k) => {
        durable.add(k);
      },
    });

    const gate2 = createHeartbeatLinkedAdmissionGate();
    const second = createDefaultActivationGuards(gate2, {
      goalId,
      maxNudgeBudget: 1,
    }).progressGuard;
    expect(second.metrics().bannedCallKeyCount).toBe(1);

    const admit = second.beforeAdmit({
      tool: 'read_file',
      argsDigest: argsA,
    });
    expect(admit.action).toBe('force_stop');
    if (admit.action === 'force_stop') {
      expect(admit.closeToolAdmission).toBe(false);
      expect(admit.reason).toMatch(/^repeat_banned:/);
      expect(admit.marksGoalDone).toBe(false);
    }
    expect(gate2.allowed()).toBe(true);
  });
});
