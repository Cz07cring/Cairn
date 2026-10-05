import {expect, test, vi} from 'vitest';
import {
  buildFixturePlanFromGoal,
  createControlHttpCordisPorts,
  createControlHttpCordisPortsWithGoalPlan,
} from './controlHttpPorts.js';

const lease = {
  activity_id: '11111111-1111-1111-1111-111111111111',
  attempt_id: '22222222-2222-2222-2222-222222222222',
  fencing_epoch: '1',
};

const activityRow = {
  id: lease.activity_id,
  kind: 'PLAN',
  state_revision: 3,
  binding: {goal_contract_revision: 1, subject_digest: 'sha256:a'},
  goal_id: '33333333-3333-3333-3333-333333333333',
  task_id: null,
  project_id: '44444444-4444-4444-4444-444444444444',
};

const goalRow = {
  id: activityRow.goal_id,
  contract: {
    budget: {
      wall_clock_seconds: 3600,
      max_tokens: 10000,
      max_cost_usd: '1',
      max_tool_calls: 10,
      max_network_calls: 0,
      max_disk_bytes: 1048576,
      max_gpu_seconds: null,
    },
    success_criteria: [
      {
        id: 'C1',
        verification_profile_id: '55555555-5555-5555-5555-555555555555',
      },
    ],
  },
};

function jsonOk(data: unknown, status = 200) {
  return {
    ok: true,
    status,
    json: async () => ({data}),
  };
}

test('claimPlan：GET activity + admitted lease，永不 POST /claims', async () => {
  const fetchImpl = vi.fn(async (url: string) => {
    expect(String(url)).toContain(`/api/v1/activities/${lease.activity_id}`);
    expect(String(url)).not.toContain('/claims');
    return jsonOk(activityRow);
  }) as unknown as typeof fetch;

  const ports = createControlHttpCordisPorts({
    baseUrl: 'http://127.0.0.1:58101',
    authorization: 'test-jwt',
    admittedLease: lease,
    fetchImpl,
    buildPlan: () => ({tasks: []}),
  });

  const claimed = await ports.claimPlan();
  expect(claimed).toEqual({
    lease,
    activity: {
      id: activityRow.id,
      kind: 'PLAN',
      state_revision: 3,
      binding: activityRow.binding,
      goal_id: activityRow.goal_id,
      task_id: null,
      project_id: activityRow.project_id,
    },
  });
  expect(fetchImpl).toHaveBeenCalledOnce();
  const [, init] = (fetchImpl as ReturnType<typeof vi.fn>).mock.calls[0];
  expect(init.method).toBe('GET');
  expect(init.headers.Authorization).toBe('Bearer test-jwt');
});

