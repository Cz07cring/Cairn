/**
 * 审批列表投影；裁决成功 ≠ Effect SUCCEEDED ≠ Goal DONE。
 */
export type ApprovalRow = {
  id: string;
  status: string;
  subjectType: string;
  subjectId: string;
  goalId: string | null;
  stateRevision: number;
  payloadDigest: string;
  expiresAt: string | null;
  decisionReason: string | null;
};

export type ApprovalListItem = {
  id: string;
  status: string;
  subject: {type: string; id: string};
  goal_id?: string | null;
  state_revision: number;
  payload_digest: string;
  expires_at?: string | null;
  decision_reason?: string | null;
};

export function approvalRowsFromList(items: ApprovalListItem[]): ApprovalRow[] {
  return items.map((item) => ({
    id: item.id,
    status: String(item.status ?? ''),
    subjectType: String(item.subject?.type ?? ''),
    subjectId: String(item.subject?.id ?? ''),
    goalId: item.goal_id ?? null,
    stateRevision: Number(item.state_revision),
    payloadDigest: String(item.payload_digest ?? ''),
    expiresAt: item.expires_at ?? null,
    decisionReason: item.decision_reason ?? null,
  }));
}

export function approvalsCaption(input: {
  rowCount: number;
  pendingCount: number;
  filterStatus: string | null;
}): string {
  const filter = input.filterStatus
    ? `筛选 ${input.filterStatus}；`
    : '全部状态；';
  return `${filter}共 ${input.rowCount} 条，其中 PENDING ${input.pendingCount}。裁决/撤销只改审批行，≠ Effect 成功，≠ Goal DONE。`;
}

export function canApproverAct(roles: string[] | undefined | null): boolean {
  if (!roles?.length) {
    return false;
  }
  const set = new Set(roles);
  return set.has('approver') || set.has('admin');
}

export function approvalDecisionSuccessCaption(
  decision: 'APPROVE' | 'DENY',
): string {
  return decision === 'APPROVE'
    ? '已批准该审批（≠ Effect 已执行，≠ Goal DONE）。'
    : '已拒绝该审批（≠ Goal DONE）。';
}

export function approvalRevokeSuccessCaption(): string {
  return '已撤销该审批（≠ Goal DONE）。';
}
