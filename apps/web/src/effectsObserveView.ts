/**
 * Effect 列表投影；UNKNOWN ≠ 可重试失败；对账受理 ≠ Effect 终态 ≠ DONE。
 */
export type EffectRow = {
  id: string;
  status: string;
  toolRef: string;
  scope: string;
  replayClass: string;
  stateRevision: number;
  activityId: string;
  logicalStepId: string;
  evidenceIds: string[];
};

export type EffectListItem = {
  id: string;
  status: string;
  tool_ref: string;
  scope: string;
  replay_class: string;
  state_revision: number;
  activity_id: string;
  logical_step_id: string;
  evidence_ids?: string[];
};

export function effectRowsFromList(items: EffectListItem[]): EffectRow[] {
  return items.map((item) => ({
    id: item.id,
    status: String(item.status ?? ''),
    toolRef: String(item.tool_ref ?? ''),
    scope: String(item.scope ?? ''),
    replayClass: String(item.replay_class ?? ''),
    stateRevision: Number(item.state_revision),
    activityId: String(item.activity_id ?? ''),
    logicalStepId: String(item.logical_step_id ?? ''),
    evidenceIds: Array.isArray(item.evidence_ids)
      ? item.evidence_ids.map(String)
      : [],
  }));
}

export function effectsCaption(input: {
  rowCount: number;
  unknownCount: number;
  filterStatus: string | null;
}): string {
  const filter = input.filterStatus
    ? `筛选 ${input.filterStatus}；`
    : '全部状态；';
  return `${filter}共 ${input.rowCount} 条，UNKNOWN ${input.unknownCount}。UNKNOWN 不是「请求失败可重试」；禁止浏览器生成新 effect_id 盲重放。对账 202 ≠ Effect 已改写 ≠ Goal DONE。`;
}

export function effectStatusCaption(status: string): string {
  if (status === 'UNKNOWN') {
    return 'UNKNOWN：须核对既有效果；不可「再执行一次」捷径；≠ Goal DONE。';
  }
  if (status === 'SUCCEEDED') {
    return 'SUCCEEDED：工具效果成功；≠ Goal/Task DONE。';
  }
  if (status === 'DISPATCHED') {
    return 'DISPATCHED：已发出、结果未确认；≠ DONE。';
  }
  return `${status}：只读观察。`;
}

export function canApproverReconcile(
  roles: string[] | null | undefined,
): boolean {
  if (!roles?.length) {
    return false;
  }
  const set = new Set(roles);
  return set.has('approver') || set.has('admin');
}

export function reconcileSuccessCaption(): string {
  return '已受理 RECONCILE_EFFECT：创建对账 Activity，不直接改 Effect 状态；≠ Goal DONE。';
}
