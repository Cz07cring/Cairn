import type {TaskResource} from '@ring/api-client';

export function taskProgress(tasks: TaskResource[]): {done: number; active: number; attention: number; total: number} {
  return {
    done: tasks.filter((task) => task.status === 'DONE').length,
    active: tasks.filter((task) => ['READY', 'RUNNING', 'VERIFYING', 'RETRYING', 'RECOVERING'].includes(task.status)).length,
    attention: tasks.filter((task) => ['BLOCKED', 'STALE', 'FAILED'].includes(task.status)).length,
    total: tasks.length,
  };
}

export function goalStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    DRAFT: '草稿', PLANNING: '正在规划', RUNNING: '状态为运行中', VERIFYING: '正在验收',
    BLOCKED: '等待处理', PAUSING: '正在暂停', PAUSED: '已暂停', CANCELLING: '正在停止',
    CANCELLED: '已停止', FAILED: '执行失败', DONE: '已通过最终验收',
  };
  return labels[status] ?? status;
}
