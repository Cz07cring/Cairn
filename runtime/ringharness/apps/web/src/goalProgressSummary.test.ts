import {describe, expect, it} from 'vitest';
import {goalStatusLabel, taskProgress} from './goalProgressSummary.js';

describe('currentGoalSummary', () => {
  it('分别统计完成、推进中和需处理任务', () => {
    const tasks = ['DONE', 'RUNNING', 'VERIFYING', 'BLOCKED', 'FAILED'].map((status, index) => ({id: String(index), status})) as never;
    expect(taskProgress(tasks)).toEqual({done: 1, active: 2, attention: 2, total: 5});
  });

  it('Goal 状态使用普通用户语言且不误判完成', () => {
    expect(goalStatusLabel('RUNNING')).toBe('状态为运行中');
    expect(goalStatusLabel('VERIFYING')).toBe('正在验收');
    expect(goalStatusLabel('DONE')).toContain('最终验收');
  });
});
