/**
 * AB06：跨 activation 软禁同参键。
 * 同 Goal 失租重领后新建 guard 仍禁止已软 ForceStop 的 tool|argsDigest；≠ DONE。
 */
import {afterEach, describe, expect, test} from 'vitest';
import {createDefaultActivationGuards} from './activationGuards.js';
import {
  getOrCreateBanCallKeyScope,
  resetBanCallKeyScopeRegistryForTests,
} from './banCallKeyScope.js';
import {createHeartbeatLinkedAdmissionGate} from './brokerToolHttpPorts.js';
import {
  argsDigestOfCanonicalPayload,
  createNoProgressGuard,
} from './noProgressGuard.js';
import {resetNudgeBudgetScopeRegistryForTests} from './nudgeBudgetScope.js';

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
  resetBanCallKeyScopeRegistryForTests();
  resetNudgeBudgetScopeRegistryForTests();
});

describe('AB06 跨 activation 软禁同参', () => {
  test('同 goal 两代 guard：第一代软禁后，第二代 beforeAdmit 仍挡同参；≠DONE', () => {
    const goalId = 'goal-ab06-ban-1';
    const gate1 = createHeartbeatLinkedAdmissionGate();
    const first = createDefaultActivationGuards(gate1, {
      goalId,
      maxNudgeBudget: 1,
    }).progressGuard;

    expect(first.observe(failedObs('a0')).action).toBe('continue');
    expect(first.observe(failedObs('a1')).action).toBe('nudge');
    // 再同参 → 软 ForceStop（禁同参，不关全闸）
    const soft = first.observe(failedObs('a2'));
    expect(soft.action).toBe('force_stop');
    if (soft.action === 'force_stop') {
      expect(soft.closeToolAdmission).toBe(false);
      expect(soft.marksGoalDone).toBe(false);
    }
    expect(first.metrics().bannedCallKeyCount).toBe(1);

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

  test('无 goalId 时各 activation 独立禁令（旧行为）', () => {
    const gate = createHeartbeatLinkedAdmissionGate();
    const a = createDefaultActivationGuards(gate).progressGuard;
    const b = createDefaultActivationGuards(gate).progressGuard;
    a.observe(failedObs('x0'));
    a.observe(failedObs('x1'));
    a.observe(failedObs('x2'));
    expect(a.metrics().bannedCallKeyCount).toBe(1);
    expect(b.metrics().bannedCallKeyCount).toBe(0);
    expect(
      b.beforeAdmit({tool: 'read_file', argsDigest: argsA}).action,
    ).toBe('continue');
  });

  test('手工 banCallKeyScope 跨 guard 共享', () => {
    const scope = getOrCreateBanCallKeyScope('manual-ban');
    const g1 = createNoProgressGuard({banCallKeyScope: scope});
    g1.observe(failedObs('m0'));
    g1.observe(failedObs('m1'));
    g1.observe(failedObs('m2'));
    const g2 = createNoProgressGuard({banCallKeyScope: scope});
    expect(g2.metrics().bannedCallKeyCount).toBe(1);
    expect(
      g2.beforeAdmit({tool: 'read_file', argsDigest: argsA}).action,
    ).toBe('force_stop');
  });
});
