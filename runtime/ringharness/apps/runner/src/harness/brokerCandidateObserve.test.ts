import {expect, test, vi} from 'vitest';
import {
  canonicalizeAuditorRunTestsPayload,
  observeCandidateViaBrokerAuditorSuite,
} from './brokerCandidateObserve.js';
import {RUN_TESTS_SCHEMA_DIGEST} from './cordisExecuteBridge.js';
import type {ExecuteToolHost} from './executeToolHost.js';

const claimed = {
  lease: {
    activity_id: 'act-1',
    attempt_id: 'att-1',
    fencing_epoch: '1',
  },
  activity: {
    id: 'act-1',
    kind: 'AUDIT' as const,
    state_revision: 1,
    binding: {goal_contract_revision: 1, task_contract_revision: 1},
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
        // Kernel 在创建审计/FINALIZE 时冻结校验器身份（见 candidates.py / finalization.py）；
        // 桩必须带上，否则与「分配即契约」的真实形状不符。
        verifier_digest: 'sha256:' + 'bb'.repeat(32),
      },
    ],
  },
};

test('canonicalizeAuditorRunTestsPayload：suite=auditor + 现行 schema digest', () => {
  const body = JSON.parse(canonicalizeAuditorRunTestsPayload());
  expect(body).toEqual({
    tool_ref: 'run_tests',
    tool_schema_digest: RUN_TESTS_SCHEMA_DIGEST,
    parameters: {suite: 'auditor'},
  });
});

test('observeCandidateViaBrokerAuditorSuite：绿测 → checksPassed=true', async () => {
  const putCollectorContent = vi.fn(async () => ({
    artifactId: 'in-art',
    digest: 'sha256:' + '11'.repeat(32),
  }));
  const getArtifactContent = vi.fn(async () =>
    JSON.stringify({suite: 'auditor', exit_code: 0, argv: ['python', '-m', 'pytest']}),
  );
  const createStep = vi.fn(async () => ({
    stepId: 'step-1',
    logicalStepId: 'log-1',
    intentRevision: 1,
  }));
  const prepareEffect = vi.fn(async () => ({
    effectId: 'eff-1',
    status: 'PREPARED',
    stateRevision: 1,
  }));
  const dispatchEffect = vi.fn(async () => ({status: 'DISPATCHED'}));
  const getEffect = vi.fn(async () => ({
    id: 'eff-1',
    status: 'SUCCEEDED',
    stateRevision: 2,
    evidenceIds: ['ev-1'],
  }));

  const host: ExecuteToolHost = {
    ports: {createStep, prepareEffect, dispatchEffect},
    gate: {
      allowed: () => true,
      closedReason: () => null,
      onHeartbeatFailure: vi.fn(),
    },
    observe: {
      getEffect,
      postTrustedReceipt: vi.fn(),
    },
    artifacts: {putCollectorContent, getArtifactContent},
  };

  const fetchImpl = vi.fn(async (url: string) => {
    expect(String(url)).toContain('/api/v1/verification-profiles');
    return new Response(
      JSON.stringify({
        data: [
          {
            id: 'prof-1',
            config: {verifier_digest: 'sha256:' + 'bb'.repeat(32)},
          },
        ],
      }),
      {status: 200, headers: {'Content-Type': 'application/json'}},
    );
  }) as unknown as typeof fetch;

  const obs = await observeCandidateViaBrokerAuditorSuite(
    {
      baseUrl: 'http://127.0.0.1:58101',
      authorization: 'jwt',
      fetchImpl,
      host,
      poll: {maxAttempts: 1, delayMs: 0},
    },
    claimed,
  );

  expect(obs.checksPassed).toBe(true);
  expect(obs.criterionId).toBe('A1');
  expect(obs.evidenceArtifactId).toBe('ev-1');
  expect(obs.receiptArtifactId).toBe('ev-1');
  expect(obs.verifierDigest).toBe('sha256:' + 'bb'.repeat(32));
  expect(createStep).toHaveBeenCalledOnce();
  expect(prepareEffect).toHaveBeenCalledOnce();
  // 审计回合同样只 prepare：Runner 不派发，派发与执行归 Broker
  expect(dispatchEffect).not.toHaveBeenCalled();
  expect(putCollectorContent.mock.calls.length).toBe(1);
  const firstCall = putCollectorContent.mock.calls.at(0);
  expect(firstCall).toBeDefined();
  const putBody = (firstCall as unknown as [{body: string}])[0].body;
  expect(JSON.parse(String(putBody)).parameters.suite).toBe('auditor');
});

