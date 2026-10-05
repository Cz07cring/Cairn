import {expect, test, vi} from 'vitest';
import {
  runIntegrateTurnWithLease,
  selectIntegrateCandidateIdFromAudits,
  type IntegratePorts,
} from './integrateHost.js';

const lease = {
  activity_id: 'act-int',
  attempt_id: 'att-1',
  fencing_epoch: '1',
};

const claimedBase = {
  lease,
  activity: {
    id: 'act-int',
    kind: 'INTEGRATE' as const,
    state_revision: 2,
    binding: {},
    goal_id: 'goal-1',
    task_id: null,
    project_id: 'proj-1',
    target: {type: 'INTEGRATION', id: 'goal-1'},
  },
};

test('runIntegrateTurnWithLease：解析候选 → outcome，marks_goal_done=false', async () => {
  const submit = vi.fn(async () => undefined);
  const ports: Omit<IntegratePorts, 'claimIntegrate'> = {
    resolveIntegrateCandidate: async () => ({
      candidateManifestId: 'cand-pass',
      integrationCommit: 'a'.repeat(40),
      evidenceIds: [],
    }),
    submitIntegrateOutcome: submit,
  };

  const result = await runIntegrateTurnWithLease(ports, claimedBase);
  expect(result).toMatchObject({
    status: 'ACTIVATION_SUBMITTED',
    kind: 'INTEGRATE',
    candidate_manifest_id: 'cand-pass',
    marks_goal_done: false,
  });
  expect(submit).toHaveBeenCalledWith(
    expect.objectContaining({
      candidateManifestId: 'cand-pass',
      integrationCommit: 'a'.repeat(40),
    }),
  );
});

test('解析失败 → FAILED，不 stub 候选', async () => {
  const result = await runIntegrateTurnWithLease(
    {
      resolveIntegrateCandidate: async () => {
        throw new Error('NO_PASS_CANDIDATE_AUDIT');
      },
      submitIntegrateOutcome: async () => undefined,
    },
    claimedBase,
  );
  expect(result.status).toBe('FAILED');
  expect(result.reason).toContain('NO_PASS_CANDIDATE_AUDIT');
  expect(result.marks_goal_done).toBe(false);
});

test('非 INTEGRATION target → FAILED', async () => {
  const result = await runIntegrateTurnWithLease(
    {
      resolveIntegrateCandidate: async () => ({
        candidateManifestId: 'x',
        integrationCommit: null,
      }),
      submitIntegrateOutcome: async () => undefined,
    },
    {
      ...claimedBase,
      activity: {
        ...claimedBase.activity,
        target: {type: 'CANDIDATE', id: 'c'},
      },
    },
  );
  expect(result.status).toBe('FAILED');
  expect(result.reason).toContain('UNSUPPORTED_INTEGRATE_TARGET');
});

test('selectIntegrateCandidateIdFromAudits：仅 aggregation=PASS 可入选', () => {
  const id = selectIntegrateCandidateIdFromAudits([
    {
      record_type: 'CANDIDATE_AUDIT',
      audit: {
        verdict: 'PASS',
        layer: 'MECHANICAL',
        subject_candidate_manifest_id: 'cand-a',
      },
      // 单层 PASS 但缺 SEMANTIC → Kernel 标 INSUFFICIENT
      aggregation: {
        verdict: 'INSUFFICIENT',
        subject_candidate_manifest_id: 'cand-a',
        missing_items: [{layer: 'SEMANTIC'}],
        accepted_audit_ids: ['a1'],
      },
    },
    {
      record_type: 'CANDIDATE_AUDIT',
      audit: {
        verdict: 'FAIL',
        layer: 'SEMANTIC',
        subject_candidate_manifest_id: 'cand-a',
      },
      aggregation: {
        verdict: 'FAIL',
        subject_candidate_manifest_id: 'cand-a',
        accepted_audit_ids: ['a1', 'a2'],
      },
    },
    {
      record_type: 'CANDIDATE_AUDIT',
      audit: {
        verdict: 'PASS',
        layer: 'MECHANICAL',
        subject_candidate_manifest_id: 'cand-b',
      },
      aggregation: {
        verdict: 'PASS',
        subject_candidate_manifest_id: 'cand-b',
        missing_items: [],
        accepted_audit_ids: ['b1'],
      },
    },
    {
      record_type: 'CANDIDATE_AUDIT',
      audit: {
        verdict: 'PASS',
        layer: 'SEMANTIC',
        subject_candidate_manifest_id: 'cand-b',
      },
      aggregation: {
        verdict: 'PASS',
        subject_candidate_manifest_id: 'cand-b',
        missing_items: [],
        accepted_audit_ids: ['b1', 'b2'],
      },
    },
  ]);
  expect(id).toBe('cand-b');
});

test('selectIntegrateCandidateIdFromAudits：audit PASS 但 aggregation 缺层 → 抛错', () => {
  expect(() =>
    selectIntegrateCandidateIdFromAudits([
      {
        record_type: 'CANDIDATE_AUDIT',
        audit: {
          verdict: 'PASS',
          layer: 'MECHANICAL',
          subject_candidate_manifest_id: 'cand-partial',
        },
        aggregation: {
          verdict: 'INSUFFICIENT',
          subject_candidate_manifest_id: 'cand-partial',
          missing_items: [{layer: 'SEMANTIC'}],
          accepted_audit_ids: ['x1'],
        },
      },
    ]),
  ).toThrow(/NO_ELIGIBLE/);
});

test('selectIntegrateCandidateIdFromAudits：仅 FAIL aggregation → 抛错', () => {
  expect(() =>
    selectIntegrateCandidateIdFromAudits([
      {
        record_type: 'CANDIDATE_AUDIT',
        audit: {
          verdict: 'FAIL',
          layer: 'MECHANICAL',
          subject_candidate_manifest_id: 'cand-x',
        },
        aggregation: {
          verdict: 'FAIL',
          subject_candidate_manifest_id: 'cand-x',
          accepted_audit_ids: ['x1'],
        },
      },
    ]),
  ).toThrow(/NO_ELIGIBLE/);
});
