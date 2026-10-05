/**
 * Goal Task 列表投影。
 * Task.status=DONE ≠ Goal DONE。
 */
export type TaskRow = {
  id: string;
  status: string;
  objective: string;
  planRevision: number;
  contractRevision: number;
  stateRevision: number;
  executionRound: number;
  blockReason: string | null;
  updatedAt: string | null;
};

export type TaskListItem = {
  id: string;
  status: string;
  contract?: {objective?: string} | null;
  plan_revision?: number;
  contract_revision?: number;
  state_revision?: number;
  execution_round?: number;
  block_reason?: string | null;
  updated_at?: string | null;
};

export function taskRowsFromList(items: TaskListItem[]): TaskRow[] {
  return items.map((item) => ({
    id: item.id,
    status: String(item.status ?? ''),
    objective: String(item.contract?.objective ?? '').trim() || '(无 objective)',
    planRevision: Number(item.plan_revision ?? 0),
    contractRevision: Number(item.contract_revision ?? 0),
    stateRevision: Number(item.state_revision ?? 0),
    executionRound: Number(item.execution_round ?? 0),
    blockReason:
      item.block_reason == null || item.block_reason === ''
        ? null
        : String(item.block_reason),
    updatedAt: item.updated_at ?? null,
  }));
}

export function countTaskDone(rows: TaskRow[]): number {
  return rows.filter((r) => r.status === 'DONE').length;
}

export function tasksCaption(input: {
  rowCount: number;
  doneCount: number;
  statusFilter: string | null;
}): string {
  const status = input.statusFilter
    ? `筛选 status=${input.statusFilter}；`
    : '全部 status；';
  return `${status}共 ${input.rowCount} 条，其中 Task DONE ${input.doneCount}。Task DONE ≠ Goal DONE（Goal 终局只经 Kernel + VerificationProfile + 屏障）。`;
}

export function taskStatusCaption(status: string): string {
  if (status === 'DONE') {
    return 'DONE：该 Task 合同已裁决完成；≠ Goal DONE。';
  }
  if (status === 'BLOCKED') {
    return 'BLOCKED：阻塞中；≠ 终局。';
  }
  if (status === 'VERIFYING') {
    return 'VERIFYING：验收进行中；≠ Goal DONE。';
  }
  if (status === 'FAILED') {
    return 'FAILED：任务失败；≠ Goal 终局裁决。';
  }
  return `${status}：只读观察。`;
}
