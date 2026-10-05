/**
 * 候选 AUDIT（target=CANDIDATE）：观察 → VerificationRun → AuditCandidateOutcome。
 * 观察须由宿主注入（Broker/真实 verifier）；禁止本地 stub PASS。
 * ≠ Goal DONE（Task 是否 DONE 仅由 Kernel 聚合裁决）。
 */
import {createHash, randomUUID} from 'node:crypto';
import type {ActivityLeaseView, LeaseIdentity} from './fakePlanHost.js';

export type VerificationAssignment = {
  subject_type?: string;
  subject_id?: string;
  subject_digest?: string;
  verification_profile_id: string;
  profile_digest?: string;
  layer: string;
  audit_round: number;
};

export type CandidateObservation = {
  /** true→PASS；false→FAIL；不得无观察却报 PASS */
  checksPassed: boolean;
  criterionId: string;
  reason: string;
  evidenceArtifactId: string;
  receiptArtifactId: string;
  verifierDigest: string;
  subjectDigest: string;
  inputDigest?: string;
  environmentDigest?: string;
};

export type CandidateAuditPorts = {
  claimAudit: () => Promise<ActivityLeaseView | null>;
  /** 真实观察（Broker run_tests / 隔离 verifier）；禁止恒返回 PASS 的假实现冒充完成。 */
  observeCandidate: (claimed: ActivityLeaseView) => Promise<CandidateObservation>;
  createVerificationRun: (input: {
    lease: LeaseIdentity;
    activity: ActivityLeaseView['activity'];
    assignment: VerificationAssignment;
    observation: CandidateObservation;
  }) => Promise<{runId: string}>;
  submitCandidateAuditOutcome: (input: {
    activityId: string;
    lease: LeaseIdentity;
    expectedStateRevision: number;
    audit: Record<string, unknown>;
  }) => Promise<void>;
};

export type CandidateAuditResult = {
  status: 'ACTIVATION_SUBMITTED' | 'FAILED';
  pending_harness: false;
  kind: 'AUDIT';
  reason?: string;
  activity_id: string;
  attempt_id?: string;
  fencing_epoch?: string;
  verdict?: 'PASS' | 'FAIL' | 'INSUFFICIENT';
  verifier_run_id?: string;
  /** 恒 false：AUDIT outcome ≠ Goal DONE */
  marks_goal_done: false;
};

function digestOf(label: string): string {
  return (
    'sha256:' + createHash('sha256').update(label).digest('hex')
  );
}

function firstAssignment(
  activity: ActivityLeaseView['activity'],
): VerificationAssignment | null {
  const rows = activity.verification_assignments;
  if (!Array.isArray(rows) || rows.length === 0) {
    return null;
  }
  const row = rows[0] as VerificationAssignment;
  if (!row?.verification_profile_id || row.audit_round == null || !row.layer) {
    return null;
  }
  return row;
}

/**
 * 已持有租约：观察 → VerificationRun → CANDIDATE outcome。
 */
export async function runCandidateAuditTurnWithLease(
  ports: Omit<CandidateAuditPorts, 'claimAudit'>,
  claimed: ActivityLeaseView,
): Promise<CandidateAuditResult> {
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
  if (activity.target?.type !== 'CANDIDATE') {
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

  const assignment = firstAssignment(activity);
  if (!assignment) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'AUDIT',
      reason: 'MISSING_VERIFICATION_ASSIGNMENT',
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  const candidateId =
    activity.target.id ??
    assignment.subject_id ??
    String(activity.binding?.subject_id ?? '');
  if (!candidateId) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'AUDIT',
      reason: 'MISSING_CANDIDATE_ID',
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  const binding = activity.binding ?? {};
  const goalRev = Number(binding.goal_contract_revision);
  const taskRev =
    binding.task_contract_revision == null
      ? null
      : Number(binding.task_contract_revision);
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

  let observation: CandidateObservation;
  try {
    observation = await ports.observeCandidate(claimed);
  } catch (err) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'AUDIT',
      reason: `CANDIDATE_OBSERVE_FAILED:${err instanceof Error ? err.message : String(err)}`,
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  if (!observation.verifierDigest.startsWith('sha256:')) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'AUDIT',
      reason: 'INVALID_VERIFIER_DIGEST',
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  const observationForRun: CandidateObservation = {
    ...observation,
    inputDigest: observation.inputDigest ?? digestOf(`input:${randomUUID()}`),
    environmentDigest:
      observation.environmentDigest ?? digestOf(`env:${randomUUID()}`),
  };
  const {runId} = await ports.createVerificationRun({
    lease,
    activity,
    assignment,
    observation: observationForRun,
  });

  const verdict: 'PASS' | 'FAIL' = observation.checksPassed ? 'PASS' : 'FAIL';
  await ports.submitCandidateAuditOutcome({
    activityId: activity.id,
    lease,
    expectedStateRevision: activity.state_revision,
    audit: {
      subject_candidate_manifest_id: candidateId,
      goal_contract_revision: goalRev,
      task_contract_revision:
        taskRev != null && Number.isFinite(taskRev) ? taskRev : null,
      verification_profile_id: assignment.verification_profile_id,
      layer: assignment.layer,
      audit_round: assignment.audit_round,
      verifier_run_ids: [runId],
      verdict,
      criterion_results: [
        {
          criterion_id: observation.criterionId,
          verdict,
          evidence_ids: [observation.evidenceArtifactId],
          reason: observation.reason,
        },
      ],
      evidence_ids: [observation.evidenceArtifactId],
      reason: observation.reason,
    },
  });

  return {
    status: 'ACTIVATION_SUBMITTED',
    pending_harness: false,
    kind: 'AUDIT',
    activity_id: activity.id,
    attempt_id: lease.attempt_id,
    fencing_epoch: lease.fencing_epoch,
    verdict,
    verifier_run_id: runId,
    marks_goal_done: false,
  };
}

/** claim → WithLease。 */
export async function runCandidateAuditTurn(
  ports: CandidateAuditPorts,
): Promise<CandidateAuditResult> {
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
  return runCandidateAuditTurnWithLease(ports, claimed);
}
