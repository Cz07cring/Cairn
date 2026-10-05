import {describe, expect, it} from 'vitest';
import {
  abandonmentCaption,
  abandonmentReleaseEligibility,
  observeGoalOptionLabel,
  preferredObserveGoalId,
  toObserveGoalOption,
  type ObserveGoalOption,
} from './observeGoalPicker.js';

const blocked: ObserveGoalOption = {
  id: '11111111-1111-1111-1111-111111111111',
  status: 'BLOCKED',
  stateRevision: 3,
  objective: '修复幂等',
  blockReason: 'ORCHESTRATION_ABANDONED:INTENT_EXPIRED',
};

describe('observeGoalPicker', () => {
  it('仅编排放弃 BLOCKED 可解除', () => {
    expect(
      abandonmentReleaseEligibility(blocked, true, 1).canRelease,
    ).toBe(true);
    expect(
      abandonmentReleaseEligibility(blocked, false, 1).canRelease,
    ).toBe(false);
    expect(
      abandonmentReleaseEligibility(
        {...blocked, status: 'RUNNING'},
        true,
        1,
      ).canRelease,
    ).toBe(false);
    expect(
      abandonmentReleaseEligibility(
        {...blocked, blockReason: 'FINALIZATION_FAIL'},
        true,
        1,
      ).canRelease,
    ).toBe(false);
  });

  it('文案不把放弃当成 DONE', () => {
    expect(
      abandonmentCaption({
        goalStatus: 'BLOCKED',
        count: 2,
        marksGoalDoneAny: false,
      }),
    ).toContain('≠ DONE');
    expect(
      abandonmentCaption({
        goalStatus: 'RUNNING',
        count: 0,
        marksGoalDoneAny: true,
      }),
    ).toContain('绝不等于 DONE');
  });

  it('选项投影', () => {
    const o = toObserveGoalOption({
      id: blocked.id,
      status: 'BLOCKED',
      state_revision: 3,
      block_reason: 'ORCHESTRATION_ABANDONED:INTENT_EXPIRED',
      contract: {objective: '修复幂等'},
    });
    expect(observeGoalOptionLabel(o)).toContain('BLOCKED');
    expect(o.stateRevision).toBe(3);
  });

  it('无有效历史选择时优先正在运行的目标', () => {
    const goals = [
      {...blocked, id: 'done', status: 'DONE'},
      {...blocked, id: 'running', status: 'RUNNING'},
      {...blocked, id: 'verifying', status: 'VERIFYING'},
    ];
    expect(preferredObserveGoalId(goals, '')).toBe('running');
    expect(preferredObserveGoalId(goals, 'verifying')).toBe('verifying');
  });
});