test('compile / bind / outcome / model 路由对齐 test_plan_host', async () => {
  const calls: string[] = [];
  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    const method = init?.method ?? 'GET';
    calls.push(`${method} ${u.replace('http://127.0.0.1:58101', '')}`);
    if (u.includes('/context-compile')) {
      return jsonOk(
        {
          id: 'bundle-1',
          content_digest: 'sha256:' + 'a'.repeat(64),
          content: {role: 'PLANNER'},
        },
        201,
      );
    }
    if (u.endsWith('/context')) {
      return jsonOk({context_digest: 'sha256:' + 'b'.repeat(64)});
    }
    if (u.endsWith('/model-invocations')) {
      return jsonOk({id: 'inv-1'}, 201);
    }
    if (u.endsWith('/outcomes')) {
      return jsonOk({status: 'SUCCEEDED'});
    }
    throw new Error(`unexpected ${method} ${u}`);
  }) as unknown as typeof fetch;

  const ports = createControlHttpCordisPorts({
    baseUrl: 'http://127.0.0.1:58101/',
    authorization: 'Bearer t',
    admittedLease: lease,
    fetchImpl,
    buildPlan: () => ({reason: 'x', tasks: [{id: 't'}], coverage: []}),
  });

  const compiled = await ports.compileContext({
    activityId: lease.activity_id,
    lease,
  });
  expect(compiled.content.role).toBe('PLANNER');

  const bound = await ports.bindContext({
    activityId: lease.activity_id,
    lease,
    bindingDigest: 'sha256:bd',
    contextBundleId: 'bundle-1',
  });
  expect(bound.context_digest.startsWith('sha256:')).toBe(true);

  const created = await ports.modelInvocation.createInvocation({
    lease,
    contextDigest: bound.context_digest,
    providerRef: 'ring-kernel',
    modelId: 'plan-fixture',
    inputDigest: 'sha256:' + 'c'.repeat(64),
    toolsExposedToModel: [],
  });
  expect(created.invocationId).toBe('inv-1');

  await ports.submitPlanOutcome({
    activityId: lease.activity_id,
    lease,
    expectedStateRevision: 3,
    plan: ports.buildPlan({
      lease,
      activity: {
        id: lease.activity_id,
        kind: 'PLAN',
        state_revision: 3,
        binding: {},
        goal_id: activityRow.goal_id,
        task_id: null,
        project_id: activityRow.project_id,
      },
    }),
  });

  expect(calls).toEqual([
    `POST /internal/v1/activities/${lease.activity_id}/context-compile`,
    `POST /internal/v1/activities/${lease.activity_id}/context`,
    'POST /internal/v1/model-invocations',
    `POST /internal/v1/activities/${lease.activity_id}/outcomes`,
  ]);
  expect(calls.some((c) => c.includes('/claims'))).toBe(false);
});

test('WithGoalPlan：预取 Goal 并生成 fixture PlanCreate', async () => {
  const fetchImpl = vi.fn(async (url: string) => {
    const u = String(url);
    if (u.includes('/activities/')) {
      return jsonOk(activityRow);
    }
    if (u.includes('/goals/')) {
      return jsonOk(goalRow);
    }
    throw new Error(u);
  }) as unknown as typeof fetch;

  const ports = await createControlHttpCordisPortsWithGoalPlan({
    baseUrl: 'http://127.0.0.1:58101',
    authorization: 't',
    admittedLease: lease,
    fetchImpl,
  });
  const claimed = {
    lease,
    activity: {
      id: lease.activity_id,
      kind: 'PLAN' as const,
      state_revision: 1,
      binding: {},
      goal_id: activityRow.goal_id,
      task_id: null,
      project_id: activityRow.project_id,
    },
  };
  const plan = ports.buildPlan(claimed);
  expect(plan.reason).toBe('runner-control-http fixture plan');
  expect(Array.isArray(plan.tasks)).toBe(true);
  expect((plan.tasks as unknown[]).length).toBe(1);
  expect(Array.isArray(plan.coverage)).toBe(true);

  // live：散文不得静默回退 fixture
  expect(() =>
    ports.buildPlan(claimed, {modelNarrative: '这只是一段计划散文'}),
  ).toThrow(/未找到|LivePlanParseError|模型/);

  const fromSlots = ports.buildPlan(claimed, {
    modelNarrative: JSON.stringify({
      reason: '由模型写出的理由',
      objective: '模型任务目标',
      acceptance_description: '模型验收描述',
    }),
  });
  expect(String(fromSlots.reason)).toContain('live-qwen-plan-host:');
  expect(String(fromSlots.reason)).toContain('由模型写出的理由');
  expect(fromSlots.reason).not.toBe('runner-control-http fixture plan');
  expect(ports.livePlanPrompt?.('sha256:x')).toContain('acceptance_description');
});

test('buildFixturePlanFromGoal 覆盖首个 success criterion', () => {
  const plan = buildFixturePlanFromGoal(goalRow);
  expect(plan.coverage).toEqual([
    expect.objectContaining({
      goal_criterion_id: 'C1',
      task_acceptance_id: 'A1',
      verification_profile_id: goalRow.contract.success_criteria[0].verification_profile_id,
    }),
  ]);
});

