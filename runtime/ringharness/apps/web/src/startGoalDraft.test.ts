import {describe, expect, it} from 'vitest';
import {
  goalOptionLabel,
  startGoalEligibility,
  startGoalSuccessCaption,
  toStartGoalCandidate,
  type StartGoalCandidate,
} from './startGoalDraft.js';

const draft: StartGoalCandidate = {
  id: '11111111-1111-1111-1111-111111111111',
  status: 'DRAFT',
  stateRevision: 1,
  objective: '修复订单幂等',
  orchestrationBackend: 'TEMPORAL',
};

describe('startGoalDraft', () => {
  it('仅 DRAFT + operator 可 START', () => {
    expect(startGoalEligibility(draft, true).canStart).toBe(true);
    expect(startGoalEligibility(draft, false).canStart).toBe(false);
    expect(
      startGoalEligibility({...draft, status: 'PLANNING'}, true).canStart,
    ).toBe(false);
    expect(startGoalEligibility(null, true).canStart).toBe(false);
  });

  it('成功文案不宣称 DONE', () => {
    const text = startGoalSuccessCaption({
      commandStatus: 'SUCCEEDED',
      finalStatus: 'PLANNING',
    });
    expect(text).toContain('PLANNING');
    expect(text).toContain('≠ Goal DONE');
  });

  it('候选投影与选项标签', () => {
    const c = toStartGoalCandidate({
      id: draft.id,
      status: 'DRAFT',
      state_revision: 1,
      orchestration_backend: 'TEMPORAL',
      contract: {objective: '修复订单幂等'},
    });
    expect(c.stateRevision).toBe(1);
    expect(goalOptionLabel(c)).toContain('DRAFT');
    expect(goalOptionLabel(c)).toContain('修复订单幂等');
  });
});
