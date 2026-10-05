import {describe, expect, it} from 'vitest';
import {
  isRunPage,
  parseWorkbenchHash,
  readWorkbenchParam,
  withWorkbenchParam,
  workbenchHash,
} from './workbenchNavigation.js';

describe('workbenchNavigation', () => {
  it('未知或空 hash 回到总览', () => {
    expect(parseWorkbenchHash('')).toBe('overview');
    expect(parseWorkbenchHash('#unknown')).toBe('overview');
  });

  it('保留旧面板深链，但映射到新的渐进披露页面', () => {
    expect(parseWorkbenchHash('#tasks')).toBe('run-overview');
    expect(parseWorkbenchHash('#commands')).toBe('run-logs');
    expect(parseWorkbenchHash('#finalization')).toBe('run-evidence');
    expect(parseWorkbenchHash('#quarantine-inbox')).toBe('attention');
  });

  it('生成稳定 hash 并识别运行详情页', () => {
    expect(parseWorkbenchHash('#runs')).toBe('runs');
    expect(workbenchHash('run-trace')).toBe('#run-trace');
    expect(isRunPage(parseWorkbenchHash('#run-trace'))).toBe(true);
    expect(isRunPage(parseWorkbenchHash('#goals'))).toBe(false);
    expect(parseWorkbenchHash('#run-live')).toBe('run-live');
  });

  it('任务选择写入 hash，刷新后可以恢复', () => {
    const hash = withWorkbenchParam('run-overview', 'task', 'task-1');
    expect(hash).toBe('#run-overview?task=task-1');
    expect(readWorkbenchParam(hash, 'task')).toBe('task-1');
    expect(withWorkbenchParam('run-overview', 'task')).toBe('#run-overview');
  });
});
