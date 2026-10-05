/**
 * GoalReview Critic（确定性）：从权威快照派生 findings，经 Kernel outcome 落库。
 * 非官方 Cordis AgentLoop；≠ criterion PASS；≠ Goal DONE。
 */
import {bindingDigestOf, type ActivityLeaseView, type LeaseIdentity} from './fakePlanHost.js';

export type GoalReviewFinding = {
  code: string;
  severity: 'INFO' | 'WARN' | 'BLOCKER';
  evidence_ids: string[];
  recommendation: string;
};

export type ReviewSnapshotPayload = {
  open_effect_ids?: string[];
  recent_activities?: Array<{kind: string; status: string}>;
  task_statuses?: Array<{task_id: string; status: string}>;
  goal_status?: string;
};

/** 由 Kernel 钉扎的运行快照派生诊断；规则可演进，但不得写 DONE。 */
export function findingsFromReviewSnapshot(
  snapshot: ReviewSnapshotPayload,
): GoalReviewFinding[] {
  const findings: GoalReviewFinding[] = [];
  const open = snapshot.open_effect_ids ?? [];
  if (open.length > 0) {
    findings.push({
      code: 'OPEN_EFFECTS',
      severity: 'BLOCKER',
      evidence_ids: [],
      recommendation: `存在 ${open.length} 个未决 effect，禁止最终屏障 SEAL / 新 ENGINEERING`,
    });
  }
  const failed = (snapshot.recent_activities ?? []).filter(
    (a) => a.status === 'FAILED',
  );
  if (failed.length > 0) {
    findings.push({
      code: 'RECENT_ACTIVITY_FAILED',
      severity: 'WARN',
      evidence_ids: [],
      recommendation: `近期有 ${failed.length} 个 FAILED 活动，建议复盘后重规划`,
    });
  }
  if (findings.length === 0) {
    findings.push({
      code: 'NO_BLOCKER',
      severity: 'INFO',
      evidence_ids: [],
      recommendation: '快照未见未决副作用或近期失败',
    });
  }
  return findings;
}

export type GoalReviewCriticPorts = {
  claimAudit: () => Promise<ActivityLeaseView | null>;
  compileContext: (input: {
    activityId: string;
    lease: LeaseIdentity;
    maxInputTokens?: number;
  }) => Promise<{id: string; content_digest: string; content: Record<string, unknown>}>;
  bindContext: (input: {
    activityId: string;
    lease: LeaseIdentity;
    bindingDigest: string;
    contextBundleId: string;
  }) => Promise<{context_digest: string}>;
  /** 读取 ContextBundle 中 EVIDENCE 快照工件字节（UTF-8 JSON）。 */
  readEvidenceArtifact: (artifactId: string) => Promise<string>;
  submitGoalReviewOutcome: (input: {
    activityId: string;
    lease: LeaseIdentity;
    expectedStateRevision: number;
    review: {
      goal_contract_revision: number;
      plan_revision: number | null;
      review_snapshot_digest: string;
      findings: GoalReviewFinding[];
    };
  }) => Promise<void>;
};

export type GoalReviewCriticResult = {
  status: 'ACTIVATION_SUBMITTED' | 'FAILED';
  pending_harness: false;
  kind: 'AUDIT';
  reason?: string;
  activity_id: string;
  attempt_id?: string;
  fencing_epoch?: string;
  finding_codes?: string[];
  marks_goal_done: false;
};

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/**
 * 已持有租约：compile → bind → 读快照 → 确定性 findings → GOAL_REVIEW outcome。
 * 仅支持 target=GOAL_REVIEW；候选 AUDIT 由调用方事先拒绝。
 */
export async function runGoalReviewCriticTurnWithLease(
  ports: Omit<GoalReviewCriticPorts, 'claimAudit'>,
  claimed: ActivityLeaseView,
): Promise<GoalReviewCriticResult> {
  const {lease, activity} = claimed;
  if (activity.kind !== 'AUDIT') {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'AUDIT',
      reason: `UNEXPECTED_ACTIVITY_KIND:${activity.kind}`,
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }
  if (activity.target?.type !== 'GOAL_REVIEW') {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'AUDIT',
      reason: `UNSUPPORTED_AUDIT_TARGET:${activity.target?.type ?? 'missing'}`,
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  const binding = activity.binding ?? {};
  const snapDigest = String(binding.subject_digest ?? '');
  if (!snapDigest.startsWith('sha256:')) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'AUDIT',
      reason: 'MISSING_REVIEW_SNAPSHOT_DIGEST',
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  const compiled = await ports.compileContext({
    activityId: activity.id,
    lease,
  });
  await ports.bindContext({
    activityId: activity.id,
    lease,
    bindingDigest: bindingDigestOf(binding),
    contextBundleId: compiled.id,
  });

  const content = compiled.content ?? {};
  const bindings = Array.isArray(content.input_bindings)
    ? content.input_bindings
    : [];
  const evidence = bindings.find((b) => {
    const row = asRecord(b);
    return (
      row?.classification === 'EVIDENCE' && String(row.digest ?? '') === snapDigest
    );
  });
  const evidenceRow = asRecord(evidence);
  if (!evidenceRow?.artifact_id) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'AUDIT',
      reason: 'SNAPSHOT_EVIDENCE_MISSING_IN_CONTEXT',
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  let snapshot: ReviewSnapshotPayload;
  try {
    const raw = await ports.readEvidenceArtifact(String(evidenceRow.artifact_id));
    snapshot = JSON.parse(raw) as ReviewSnapshotPayload;
  } catch (err) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'AUDIT',
      reason: `SNAPSHOT_READ_FAILED:${err instanceof Error ? err.message : String(err)}`,
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  const findings = findingsFromReviewSnapshot(snapshot);
  const goalRev = Number(binding.goal_contract_revision);
  const planRev =
    binding.plan_revision == null ? null : Number(binding.plan_revision);
  if (!Number.isFinite(goalRev) || goalRev < 1) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'AUDIT',
      reason: 'INVALID_GOAL_CONTRACT_REVISION',
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  await ports.submitGoalReviewOutcome({
    activityId: activity.id,
    lease,
    expectedStateRevision: activity.state_revision,
    review: {
      goal_contract_revision: goalRev,
      plan_revision: planRev != null && Number.isFinite(planRev) ? planRev : null,
      review_snapshot_digest: snapDigest,
      findings,
    },
  });

  return {
    status: 'ACTIVATION_SUBMITTED',
    pending_harness: false,
    kind: 'AUDIT',
    activity_id: activity.id,
    attempt_id: lease.attempt_id,
    fencing_epoch: lease.fencing_epoch,
    finding_codes: findings.map((f) => f.code),
    marks_goal_done: false,
  };
}

/** claim → WithLease；无活动则 FAILED。 */
export async function runGoalReviewCriticTurn(
  ports: GoalReviewCriticPorts,
): Promise<GoalReviewCriticResult> {
  const claimed = await ports.claimAudit();
  if (!claimed) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'AUDIT',
      reason: 'AUDIT activity 不可读（GET 404 / idle）',
      activity_id: '',
      marks_goal_done: false,
    };
  }
  return runGoalReviewCriticTurnWithLease(ports, claimed);
}
