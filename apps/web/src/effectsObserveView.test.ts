import {describe, expect, it} from 'vitest';
import {
  canApproverReconcile,
  effectRowsFromList,
  effectStatusCaption,
  effectsCaption,
  reconcileSuccessCaption,
} from './effectsObserveView.js';

describe('effectsObserveView', () => {
  it('投影列表并统计 UNKNOWN', () => {
    const rows = effectRowsFromList([
      {
        id: 'e1',
        status: 'UNKNOWN',
        tool_ref: 'read_file',
        scope: 'ENGINEERING',
        replay_class: 'READ_ONLY',
        state_revision: 2,
        activity_id: 'a1',
        logical_step_id: 's1',
        evidence_ids: [],
      },
      {
        id: 'e2',
        status: 'SUCCEEDED',
        tool_ref: 'run_tests',
        scope: 'VERIFICATION',
        replay_class: 'IDEMPOTENT',
        state_revision: 1,
        activity_id: 'a2',
        logical_step_id: 's2',
      },
    ]);
    expect(rows).toHaveLength(2);
    expect(rows[0]?.status).toBe('UNKNOWN');
    const caption = effectsCaption({
      rowCount: rows.length,
      unknownCount: 1,
      filterStatus: 'UNKNOWN',
    });
    expect(caption).toContain('UNKNOWN 1');
    expect(caption).toContain('禁止浏览器生成新 effect_id');
    expect(caption).toContain('≠ Goal DONE');
  });

  it('UNKNOWN 文案禁止重试捷径', () => {
    expect(effectStatusCaption('UNKNOWN')).toContain('不可「再执行一次」');
    expect(effectStatusCaption('SUCCEEDED')).toContain('≠ Goal/Task DONE');
  });

  it('approver/admin 才可对账', () => {
    expect(canApproverReconcile(['viewer'])).toBe(false);
    expect(canApproverReconcile(['approver'])).toBe(true);
    expect(reconcileSuccessCaption()).toContain('≠ Goal DONE');
  });
});
