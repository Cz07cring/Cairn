import {expect, test, vi} from 'vitest';
import {
  runFinalizeTurnWithLease,
  type FinalizeAuditorPorts,
} from './finalizeAuditor.js';

const lease = {
  activity_id: 'act-fin',
  attempt_id: 'att-1',
  fencing_epoch: '1',
};

const claimedBase = {
  lease,
  activity: {
    id: 'act-fin',
    kind: 'FINALIZE' as const,
    state_revision: 2,
    binding: {goal_contract_revision: 1},
    goal_id: 'goal-1',
    task_id: null,
    project_id: 'proj-1',
    target: {type: 'CANDIDATE', id: 'cand-1'},
    verification_assignments: [
      {
        subject_id: 'cand-1',
        subject_digest: 'sha256:' + 'aa'.repeat(32),
        verification_profile_id: 'prof-global',
        layer: 'GLOBAL',
        audit_round: 1,
      },
    ],
  },
};

test('runFinalizeTurnWithLease：观察 PASS → outcome，marks_goal_done=false', async () => {
  const createRun = vi.fn(async () => ({runId: 'run-fin'}));
  const submit = vi.fn(async () => undefined);
  const ports: Omit<FinalizeAuditorPorts, 'claimFinalize'> = {
    loadBarrierContext: async () => ({
      barrierId: 'bar-1',
      candidateManifestId: 'cand-1',
      goalContractRevision: 1,
      criterionByProfile: {['prof-global']: 'C1'},
    }),
    observeFinalize: async () => ({
      checksPassed: true,
      criterionId: 'C1',
      reason: '绿',
      evidenceArtifactId: '11111111-1111-1111-1111-111111111111',
      receiptArtifactId: '22222222-2222-2222-2222-222222222222',
      verifierDigest: 'sha256:' + 'bb'.repeat(32),
      subjectDigest: 'sha256:' + 'aa'.repeat(32),
    }),
    createVerificationRun: createRun,
    submitFinalizeOutcome: submit,
  };

  const result = await runFinalizeTurnWithLease(ports, claimedBase);
  expect(result).toMatchObject({
    status: 'ACTIVATION_SUBMITTED',
    kind: 'FINALIZE',
    verdict: 'PASS',
    verifier_run_ids: ['run-fin'],
    marks_goal_done: false,
  });
  expect(submit).toHaveBeenCalledWith(
    expect.objectContaining({
      barrierId: 'bar-1',
      candidateManifestId: 'cand-1',
      globalAudits: [
        expect.objectContaining({
          layer: 'GLOBAL',
          verdict: 'PASS',
          verifier_run_ids: ['run-fin'],
        }),
      ],
    }),
  );
});

test('观察红 → FAIL verdict，仍 marks_goal_done=false（不 stub DONE）', async () => {
  const result = await runFinalizeTurnWithLease(
    {
      loadBarrierContext: async () => ({
        barrierId: 'bar-1',
        candidateManifestId: 'cand-1',
        goalContractRevision: 1,
        criterionByProfile: {['prof-global']: 'C1'},
      }),
      observeFinalize: async () => ({
        checksPassed: false,
        criterionId: 'C1',
        reason: '红',
        evidenceArtifactId: '11111111-1111-1111-1111-111111111111',
        receiptArtifactId: '22222222-2222-2222-2222-222222222222',
        verifierDigest: 'sha256:' + 'bb'.repeat(32),
        subjectDigest: 'sha256:' + 'aa'.repeat(32),
      }),
      createVerificationRun: async () => ({runId: 'run-fail'}),
      submitFinalizeOutcome: async () => undefined,
    },
    claimedBase,
  );
  expect(result.verdict).toBe('FAIL');
  expect(result.marks_goal_done).toBe(false);
});

test('屏障候选与 target 不一致 → FAILED', async () => {
  const result = await runFinalizeTurnWithLease(
    {
      loadBarrierContext: async () => ({
        barrierId: 'bar-1',
        candidateManifestId: 'other-cand',
        goalContractRevision: 1,
        criterionByProfile: {},
      }),
      observeFinalize: async () => {
        throw new Error('should not observe');
      },
      createVerificationRun: async () => ({runId: 'x'}),
      submitFinalizeOutcome: async () => undefined,
    },
    claimedBase,
  );
  expect(result.status).toBe('FAILED');
  expect(result.reason).toContain('BARRIER_CANDIDATE_MISMATCH');
});
