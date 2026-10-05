import {describe, expect, it} from 'vitest';
import {
  approvalDecisionSuccessCaption,
  approvalRowsFromList,
  approvalRevokeSuccessCaption,
  approvalsCaption,
  canApproverAct,
} from './approvalsView.js';

describe('approvalsView', () => {
  it('投影列表并统计 PENDING', () => {
    const rows = approvalRowsFromList([
      {
        id: 'a1',
        status: 'PENDING',
        subject: {type: 'EFFECT', id: 'e1'},
        goal_id: 'g1',
        state_revision: 1,
        payload_digest: 'sha256:' + 'ab'.repeat(32),
        expires_at: '2026-09-13T12:00:00Z',
        decision_reason: null,
      },
      {
        id: 'a2',
        status: 'APPROVED',
        subject: {type: 'EFFECT', id: 'e2'},
        goal_id: null,
        state_revision: 2,
        payload_digest: 'sha256:' + 'cd'.repeat(32),
      },
    ]);
    expect(rows).toHaveLength(2);
    expect(rows[0]?.status).toBe('PENDING');
    expect(rows[0]?.subjectType).toBe('EFFECT');
    const caption = approvalsCaption({
      rowCount: rows.length,
      pendingCount: rows.filter((r) => r.status === 'PENDING').length,
      filterStatus: 'PENDING',
    });
    expect(caption).toContain('PENDING 1');
    expect(caption).toContain('≠ Goal DONE');
  });

  it('approver/admin 才可裁决', () => {
    expect(canApproverAct(['viewer'])).toBe(false);
    expect(canApproverAct(['approver'])).toBe(true);
    expect(canApproverAct(['admin'])).toBe(true);
  });

  it('成功文案不宣称 DONE', () => {
    expect(approvalDecisionSuccessCaption('APPROVE')).toContain('≠ Goal DONE');
    expect(approvalDecisionSuccessCaption('DENY')).toContain('≠ Goal DONE');
    expect(approvalRevokeSuccessCaption()).toContain('≠ Goal DONE');
  });
});
