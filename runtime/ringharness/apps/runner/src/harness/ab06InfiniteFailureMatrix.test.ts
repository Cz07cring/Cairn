/**
 * AB06 命名缝：相同失败调用的「无限失败矩阵」。
 * 证明 Nudge 有上界、耗尽后 ForceStop、永不 marksGoalDone。
 */
import {describe, expect, test} from 'vitest';
import {createHeartbeatLinkedAdmissionGate} from './brokerToolHttpPorts.js';
import {
  argsDigestOfCanonicalPayload,
  createNoProgressGuard,
} from './noProgressGuard.js';

const argsA = argsDigestOfCanonicalPayload('{"path":"fail.ts"}');
const argsB = argsDigestOfCanonicalPayload('{"path":"other.ts"}');

function failedObs(callId: string, argsDigest: string = argsA) {
  return {
    callId,
    tool: 'read_file',
    argsDigest,
    outcome: 'FAILED' as const,
    signals: [{kind: 'effect_terminal' as const, value: 'FAILED'}],
  };
}

describe('AB06 无限失败矩阵', () => {
  test('同参 FAILED×50：Nudge≤1 后软 ForceStop；不关全闸；≠DONE', () => {
    const gate = createHeartbeatLinkedAdmissionGate();
    const guard = createNoProgressGuard({
      gate,
      repeatSuccessBeforeNudge: 2,
      forceStopAfterNudge: true,
      maxNudgeBudget: 1,
    });

    const verdicts: string[] = [];
    for (let i = 0; i < 50; i += 1) {
      const v = guard.observe(failedObs(`f${i}`));
      verdicts.push(v.action);
      expect(v.marksGoalDone).toBe(false);
    }

    expect(verdicts.filter((a) => a === 'nudge')).toHaveLength(1);
    expect(verdicts.filter((a) => a === 'force_stop').length).toBeGreaterThanOrEqual(
      48,
    );
    expect(guard.metrics().nudgeCount).toBe(1);
    expect(guard.metrics().nudgeBudgetConsumed).toBe(1);
    expect(guard.metrics().nudgeBudgetConsumed).toBeLessThanOrEqual(
      guard.metrics().nudgeBudgetMax,
    );
    // 软停：全闸仍开，换工具可写
    expect(gate.allowed()).toBe(true);
    expect(guard.metrics().forceStopped).toBe(false);
    expect(guard.beforeAdmit({tool: 'read_file', argsDigest: argsA})).toMatchObject({
      action: 'force_stop',
      closeToolAdmission: false,
      marksGoalDone: false,
    });
    expect(guard.beforeAdmit({tool: 'write_file', argsDigest: argsB}).action).toBe(
      'continue',
    );
  });

  test('forceStopAfterNudge=false × maxNudgeBudget=3：FAILED 风暴后硬停，Nudge 不超预算', () => {
    const gate = createHeartbeatLinkedAdmissionGate();
    const guard = createNoProgressGuard({
      gate,
      repeatSuccessBeforeNudge: 2,
      forceStopAfterNudge: false,
      maxNudgeBudget: 3,
    });

    let nudgeEvents = 0;
    let hardStopAt = -1;
    for (let i = 0; i < 40; i += 1) {
      const v = guard.observe(failedObs(`b${i}`));
      expect(v.marksGoalDone).toBe(false);
      if (v.action === 'nudge') {
        nudgeEvents += 1;
      }
      if (v.action === 'force_stop' && hardStopAt < 0) {
        hardStopAt = i;
        expect(String((v as {reason?: string}).reason || '')).toMatch(
          /nudge_budget_exhausted/,
        );
      }
    }

    expect(nudgeEvents).toBe(3);
    expect(nudgeEvents).toBeLessThanOrEqual(3);
    expect(guard.metrics().nudgeBudgetConsumed).toBe(3);
    expect(hardStopAt).toBeGreaterThanOrEqual(0);
    expect(gate.allowed()).toBe(false);
    expect(guard.metrics().forceStopped).toBe(true);
    // 硬停后继续观察不得再发 Nudge
    const after = guard.observe(failedObs('after'));
    expect(after.action).toBe('force_stop');
    expect(guard.metrics().nudgeCount).toBe(3);
  });

  test('交替两套同参 FAILED：预算耗尽后硬停，不得无限 Nudge', () => {
    const gate = createHeartbeatLinkedAdmissionGate();
    const guard = createNoProgressGuard({
      gate,
      repeatSuccessBeforeNudge: 2,
      forceStopAfterNudge: false,
      maxNudgeBudget: 2,
    });

    const digests = [argsA, argsB];
    let nudges = 0;
    for (let i = 0; i < 30; i += 1) {
      const dig = digests[i % 2]!;
      // 每套连续两次以触发阈值
      const v1 = guard.observe(failedObs(`x${i}a`, dig));
      const v2 = guard.observe(failedObs(`x${i}b`, dig));
      for (const v of [v1, v2]) {
        expect(v.marksGoalDone).toBe(false);
        if (v.action === 'nudge') {
          nudges += 1;
        }
      }
      if (guard.metrics().forceStopped) {
        break;
      }
    }

    expect(nudges).toBeLessThanOrEqual(2);
    expect(guard.metrics().nudgeBudgetConsumed).toBeLessThanOrEqual(2);
    expect(gate.allowed()).toBe(false);
    expect(guard.metrics().forceStopped).toBe(true);
  });
});
