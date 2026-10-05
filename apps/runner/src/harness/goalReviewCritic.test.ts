import {expect, test, vi} from 'vitest';
import {bindingDigestOf} from './fakePlanHost.js';
import {
  findingsFromReviewSnapshot,
  runGoalReviewCriticTurnWithLease,
  type GoalReviewCriticPorts,
} from './goalReviewCritic.js';

test('findingsFromReviewSnapshot：未决 effect → BLOCKER', () => {
  const findings = findingsFromReviewSnapshot({
    open_effect_ids: ['e1'],
    recent_activities: [],
  });
  expect(findings.some((f) => f.code === 'OPEN_EFFECTS' && f.severity === 'BLOCKER')).toBe(
    true,
  );
});

test('findingsFromReviewSnapshot：无问题 → INFO NO_BLOCKER', () => {
  const findings = findingsFromReviewSnapshot({
    open_effect_ids: [],
    recent_activities: [{kind: 'EXECUTE', status: 'SUCCEEDED'}],
  });
  expect(findings).toEqual([
    expect.objectContaining({code: 'NO_BLOCKER', severity: 'INFO'}),
  ]);
});

test('runGoalReviewCriticTurnWithLease：compile/bind/outcome，marks_goal_done=false', async () => {
  const snapDigest = 'sha256:' + 'ab'.repeat(32);
  const binding = {
    subject_digest: snapDigest,
    goal_contract_revision: 1,
    plan_revision: 1,
  };
  const submit = vi.fn(async () => undefined);
  const ports: Omit<GoalReviewCriticPorts, 'claimAudit'> = {
    compileContext: async () => ({
      id: 'bundle-1',
      content_digest: 'sha256:' + 'cd'.repeat(32),
      content: {
        input_bindings: [
          {
            classification: 'EVIDENCE',
            digest: snapDigest,
            artifact_id: 'art-snap',
          },
        ],
      },
    }),
    bindContext: async () => ({context_digest: 'sha256:' + 'ef'.repeat(32)}),
    readEvidenceArtifact: async () =>
      JSON.stringify({
        open_effect_ids: [],
        recent_activities: [],
        goal_status: 'RUNNING',
      }),
    submitGoalReviewOutcome: submit,
  };

  const result = await runGoalReviewCriticTurnWithLease(ports, {
    lease: {
      activity_id: 'act-audit',
      attempt_id: 'att-1',
      fencing_epoch: '1',
    },
    activity: {
      id: 'act-audit',
      kind: 'AUDIT',
      state_revision: 3,
      binding,
      goal_id: 'goal-1',
      task_id: null,
      project_id: 'proj-1',
      target: {type: 'GOAL_REVIEW', id: 'goal-1'},
    },
  });

  expect(result).toMatchObject({
    status: 'ACTIVATION_SUBMITTED',
    kind: 'AUDIT',
    pending_harness: false,
    marks_goal_done: false,
    finding_codes: ['NO_BLOCKER'],
  });
  expect(submit).toHaveBeenCalledWith(
    expect.objectContaining({
      activityId: 'act-audit',
      expectedStateRevision: 3,
      review: expect.objectContaining({
        review_snapshot_digest: snapDigest,
        findings: [expect.objectContaining({code: 'NO_BLOCKER'})],
      }),
    }),
  );
  // bindingDigest 与 Python sort_keys 对齐（供 bind 使用）
  expect(bindingDigestOf(binding).startsWith('sha256:')).toBe(true);
});

test('非 GOAL_REVIEW target → FAILED UNSUPPORTED', async () => {
  const result = await runGoalReviewCriticTurnWithLease(
    {
      compileContext: async () => {
        throw new Error('should not compile');
      },
      bindContext: async () => {
        throw new Error('should not bind');
      },
      readEvidenceArtifact: async () => '',
      submitGoalReviewOutcome: async () => undefined,
    },
    {
      lease: {
        activity_id: 'act-c',
        attempt_id: 'att-1',
        fencing_epoch: '1',
      },
      activity: {
        id: 'act-c',
        kind: 'AUDIT',
        state_revision: 1,
        binding: {},
        goal_id: 'goal-1',
        task_id: 'task-1',
        project_id: 'proj-1',
        target: {type: 'CANDIDATE', id: 'cand-1'},
      },
    },
  );
  expect(result.status).toBe('FAILED');
  expect(result.reason).toContain('UNSUPPORTED_AUDIT_TARGET');
  expect(result.marks_goal_done).toBe(false);
});
