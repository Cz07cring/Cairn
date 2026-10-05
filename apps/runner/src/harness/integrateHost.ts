/**
 * INTEGRATE：从 Goal 已 PASS 的候选验收解析候选 → 提交 IntegrateSuccessOutcome。
 * 开启最终屏障由 Kernel 完成；Runner 不自报 Goal DONE / VERIFYING。
 */
import type {ActivityLeaseView, LeaseIdentity} from './fakePlanHost.js';

export type IntegrateCandidateRef = {
  candidateManifestId: string;
  /** 来自 CandidateManifest.git_commit；可为 null */
  integrationCommit: string | null;
  evidenceIds?: string[];
};

/** Goal audits 列表项（CANDIDATE_AUDIT 子集；须带 Kernel aggregation） */
export type IntegrateAuditHint = {
  record_type?: string;
  audit?: {
    verdict?: string;
    layer?: string;
    subject_candidate_manifest_id?: string;
  };
  /** Kernel 权威汇总：含 Task acceptance → profile.required_layers 槽齐全性 */
  aggregation?: {
    verdict?: string;
    subject_candidate_manifest_id?: string;
    missing_items?: unknown[];
    accepted_audit_ids?: unknown[];
  };
};

/**
 * 从 Goal audits（通常 ASC）选出可集成候选：
 * - 权威：`aggregation.verdict === 'PASS'`（Kernel 已核 required 槽 / 无 FAIL）
 * - 禁止仅凭单条 audit.verdict=PASS 冒充齐全（缺层时 aggregation=INSUFFICIENT）
 * - 多合格候选：择 accepted_audit_ids 更多者，同则取列表更后者
 */
export function selectIntegrateCandidateIdFromAudits(
  audits: IntegrateAuditHint[],
): string {
  type Acc = {acceptedCount: number; lastIndex: number};
  const byCand = new Map<string, Acc>();

  audits.forEach((item, index) => {
    if (item.record_type !== 'CANDIDATE_AUDIT') return;
    const agg = item.aggregation;
    if (!agg || agg.verdict !== 'PASS') return;
    const id =
      (typeof agg.subject_candidate_manifest_id === 'string' &&
      agg.subject_candidate_manifest_id
        ? agg.subject_candidate_manifest_id
        : undefined) ??
      (typeof item.audit?.subject_candidate_manifest_id === 'string'
        ? item.audit.subject_candidate_manifest_id
        : undefined);
    if (typeof id !== 'string' || !id) return;
    const acceptedCount = Array.isArray(agg.accepted_audit_ids)
      ? agg.accepted_audit_ids.length
      : 0;
    const prev = byCand.get(id);
    if (
      !prev ||
      acceptedCount > prev.acceptedCount ||
      (acceptedCount === prev.acceptedCount && index > prev.lastIndex)
    ) {
      byCand.set(id, {acceptedCount, lastIndex: index});
    }
  });

  let bestId: string | null = null;
  let bestCount = -1;
  let bestIndex = -1;
  for (const [id, acc] of byCand) {
    if (
      acc.acceptedCount > bestCount ||
      (acc.acceptedCount === bestCount && acc.lastIndex > bestIndex)
    ) {
      bestId = id;
      bestCount = acc.acceptedCount;
      bestIndex = acc.lastIndex;
    }
  }
  if (!bestId) {
    throw new Error(
      'NO_ELIGIBLE_CANDIDATE_AUDIT（须 Kernel aggregation.verdict=PASS，含 required 槽齐全；禁止 stub）',
    );
  }
  return bestId;
}

export type IntegratePorts = {
  claimIntegrate: () => Promise<ActivityLeaseView | null>;
  /**
   * 解析待集成候选：须来自已验收事实（如 Goal audits PASS），禁止硬编码假 UUID。
   */
  resolveIntegrateCandidate: (
    claimed: ActivityLeaseView,
  ) => Promise<IntegrateCandidateRef>;
  submitIntegrateOutcome: (input: {
    activityId: string;
    lease: LeaseIdentity;
    expectedStateRevision: number;
    candidateManifestId: string;
    integrationCommit: string | null;
    evidenceIds: string[];
  }) => Promise<void>;
};

export type IntegrateResult = {
  status: 'ACTIVATION_SUBMITTED' | 'FAILED';
  pending_harness: false;
  kind: 'INTEGRATE';
  reason?: string;
  activity_id: string;
  attempt_id?: string;
  fencing_epoch?: string;
  candidate_manifest_id?: string;
  /** 恒 false：INTEGRATE ≠ Goal DONE（随后 VERIFYING 由 Kernel） */
  marks_goal_done: false;
};

/**
 * 已持有租约：解析候选 → INTEGRATE outcome。
 */
export async function runIntegrateTurnWithLease(
  ports: Omit<IntegratePorts, 'claimIntegrate'>,
  claimed: ActivityLeaseView,
): Promise<IntegrateResult> {
  const {lease, activity} = claimed;
  if (activity.kind !== 'INTEGRATE') {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'INTEGRATE',
      reason: `UNEXPECTED_ACTIVITY_KIND:${activity.kind}`,
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }
  if (activity.target?.type !== 'INTEGRATION') {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'INTEGRATE',
      reason: `UNSUPPORTED_INTEGRATE_TARGET:${activity.target?.type ?? 'missing'}`,
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  let candidate: IntegrateCandidateRef;
  try {
    candidate = await ports.resolveIntegrateCandidate(claimed);
  } catch (err) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'INTEGRATE',
      reason: `INTEGRATE_RESOLVE_FAILED:${err instanceof Error ? err.message : String(err)}`,
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  if (!candidate.candidateManifestId) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'INTEGRATE',
      reason: 'MISSING_CANDIDATE_MANIFEST_ID',
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  await ports.submitIntegrateOutcome({
    activityId: activity.id,
    lease,
    expectedStateRevision: activity.state_revision,
    candidateManifestId: candidate.candidateManifestId,
    integrationCommit: candidate.integrationCommit,
    evidenceIds: candidate.evidenceIds ?? [],
  });

  return {
    status: 'ACTIVATION_SUBMITTED',
    pending_harness: false,
    kind: 'INTEGRATE',
    activity_id: activity.id,
    attempt_id: lease.attempt_id,
    fencing_epoch: lease.fencing_epoch,
    candidate_manifest_id: candidate.candidateManifestId,
    marks_goal_done: false,
  };
}

export async function runIntegrateTurn(
  ports: IntegratePorts,
): Promise<IntegrateResult> {
  const claimed = await ports.claimIntegrate();
  if (!claimed) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'INTEGRATE',
      reason: 'INTEGRATE activity 不可读（GET 404 / idle）',
      activity_id: '',
      marks_goal_done: false,
    };
  }
  return runIntegrateTurnWithLease(ports, claimed);
}
