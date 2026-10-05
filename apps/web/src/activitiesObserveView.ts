/**
 * Goal 活动时间线投影；含无 Task 的 PLAN。
 * Activity SUCCEEDED / RUNNING ≠ Goal/Task DONE。
 */
export type ActivityRow = {
  id: string;
  kind: string;
  status: string;
  taskId: string | null;
  retryCount: number;
  stateRevision: number;
  createdAt: string | null;
  updatedAt: string | null;
};

export type ActivityListItem = {
  id: string;
  kind: string;
  status: string;
  task_id?: string | null;
  retry_count?: number;
  state_revision?: number;
  created_at?: string | null;
  updated_at?: string | null;
};

export function activityRowsFromList(items: ActivityListItem[]): ActivityRow[] {
  return items.map((item) => ({
    id: item.id,
    kind: String(item.kind ?? ''),
    status: String(item.status ?? ''),
    taskId: item.task_id == null || item.task_id === '' ? null : String(item.task_id),
    retryCount: Number(item.retry_count ?? 0),
    stateRevision: Number(item.state_revision ?? 0),
    createdAt: item.created_at ?? null,
    updatedAt: item.updated_at ?? null,
  }));
}

/** 无 Task 绑定的活动（典型：首次 PLAN）计数。 */
export function countOrphanPlanActivities(rows: ActivityRow[]): number {
  return rows.filter((r) => r.taskId == null).length;
}

export function activitiesCaption(input: {
  rowCount: number;
  orphanCount: number;
  kindFilter: string | null;
  statusFilter: string | null;
}): string {
  const kind = input.kindFilter ? `kind=${input.kindFilter}；` : '全部 kind；';
  const status = input.statusFilter
    ? `status=${input.statusFilter}；`
    : '全部 status；';
  return `${kind}${status}共 ${input.rowCount} 条，无 Task 绑定 ${input.orphanCount}（含首次 PLAN）。Activity SUCCEEDED ≠ Goal/Task DONE。`;
}

export function activityStatusCaption(status: string): string {
  if (status === 'SUCCEEDED') {
    return 'SUCCEEDED：活动执行单元完成；≠ Goal/Task DONE。';
  }
  if (status === 'RUNNING') {
    return 'RUNNING：进行中；≠ DONE。';
  }
  if (status === 'FAILED') {
    return 'FAILED：活动失败；≠ 终局裁决。';
  }
  return `${status}：只读观察。`;
}

export function taskBindingCaption(taskId: string | null): string {
  if (taskId == null) {
    return '无 Task（如首次 PLAN）';
  }
  return taskId;
}
