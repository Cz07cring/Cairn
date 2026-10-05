/**
 * FINALIZE：Broker auditor 观察 → VerificationRun(s) → FinalizeSuccessOutcome。
 * 提交后 Goal 是否 DONE 仅由 Kernel + GLOBAL VerificationProfile + 屏障裁决；
 * 本模块恒 marks_goal_done=false（不自报 DONE）。
 */
import type {ActivityLeaseView, LeaseIdentity} from './fakePlanHost.js';
import type {
  CandidateObservation,
  VerificationAssignment,
} from './candidateAuditor.js';

export type FinalizeBarrierContext = {
  barrierId: string;
  candidateManifestId: string;
  goalContractRevision: number;
  /** profile_id → success_criteria.id */
  criterionByProfile: Record<string, string>;
};

export type FinalizeAuditorPorts = {
  claimFinalize: () => Promise<ActivityLeaseView | null>;
  /** GET Goal → barrier + contract；禁止硬编码 barrier */
  loadBarrierContext: (claimed: ActivityLeaseView) => Promise<FinalizeBarrierContext>;
  observeFinalize: (claimed: ActivityLeaseView) => Promise<CandidateObservation>;
  createVerificationRun: (input: {
    lease: LeaseIdentity;
    activity: ActivityLeaseView['activity'];
    assignment: VerificationAssignment;
    observation: CandidateObservation;
    criterionId: string;
  }) => Promise<{runId: string}>;
  submitFinalizeOutcome: (input: {
    activityId: string;
    lease: LeaseIdentity;
    expectedStateRevision: number;
    barrierId: string;
    candidateManifestId: string;
    globalAudits: Record<string, unknown>[];
    evidenceIds: string[];
  }) => Promise<void>;
};

export type FinalizeAuditorResult = {
  status: 'ACTIVATION_SUBMITTED' | 'FAILED';
  pending_harness: false;
  kind: 'FINALIZE';
  reason?: string;
  activity_id: string;
  attempt_id?: string;
  fencing_epoch?: string;
  verdict?: 'PASS' | 'FAIL' | 'INSUFFICIENT';
  verifier_run_ids?: string[];
  /** 恒 false：Runner 不自报 Goal DONE（Kernel 可能随后裁决 DONE） */
  marks_goal_done: false;
};

function allAssignments(
  activity: ActivityLeaseView['activity'],
): VerificationAssignment[] {
  const rows = activity.verification_assignments;
  if (!Array.isArray(rows) || rows.length === 0) {
    return [];
  }
  const out: VerificationAssignment[] = [];
  for (const raw of rows) {
    const row = raw as VerificationAssignment;
    if (!row?.verification_profile_id || row.audit_round == null || !row.layer) {
      continue;
    }
    out.push(row);
  }
  return out;
}

/**
 * 已持有租约：观察 → 每 assignment 一条 VerificationRun + global_audit → FINALIZE outcome。
 */
export async function runFinalizeTurnWithLease(
  ports: Omit<FinalizeAuditorPorts, 'claimFinalize'>,
  claimed: ActivityLeaseView,
): Promise<FinalizeAuditorResult> {
  const {lease, activity} = claimed;
  if (activity.kind !== 'FINALIZE') {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'FINALIZE',
      reason: `UNEXPECTED_ACTIVITY_KIND:${activity.kind}`,
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }
  if (activity.target?.type !== 'CANDIDATE' || !activity.target.id) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'FINALIZE',
      reason: `MISSING_FINALIZE_CANDIDATE_TARGET`,
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  const assignments = allAssignments(activity);
  if (assignments.length === 0) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'FINALIZE',
      reason: 'MISSING_VERIFICATION_ASSIGNMENT',
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  let barrier: FinalizeBarrierContext;
  try {
    barrier = await ports.loadBarrierContext(claimed);
  } catch (err) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'FINALIZE',
      reason: `BARRIER_CONTEXT_FAILED:${err instanceof Error ? err.message : String(err)}`,
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  if (barrier.candidateManifestId !== activity.target.id) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'FINALIZE',
      reason: 'BARRIER_CANDIDATE_MISMATCH',
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  let observation: CandidateObservation;
  try {
    observation = await ports.observeFinalize(claimed);
  } catch (err) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'FINALIZE',
      reason: `FINALIZE_OBSERVE_FAILED:${err instanceof Error ? err.message : String(err)}`,
      activity_id: activity.id,
      attempt_id: lease.attempt_id,
      fencing_epoch: lease.fencing_epoch,
      marks_goal_done: false,
    };
  }

  const verdict: 'PASS' | 'FAIL' = observation.checksPassed ? 'PASS' : 'FAIL';
  const runIds: string[] = [];
  const globalAudits: Record<string, unknown>[] = [];
  const evidenceIds = [observation.evidenceArtifactId];

  for (const assignment of assignments) {
    const criterionId =
      barrier.criterionByProfile[assignment.verification_profile_id] ??
      observation.criterionId;
    const {runId} = await ports.createVerificationRun({
      lease,
      activity,
      assignment,
      observation: {...observation, criterionId},
      criterionId,
    });
    runIds.push(runId);
    globalAudits.push({
      subject_candidate_manifest_id: barrier.candidateManifestId,
      goal_contract_revision: barrier.goalContractRevision,
      task_contract_revision: null,
      verification_profile_id: assignment.verification_profile_id,
      layer: assignment.layer,
      audit_round: assignment.audit_round,
      verifier_run_ids: [runId],
      verdict,
      criterion_results: [
        {
          criterion_id: criterionId,
          verdict,
          evidence_ids: [observation.evidenceArtifactId],
          reason: observation.reason,
        },
      ],
      evidence_ids: [observation.evidenceArtifactId],
      reason: observation.reason,
    });
  }

  await ports.submitFinalizeOutcome({
    activityId: activity.id,
    lease,
    expectedStateRevision: activity.state_revision,
    barrierId: barrier.barrierId,
    candidateManifestId: barrier.candidateManifestId,
    globalAudits,
    evidenceIds,
  });

  return {
    status: 'ACTIVATION_SUBMITTED',
    pending_harness: false,
    kind: 'FINALIZE',
    activity_id: activity.id,
    attempt_id: lease.attempt_id,
    fencing_epoch: lease.fencing_epoch,
    verdict,
    verifier_run_ids: runIds,
    marks_goal_done: false,
  };
}

export async function runFinalizeTurn(
  ports: FinalizeAuditorPorts,
): Promise<FinalizeAuditorResult> {
  const claimed = await ports.claimFinalize();
  if (!claimed) {
    return {
      status: 'FAILED',
      pending_harness: false,
      kind: 'FINALIZE',
      reason: 'FINALIZE activity 不可读（GET 404 / idle）',
      activity_id: '',
      marks_goal_done: false,
    };
  }
  return runFinalizeTurnWithLease(ports, claimed);
}