test('红测 exit_code≠0 → checksPassed=false（非 stub PASS）', async () => {
  const host: ExecuteToolHost = {
    ports: {
      createStep: async () => ({
        stepId: 's',
        logicalStepId: 'l',
        intentRevision: 1,
      }),
      prepareEffect: async () => ({
        effectId: 'e',
        status: 'PREPARED',
        stateRevision: 1,
      }),
      dispatchEffect: async () => ({status: 'DISPATCHED'}),
    },
    gate: {
      allowed: () => true,
      closedReason: () => null,
      onHeartbeatFailure: vi.fn(),
    },
    observe: {
      getEffect: async () => ({
        id: 'e',
        status: 'SUCCEEDED',
        stateRevision: 2,
        evidenceIds: ['ev'],
      }),
      postTrustedReceipt: vi.fn(),
    },
    artifacts: {
      putCollectorContent: async () => ({
        artifactId: 'in',
        digest: 'sha256:' + '11'.repeat(32),
      }),
      getArtifactContent: async () => '{"exit_code":1,"suite":"auditor"}',
    },
  };
  const fetchImpl = vi.fn(async () =>
    new Response(
      JSON.stringify({
        data: [{id: 'prof-1', config: {verifier_digest: 'sha256:' + 'bb'.repeat(32)}}],
      }),
      {status: 200},
    ),
  ) as unknown as typeof fetch;

  const obs = await observeCandidateViaBrokerAuditorSuite(
    {
      baseUrl: 'http://x',
      authorization: 'jwt',
      fetchImpl,
      host,
      poll: {maxAttempts: 1, delayMs: 0},
    },
    claimed,
  );
  expect(obs.checksPassed).toBe(false);
  expect(obs.reason).toMatch(/红/);
});

test('effect 未终态 → 抛 EFFECT_UNSETTLED（禁止 stub）', async () => {
  const onHeartbeatFailure = vi.fn();
  const host: ExecuteToolHost = {
    ports: {
      createStep: async () => ({
        stepId: 's',
        logicalStepId: 'l',
        intentRevision: 1,
      }),
      prepareEffect: async () => ({
        effectId: 'e',
        status: 'PREPARED',
        stateRevision: 1,
      }),
      dispatchEffect: async () => ({status: 'DISPATCHED'}),
    },
    gate: {
      allowed: () => true,
      closedReason: () => null,
      onHeartbeatFailure,
    },
    observe: {
      getEffect: async () => ({
        id: 'e',
        status: 'DISPATCHED',
        stateRevision: 1,
        evidenceIds: [],
      }),
      postTrustedReceipt: vi.fn(),
    },
    artifacts: {
      putCollectorContent: async () => ({
        artifactId: 'in',
        digest: 'sha256:' + '11'.repeat(32),
      }),
      getArtifactContent: async () => '',
    },
  };

  await expect(
    observeCandidateViaBrokerAuditorSuite(
      {
        baseUrl: 'http://x',
        authorization: 'jwt',
        host,
        poll: {maxAttempts: 1, delayMs: 0},
      },
      claimed,
    ),
  ).rejects.toThrow(/EFFECT_UNSETTLED/);
  expect(onHeartbeatFailure).toHaveBeenCalled();
});
