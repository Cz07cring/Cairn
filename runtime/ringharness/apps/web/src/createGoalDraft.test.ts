import {describe, expect, it} from 'vitest';
import {
  buildGoalCreateBody,
  canOperatorCreate,
  createGoalDraftCaption,
  validateCreateGoalDraftForm,
  type CreateGoalDraftForm,
} from './createGoalDraft.js';

const valid: CreateGoalDraftForm = {
  projectId: '11111111-1111-1111-1111-111111111111',
  objective: '修复订单幂等',
  criterionId: 'C1',
  criterionDescription: 'GLOBAL 验收通过',
  verificationProfileId: '22222222-2222-2222-2222-222222222222',
  policyId: '33333333-3333-3333-3333-333333333333',
  modelProfileId: '44444444-4444-4444-4444-444444444444',
  skillSetId: '55555555-5555-5555-5555-555555555555',
  baseCommit: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  constraint: '仅修改隔离工作区',
};

describe('createGoalDraft', () => {
  it('校验失败关闭', () => {
    const bad = validateCreateGoalDraftForm({
      ...valid,
      objective: '',
      baseCommit: 'short',
    });
    expect(bad.ok).toBe(false);
    expect(bad.errors.some((e) => e.includes('objective'))).toBe(true);
    expect(bad.errors.some((e) => e.includes('base_commit'))).toBe(true);
  });

  it('装配 GoalCreate 且不宣称 DONE', () => {
    const body = buildGoalCreateBody(valid);
    expect(body.objective).toBe('修复订单幂等');
    expect(body.success_criteria).toHaveLength(1);
    expect(body.success_criteria[0]?.required).toBe(true);
    expect(createGoalDraftCaption('DRAFT')).toContain('≠ DONE');
    expect(createGoalDraftCaption(null)).toContain('DRAFT');
  });

  it('仅 operator 可创建', () => {
    expect(canOperatorCreate(['viewer'])).toBe(false);
    expect(canOperatorCreate(['operator', 'viewer'])).toBe(true);
  });
});
