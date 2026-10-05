/**
 * AB06：跨 activation 持久 Nudge 预算。
 * 同 Goal 失租重领后新建 guard 仍扣同一账，禁止「每次 activation 重置 → 无限提醒」。
 */
import {afterEach, describe, expect, test} from 'vitest';
import {createDefaultActivationGuards} from './activationGuards.js';
import {createHeartbeatLinkedAdmissionGate} from './brokerToolHttpPorts.js';
import {
  argsDigestOfCanonicalPayload,
  createNoProgressGuard,
} from './noProgressGuard.js';
import {
  getOrCreateNudgeBudgetScope,
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

describe('AB06 跨 activation Nudge 预算', () => {
  test('同 goal 两代 guard：第一代耗尽预算后，第二代不得再 Nudge；≠DONE', () => {
    const goalId = 'goal-ab06-cross-1';
    const gate1 = createHeartbeatLinkedAdmissionGate();
    const first = createDefaultActivationGuards(gate1, {
      goalId,
      maxNudgeBudget: 1,
    }).progressGuard;

    // 同参 FAILED×2 → 一次 Nudge（默认 repeatSuccessBeforeNudge=2）
    expect(first.observe(failedObs('a0')).action).toBe('continue');
    const nudge = first.observe(failedObs('a1'));
    expect(nudge.action).toBe('nudge');
    expect(nudge.marksGoalDone).toBe(false);
    expect(first.metrics().nudgeBudgetConsumed).toBe(1);

    // 失租重领：新 gate + 新 guard，同一 goal scope
    const gate2 = createHeartbeatLinkedAdmissionGate();
    const second = createDefaultActivationGuards(gate2, {
      goalId,
      maxNudgeBudget: 1,
    }).progressGuard;
    expect(second.metrics().nudgeBudgetConsumed).toBe(1);
    expect(second.metrics().nudgeBudgetMax).toBe(1);

    // 再次同参风暴：不得再发 Nudge，应 ForceStop；≠DONE
    const verdicts: string[] = [];
    for (let i = 0; i < 5; i += 1) {
      const v = second.observe(failedObs(`b${i}`));
      verdicts.push(v.action);
      expect(v.marksGoalDone).toBe(false);
    }
    expect(verdicts.filter((a) => a === 'nudge')).toHaveLength(0);
    expect(verdicts).toContain('force_stop');
    expect(second.metrics().nudgeBudgetConsumed).toBe(1);
  });

  test('无 goalId 时各 activation 独立预算（旧行为保留）', () => {
    const gate = createHeartbeatLinkedAdmissionGate();
    const a = createDefaultActivationGuards(gate).progressGuard;
    const b = createDefaultActivationGuards(gate).progressGuard;
    a.observe(failedObs('x0'));
    expect(a.observe(failedObs('x1')).action).toBe('nudge');
    b.observe(failedObs('y0'));
    expect(b.observe(failedObs('y1')).action).toBe('nudge');
    expect(a.metrics().nudgeBudgetConsumed).toBe(1);
    expect(b.metrics().nudgeBudgetConsumed).toBe(1);
  });

  test('显式 scope 注入：两 guard 共享 tryConsume', () => {
    const scope = getOrCreateNudgeBudgetScope('manual-scope', 2);
    const g1 = createNoProgressGuard({
      nudgeBudgetScope: scope,
      repeatSuccessBeforeNudge: 2,
      forceStopAfterNudge: false,
    });
    const g2 = createNoProgressGuard({
      nudgeBudgetScope: scope,
      repeatSuccessBeforeNudge: 2,
      forceStopAfterNudge: false,
    });
    g1.observe(failedObs('m0'));
    expect(g1.observe(failedObs('m1')).action).toBe('nudge');
    expect(scope.consumed).toBe(1);
    g2.observe(failedObs('m2'));
    expect(g2.observe(failedObs('m3')).action).toBe('nudge');
    expect(scope.consumed).toBe(2);
    g2.observe(failedObs('m4'));
    expect(g2.observe(failedObs('m5')).action).toBe('force_stop');
    expect(g2.lastVerdict().marksGoalDone).toBe(false);
  });
});
