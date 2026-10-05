import {describe, expect, it} from 'vitest';
import type {GoalResource} from '@ring/api-client';
import {countExecutionStatuses, filterExecutionRecords, goalStatusLabel, paginateExecutionRecords} from './executionRecordsView.js';

function goal(id: string, status: GoalResource['status'], objective: string): GoalResource {
  return {id, status, contract: {objective}} as GoalResource;
}

describe('executionRecordsView', () => {
  const rows = [goal('g-running', 'RUNNING', '修复订单'), goal('g-blocked', 'BLOCKED', '升级支付'), goal('g-done', 'DONE', '同步文档')];

  it('用普通用户语言描述状态', () => {
    expect(goalStatusLabel('BLOCKED')).toBe('等待处理');
    expect(goalStatusLabel('DONE')).toBe('已交付');
  });

  it('把执行中、需要处理和已结束按用户含义分组', () => {
    expect(filterExecutionRecords(rows, '', 'ACTIVE').map((row) => row.status)).toEqual(['RUNNING']);
    expect(filterExecutionRecords(rows, '', 'ATTENTION').map((row) => row.status)).toEqual(['BLOCKED']);
    expect(filterExecutionRecords(rows, '', 'FINISHED').map((row) => row.status)).toEqual(['DONE']);
  });

  it('分页时不会越过当前页范围', () => {
    expect(paginateExecutionRecords([1, 2, 3, 4, 5], 2, 2)).toEqual([3, 4]);
    expect(paginateExecutionRecords([1, 2], 0, 1)).toEqual([1]);
  });

  it('可按目标名称、编号和状态筛选', () => {
    expect(filterExecutionRecords(rows, '订单', 'ALL')).toHaveLength(1);
    expect(filterExecutionRecords(rows, 'g-blocked', 'BLOCKED')).toHaveLength(1);
    expect(filterExecutionRecords(rows, '', 'DONE')).toHaveLength(1);
  });

  it('状态统计不会把运行结束推导成最终交付', () => {
    expect(countExecutionStatuses(rows)).toEqual({running: 1, attention: 1, verifying: 0, delivered: 1});
  });
});
