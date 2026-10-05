import {describe, expect, it} from 'vitest';
import type {TaskResource} from '@ring/api-client';
import {buildTaskColumns} from './taskGraphView.js';

function task(id: string, dependsOn: string[] = []): TaskResource {
  return {id, status: 'PENDING', contract: {objective: id, depends_on: dependsOn}} as TaskResource;
}

describe('buildTaskColumns', () => {
  it('按真实依赖关系分层，而不是按数组顺序伪造流程', () => {
    const columns = buildTaskColumns([task('c', ['a', 'b']), task('b', ['a']), task('a')]);
    expect(columns.map((column) => column.map((item) => item.id))).toEqual([['a'], ['b'], ['c']]);
  });

  it('缺失依赖不会让任务消失', () => {
    expect(buildTaskColumns([task('a', ['unknown'])]).flat().map((item) => item.id)).toEqual(['a']);
  });
});
