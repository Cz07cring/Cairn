import {describe, expect, it} from 'vitest';
import {
  activitiesCaption,
  activityRowsFromList,
  activityStatusCaption,
  countOrphanPlanActivities,
  taskBindingCaption,
} from './activitiesObserveView.js';

describe('activitiesObserveView', () => {
  it('投影列表并统计无 Task 的 PLAN', () => {
    const rows = activityRowsFromList([
      {
        id: 'a1',
        kind: 'PLAN',
        status: 'SUCCEEDED',
        task_id: null,
        retry_count: 0,
        state_revision: 1,
        created_at: '2026-09-13T00:00:00Z',
      },
      {
        id: 'a2',
        kind: 'EXECUTE',
        status: 'RUNNING',
        task_id: 't1',
        retry_count: 1,
        state_revision: 2,
      },
    ]);
    expect(rows).toHaveLength(2);
    expect(rows[0]?.taskId).toBeNull();
    expect(countOrphanPlanActivities(rows)).toBe(1);
    const caption = activitiesCaption({
      rowCount: rows.length,
      orphanCount: 1,
      kindFilter: null,
      statusFilter: null,
    });
    expect(caption).toContain('无 Task 绑定 1');
    expect(caption).toContain('≠ Goal/Task DONE');
  });

  it('SUCCEEDED 文案禁止冒充 DONE', () => {
    expect(activityStatusCaption('SUCCEEDED')).toContain('≠ Goal/Task DONE');
    expect(activityStatusCaption('RUNNING')).toContain('≠ DONE');
    expect(taskBindingCaption(null)).toContain('无 Task');
    expect(taskBindingCaption('t-1')).toBe('t-1');
  });
});
