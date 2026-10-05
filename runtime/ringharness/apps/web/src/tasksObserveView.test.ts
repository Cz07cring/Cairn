import {describe, expect, it} from 'vitest';
import {
  countTaskDone,
  taskRowsFromList,
  taskStatusCaption,
  tasksCaption,
} from './tasksObserveView.js';

describe('tasksObserveView', () => {
  it('投影列表并区分 Task DONE 与 Goal DONE 文案', () => {
    const rows = taskRowsFromList([
      {
        id: 't1',
        status: 'DONE',
        contract: {objective: '修泄漏'},
        plan_revision: 1,
        contract_revision: 1,
        state_revision: 3,
        execution_round: 1,
      },
      {
        id: 't2',
        status: 'RUNNING',
        contract: {objective: '补测'},
        plan_revision: 1,
        contract_revision: 1,
        state_revision: 1,
        execution_round: 0,
      },
    ]);
    expect(rows).toHaveLength(2);
    expect(rows[0]?.objective).toBe('修泄漏');
    expect(countTaskDone(rows)).toBe(1);
    const caption = tasksCaption({
      rowCount: rows.length,
      doneCount: 1,
      statusFilter: 'DONE',
    });
    expect(caption).toContain('筛选 status=DONE');
    expect(caption).toContain('Task DONE ≠ Goal DONE');
  });

  it('DONE 状态文案禁止冒充 Goal DONE', () => {
    expect(taskStatusCaption('DONE')).toContain('≠ Goal DONE');
    expect(taskStatusCaption('VERIFYING')).toContain('≠ Goal DONE');
  });
});
