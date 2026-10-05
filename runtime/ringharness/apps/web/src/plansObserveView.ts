/**
 * Goal Plan 列表投影。
 * Plan PUBLISHED ≠ Goal DONE。
 */
export type PlanRow = {
  id: string;
  status: string;
  planRevision: number | null;
  taskCount: number;
  reason: string;
  updatedAt: string | null;
};

export type PlanListItem = {
  id: string;
  status: string;
  plan_revision?: number | null;
  tasks?: unknown[];
  reason?: string;
  updated_at?: string | null;
};

export function planRowsFromList(items: PlanListItem[]): PlanRow[] {
  return items.map((item) => ({
    id: item.id,
    status: String(item.status ?? ''),
    planRevision:
      item.plan_revision == null ? null : Number(item.plan_revision),
    taskCount: Array.isArray(item.tasks) ? item.tasks.length : 0,
    reason: String(item.reason ?? '').trim(),
    updatedAt: item.updated_at ?? null,
  }));
}

export function countPublishedPlans(rows: PlanRow[]): number {
  return rows.filter((r) => r.status === 'PUBLISHED').length;
}

export function plansCaption(input: {
  rowCount: number;
  publishedCount: number;
}): string {
  return `共 ${input.rowCount} 条，其中 PUBLISHED ${input.publishedCount}。Plan PUBLISHED ≠ Goal DONE；CANDIDATE/REJECTED 亦不为终局。`;
}

export function planStatusCaption(status: string): string {
  if (status === 'PUBLISHED') {
    return 'PUBLISHED：计划已发布、可调度；≠ Goal DONE。';
  }
  if (status === 'CANDIDATE') {
    return 'CANDIDATE：候选计划，未发布；≠ DONE。';
  }
  if (status === 'REJECTED') {
    return 'REJECTED：计划被拒绝；≠ 终局裁决。';
  }
  return `${status}：只读观察。`;
}
