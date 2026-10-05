import type {GoalResource} from '@ring/api-client';

export type GoalStatusFilter = 'ALL' | 'ACTIVE' | 'ATTENTION' | 'FINISHED' | GoalResource['status'];

const STATUS_LABELS: Record<GoalResource['status'], string> = {
  DRAFT: '尚未启动',
  PLANNING: '正在制定计划',
  RUNNING: '执行中',
  VERIFYING: '正在验收',
  PAUSING: '正在安全暂停',
  PAUSED: '已暂停',
  CANCELLING: '正在停止',
  CANCELLED: '已停止',
  BLOCKED: '等待处理',
  FAILED: '执行失败',
  DONE: '已交付',
};

export function goalStatusLabel(status: GoalResource['status']): string {
  return STATUS_LABELS[status];
}

export function filterExecutionRecords(
  rows: GoalResource[],
  search: string,
  status: GoalStatusFilter,
): GoalResource[] {
  const needle = search.trim().toLocaleLowerCase();
  return rows.filter((row) => {
    if (status === 'ACTIVE' && !['PLANNING', 'RUNNING', 'VERIFYING', 'PAUSING', 'CANCELLING'].includes(row.status)) return false;
    if (status === 'ATTENTION' && !['BLOCKED', 'FAILED'].includes(row.status)) return false;
    if (status === 'FINISHED' && !['DONE', 'CANCELLED'].includes(row.status)) return false;
    if (!['ALL', 'ACTIVE', 'ATTENTION', 'FINISHED'].includes(status) && row.status !== status) return false;
    if (!needle) return true;
    return `${row.contract.objective} ${row.id}`.toLocaleLowerCase().includes(needle);
  });
}

export function paginateExecutionRecords<T>(rows: T[], page: number, pageSize: number): T[] {
  const safePage = Math.max(1, Math.floor(page));
  const safeSize = Math.max(1, Math.floor(pageSize));
  return rows.slice((safePage - 1) * safeSize, safePage * safeSize);
}

export function countExecutionStatuses(rows: GoalResource[]) {
  return {
    running: rows.filter((row) => row.status === 'RUNNING').length,
    attention: rows.filter((row) => ['BLOCKED', 'FAILED'].includes(row.status)).length,
    verifying: rows.filter((row) => row.status === 'VERIFYING').length,
    delivered: rows.filter((row) => row.status === 'DONE').length,
  };
}
