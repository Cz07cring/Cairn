import {expect, test, vi} from 'vitest';
import {
  runCandidateAuditTurnWithLease,
  type CandidateAuditPorts,
} from './candidateAuditor.js';

const lease = {
  activity_id: 'act-audit',
  attempt_id: 'att-1',
  fencing_epoch: '1',
};

const claimedBase = {
  lease,
  activity: {
    id: 'act-audit',
    kind: 'AUDIT' as const,
    state_revision: 2,
    binding: {
      goal_contract_revision: 1,
      task_contract_revision: 1,
    },
    goal_id: 'goal-1',
    task_id: 'task-1',
    project_id: 'proj-1',
    target: {type: 'CANDIDATE', id: 'cand-1'},
    verification_assignments: [
      {
        subject_id: 'cand-1',
        subject_digest: 'sha256:' + 'aa'.repeat(32),
        verification_profile_id: 'prof-1',
        layer: 'MECHANICAL',
        audit_round: 1,
      },
    ],
  },
};

test('runCandidateAuditTurnWithLease：观察 PASS → outcome，marks_goal_done=false', async () => {
  const createRun = vi.fn(async () => ({runId: 'run-1'}));
  const submit = vi.fn(async () => undefined);
  const ports: Omit<CandidateAuditPorts, 'claimAudit'> = {
    observeCandidate: async () => ({
      checksPassed: true,
      criterionId: 'A1',
      reason: '测绿',
      evidenceArtifactId: '11111111-1111-1111-1111-111111111111',
      receiptArtifactId: '22222222-2222-2222-2222-222222222222',
      verifierDigest: 'sha256:' + 'bb'.repeat(32),
      subjectDigest: 'sha256:' + 'aa'.repeat(32),
    }),
    createVerificationRun: createRun,
    submitCandidateAuditOutcome: submit,
  };

  const result = await runCandidateAuditTurnWithLease(ports, claimedBase);
  expect(result).toMatchObject({
    status: 'ACTIVATION_SUBMITTED',
    kind: 'AUDIT',
    verdict: 'PASS',
    verifier_run_id: 'run-1',
    marks_goal_done: false,
    pending_harness: false,
  });
  expect(submit).toHaveBeenCalledWith(
    expect.objectContaining({
      audit: expect.objectContaining({
        verdict: 'PASS',
        verifier_run_ids: ['run-1'],
        subject_candidate_manifest_id: 'cand-1',
      }),
    }),
  );
});

test('观察失败 → FAIL verdict，仍 marks_goal_done=false', async () => {
  const result = await runCandidateAuditTurnWithLease(
    {
      observeCandidate: async () => ({
        checksPassed: false,
        criterionId: 'A1',
        reason: '测红',
        evidenceArtifactId: '11111111-1111-1111-1111-111111111111',
        receiptArtifactId: '22222222-2222-2222-2222-222222222222',
        verifierDigest: 'sha256:' + 'bb'.repeat(32),
        subjectDigest: 'sha256:' + 'aa'.repeat(32),
      }),
      createVerificationRun: async () => ({runId: 'run-fail'}),
      submitCandidateAuditOutcome: async () => undefined,
    },
    claimedBase,
  );
  expect(result.verdict).toBe('FAIL');
  expect(result.marks_goal_done).toBe(false);
});

test('GOAL_REVIEW target → FAILED UNSUPPORTED', async () => {
  const result = await runCandidateAuditTurnWithLease(
    {
      observeCandidate: async () => {
        throw new Error('should not observe');
      },
      createVerificationRun: async () => ({runId: 'x'}),
      submitCandidateAuditOutcome: async () => undefined,
    },
    {
      ...claimedBase,
      activity: {
        ...claimedBase.activity,
        target: {type: 'GOAL_REVIEW', id: 'goal-1'},
        verification_assignments: undefined,
      },
    },
  );
  expect(result.status).toBe('FAILED');
  expect(result.reason).toContain('UNSUPPORTED_AUDIT_TARGET');
});