test('claimAudit：GET activity 透出 GOAL_REVIEW target，永不 POST /claims', async () => {
  const {createControlHttpGoalReviewPorts} = await import('./controlHttpPorts.js');
  const snap = 'sha256:' + '11'.repeat(32);
  const auditRow = {
    id: lease.activity_id,
    kind: 'AUDIT',
    state_revision: 2,
    binding: {
      subject_digest: snap,
      goal_contract_revision: 1,
      plan_revision: 1,
    },
    goal_id: activityRow.goal_id,
    task_id: null,
    project_id: activityRow.project_id,
    target: {type: 'GOAL_REVIEW', id: activityRow.goal_id},
  };
  const fetchImpl = vi.fn(async (url: string) => {
    expect(String(url)).toContain(`/api/v1/activities/${lease.activity_id}`);
    expect(String(url)).not.toContain('/claims');
    return jsonOk(auditRow);
  }) as unknown as typeof fetch;

  const ports = createControlHttpGoalReviewPorts({
    baseUrl: 'http://127.0.0.1:58101',
    authorization: 'jwt',
    admittedLease: lease,
    fetchImpl,
  });
  const claimed = await ports.claimAudit();
  expect(claimed?.activity.target).toEqual({
    type: 'GOAL_REVIEW',
    id: activityRow.goal_id,
  });
  expect(claimed?.activity.kind).toBe('AUDIT');
  expect(fetchImpl).toHaveBeenCalledOnce();
});

test('GoalReview ports：compile / artifact / outcome(target_type=GOAL_REVIEW)', async () => {
  const {createControlHttpGoalReviewPorts} = await import('./controlHttpPorts.js');
  const snap = 'sha256:' + '22'.repeat(32);
  let outcomeBody: unknown;
  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    const method = (init?.method ?? 'GET').toUpperCase();
    if (method === 'POST' && u.includes('/context-compile')) {
      return jsonOk(
        {
          id: 'bundle-gr',
          content_digest: 'sha256:' + '33'.repeat(32),
          content: {
            role: 'AUDITOR',
            input_bindings: [
              {
                classification: 'EVIDENCE',
                digest: snap,
                artifact_id: 'art-snap',
              },
            ],
          },
        },
        201,
      );
    }
    if (method === 'POST' && u.endsWith('/context')) {
      return jsonOk({context_digest: 'sha256:' + '44'.repeat(32)});
    }
    if (method === 'GET' && u.includes('/api/v1/artifacts/art-snap/content')) {
      return {
        ok: true,
        status: 200,
        text: async () =>
          JSON.stringify({open_effect_ids: ['fx-1'], recent_activities: []}),
      };
    }
    if (method === 'POST' && u.includes('/outcomes')) {
      outcomeBody = JSON.parse(String(init?.body ?? '{}'));
      return jsonOk({status: 'SUCCEEDED'});
    }
    throw new Error(`unexpected ${method} ${u}`);
  }) as unknown as typeof fetch;

  const ports = createControlHttpGoalReviewPorts({
    baseUrl: 'http://127.0.0.1:58101',
    authorization: 'Bearer t',
    admittedLease: lease,
    fetchImpl,
  });

  const compiled = await ports.compileContext({
    activityId: lease.activity_id,
    lease,
  });
  expect(compiled.content.role).toBe('AUDITOR');
  await ports.bindContext({
    activityId: lease.activity_id,
    lease,
    bindingDigest: 'sha256:' + '55'.repeat(32),
    contextBundleId: compiled.id,
  });
  const raw = await ports.readEvidenceArtifact('art-snap');
  expect(JSON.parse(raw).open_effect_ids).toEqual(['fx-1']);
  await ports.submitGoalReviewOutcome({
    activityId: lease.activity_id,
    lease,
    expectedStateRevision: 2,
    review: {
      goal_contract_revision: 1,
      plan_revision: 1,
      review_snapshot_digest: snap,
      findings: [
        {
          code: 'OPEN_EFFECTS',
          severity: 'BLOCKER',
          evidence_ids: [],
          recommendation: '有未决 effect',
        },
      ],
    },
  });
  expect(outcomeBody).toMatchObject({
    expected_state_revision: 2,
    outcome: {
      target_type: 'GOAL_REVIEW',
      review: {
        review_snapshot_digest: snap,
        findings: [expect.objectContaining({code: 'OPEN_EFFECTS'})],
      },
    },
  });
});

test('CandidateAudit ports：VerificationRun + outcome(target_type=CANDIDATE)，无 stub 观察', async () => {
  const {createControlHttpCandidateAuditPorts} = await import('./controlHttpPorts.js');
  let runBody: unknown;
  let outcomeBody: unknown;
  const auditRow = {
    id: lease.activity_id,
    kind: 'AUDIT',
    state_revision: 4,
    binding: {goal_contract_revision: 1, task_contract_revision: 1},
    goal_id: activityRow.goal_id,
    task_id: '66666666-6666-6666-6666-666666666666',
    project_id: activityRow.project_id,
    target: {type: 'CANDIDATE', id: 'cand-ports'},
    verification_assignments: [
      {
        subject_id: 'cand-ports',
        subject_digest: 'sha256:' + 'aa'.repeat(32),
        verification_profile_id: 'prof-1',
        layer: 'MECHANICAL',
        audit_round: 1,
      },
    ],
  };
  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    const href = String(url);
    const method = (init?.method ?? 'GET').toUpperCase();
    if (href.includes(`/api/v1/activities/${lease.activity_id}`) && method === 'GET') {
      return jsonOk(auditRow);
    }
    if (href.includes('/internal/v1/verification-runs') && method === 'POST') {
      runBody = JSON.parse(String(init!.body));
      return jsonOk({id: 'run-ports-1'});
    }
    if (href.includes('/outcomes') && method === 'POST') {
      outcomeBody = JSON.parse(String(init!.body));
      return jsonOk({});
    }
    throw new Error(`unexpected ${href} ${method}`);
  }) as unknown as typeof fetch;

  const ports = createControlHttpCandidateAuditPorts({
    baseUrl: 'http://127.0.0.1:58101/',
    authorization: 'jwt',
    admittedLease: lease,
    fetchImpl,
    observeCandidate: async () => ({
      checksPassed: true,
      criterionId: 'A1',
      reason: 'ports 测',
      evidenceArtifactId: '11111111-1111-1111-1111-111111111111',
      receiptArtifactId: '22222222-2222-2222-2222-222222222222',
      verifierDigest: 'sha256:' + 'bb'.repeat(32),
      subjectDigest: 'sha256:' + 'aa'.repeat(32),
      inputDigest: 'sha256:' + 'cc'.repeat(32),
      environmentDigest: 'sha256:' + 'dd'.repeat(32),
    }),
  });

  const claimed = await ports.claimAudit();
  expect(claimed?.activity.target).toEqual({type: 'CANDIDATE', id: 'cand-ports'});
  expect(claimed?.activity.verification_assignments).toHaveLength(1);

  const {runCandidateAuditTurnWithLease} = await import('./candidateAuditor.js');
  const result = await runCandidateAuditTurnWithLease(ports, claimed!);
  expect(result).toMatchObject({
    status: 'ACTIVATION_SUBMITTED',
    verdict: 'PASS',
    verifier_run_id: 'run-ports-1',
    marks_goal_done: false,
  });
  expect(runBody).toMatchObject({
    run: {
      subject_type: 'CANDIDATE',
      subject_id: 'cand-ports',
      layer: 'MECHANICAL',
    },
  });
  expect(outcomeBody).toMatchObject({
    outcome: {
      target_type: 'CANDIDATE',
      audit: {
        verdict: 'PASS',
        verifier_run_ids: ['run-ports-1'],
        subject_candidate_manifest_id: 'cand-ports',
      },
    },
  });
});
