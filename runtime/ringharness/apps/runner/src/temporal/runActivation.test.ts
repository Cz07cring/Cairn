import {existsSync} from 'node:fs';
import {expect, test, vi} from 'vitest';
import type {PutCollectorInput} from '../harness/artifactPutHttpPorts.js';
import {cordisEntryPath} from '../harness/cordisBootGate.js';
import type {CordisPlanBridgePorts} from '../harness/cordisLlmBridge.js';
import {EXECUTE_RUNTIME_OFFICIAL} from '../harness/executeOfficialRuntime.js';
import {isOfficialAgentLoopBuilt} from '../harness/officialAgentLoopPaths.js';
import {
  admittedLeaseFromInput,
  assessPlanHarnessEnv,
  runActivation,
  RUN_ACTIVATION_ACTIVITY_NAME,
  type RunActivationInput,
} from './runActivation.js';

const baseInput: RunActivationInput = {
  activity_id: 'act-1',
  attempt_id: 'att-1',
  goal_id: 'goal-1',
  kind: 'PLAN',
  fencing_epoch: '1',
  owner_epoch: 'owner-1',
};

const checkout = process.env.RING_HARNESS_CHECKOUT;
const cordisBuilt =
  Boolean(checkout) && existsSync(cordisEntryPath(checkout as string));

test('PLAN 缺环境返回 PENDING_ENV，不伪造 PlanCreate', async () => {
  const result = await runActivation(baseInput, {env: {}});
  expect(RUN_ACTIVATION_ACTIVITY_NAME).toBe('RunActivation');
  expect(result).toEqual({
    status: 'PENDING_ENV',
    reason: '缺少 RING_HARNESS_CHECKOUT',
    kind: 'PLAN',
    pending_harness: true,
  });
  expect(result).not.toHaveProperty('plan');
  expect(result).not.toHaveProperty('PlanCreate');
});

test('assessPlanHarnessEnv 需要 checkout + control URL + worker JWT', () => {
  expect(assessPlanHarnessEnv({}).ok).toBe(false);
  expect(
    assessPlanHarnessEnv({
      RING_HARNESS_CHECKOUT: '/tmp/checkout',
      RING_CONTROL_URL: 'http://127.0.0.1:58101',
      RING_WORKER_JWT: 'jwt',
    }),
  ).toEqual({
    ok: true,
    checkout: '/tmp/checkout',
    controlUrl: 'http://127.0.0.1:58101',
    workerJwt: 'jwt',
    liveDispatch: false,
    modelId: 'plan-fixture',
  });
  expect(
    assessPlanHarnessEnv({
      RING_HARNESS_CHECKOUT: '/tmp/checkout',
      RING_CONTROL_URL: 'http://127.0.0.1:58101',
      RING_WORKER_JWT: 'jwt',
      RING_RUNNER_LIVE_DISPATCH: '1',
      RING_LOCAL_QWEN_MODEL: 'Qwen3.8-Flash-Next-Uncensored-Mixed-omlx',
    }).ok,
  ).toBe(true);
});

test('admittedLeaseFromInput 缺 attempt/epoch 则 null', () => {
  expect(admittedLeaseFromInput({...baseInput, attempt_id: undefined})).toBeNull();
  expect(admittedLeaseFromInput(baseInput)).toEqual({
    activity_id: 'act-1',
    attempt_id: 'att-1',
    fencing_epoch: '1',
  });
});

test('EXECUTE 缺 env → PENDING_ENV（与 PLAN 同形）', async () => {
  const result = await runActivation({...baseInput, kind: 'EXECUTE'}, {env: {}});
  expect(result).toEqual({
    status: 'PENDING_ENV',
    reason: '缺少 RING_HARNESS_CHECKOUT',
    kind: 'EXECUTE',
    pending_harness: true,
  });
});

test('EXECUTE 环境齐备无 DI → 默认装配（mock Control）→ NO_TOOL_PROPOSAL', async () => {
  if (!cordisBuilt || !checkout) {
    const result = await runActivation(
      {...baseInput, kind: 'EXECUTE'},
      {
        env: {
          RING_HARNESS_CHECKOUT: checkout || '/tmp/fake-harness-checkout',
          RING_CONTROL_URL: 'http://127.0.0.1:58101',
          RING_WORKER_JWT: 'test-jwt',
        },
      },
    );
    expect(result.status).toBe('PENDING_ENV');
    expect(result.kind).toBe('EXECUTE');
    return;
  }
  const activityId = baseInput.activity_id;
  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    const method = init?.method ?? 'GET';
    expect(u).not.toContain('/claims');
    if (method === 'GET' && u.includes(`/api/v1/activities/${activityId}`)) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          data: {
            id: activityId,
            kind: 'EXECUTE',
            state_revision: 1,
            binding: {goal_contract_revision: 1},
            goal_id: baseInput.goal_id,
            task_id: null,
            project_id: 'p',
          },
        }),
      } as Response;
    }
    if (method === 'POST' && u.includes('/context-compile')) {
      return {
        ok: true,
        status: 201,
        json: async () => ({
          data: {
            id: 'bundle-e',
            content_digest: 'sha256:' + 'a'.repeat(64),
            content: {role: 'EXECUTOR'},
          },
        }),
      } as Response;
    }
    if (method === 'POST' && u.endsWith('/context')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          data: {context_digest: 'sha256:' + 'b'.repeat(64)},
        }),
      } as Response;
    }
    if (method === 'POST' && u.endsWith('/model-invocations')) {
      const body = JSON.parse(String(init?.body ?? '{}')) as {
        exposed_tools?: string[];
      };
      expect(body.exposed_tools).toEqual([
        'read_file',
        'write_file',
        'run_tests',
        'git_diff',
        'seal_candidate',
      ]);
      return {
        ok: true,
        status: 201,
        json: async () => ({data: {id: 'inv-default-e'}}),
      } as Response;
    }
    throw new Error(`unexpected fetch ${method} ${u}`);
  });

  const result = await runActivation(
    {...baseInput, kind: 'EXECUTE'},
    {
      env: {
        RING_HARNESS_CHECKOUT: checkout,
        RING_CONTROL_URL: 'http://127.0.0.1:58101',
        RING_WORKER_JWT: 'test-jwt',
      },
      fetchImpl: fetchImpl as unknown as typeof fetch,
    },
  );
  // 默认 fixture 无 tool-call → 诚实 FAILED，不再 NOT_IMPLEMENTED
  expect(result).toMatchObject({
    status: 'FAILED',
    reason: 'NO_TOOL_PROPOSAL',
    kind: 'EXECUTE',
    pending_harness: false,
  });
});

test.skipIf(!cordisBuilt)(
  'EXECUTE DI fixture：无 tool-call → FAILED NO_TOOL_PROPOSAL',
  async () => {
    const createInvocation = vi.fn(async () => ({invocationId: 'inv-e'}));
    const completeInvocation = vi.fn(async () => ({
      text: 'fixture-execute-no-tools',
    }));
    const {createHeartbeatLinkedAdmissionGate} = await import(
      '../harness/brokerToolHttpPorts.js'
    );
    const gate = createHeartbeatLinkedAdmissionGate();
    const host = {
      gate,
      ports: {
        createStep: vi.fn(),
        prepareEffect: vi.fn(),
        dispatchEffect: vi.fn(),
      },
      observe: {getEffect: vi.fn(), postTrustedReceipt: vi.fn()},
      artifacts: {
        putCollectorContent: vi.fn(),
        getArtifactContent: vi.fn(async () => ''),
      },
    };
    const ports: CordisPlanBridgePorts = {
      claimPlan: async () => ({
        lease: {
          activity_id: baseInput.activity_id,
          attempt_id: baseInput.attempt_id!,
          fencing_epoch: baseInput.fencing_epoch!,
        },
        activity: {
          id: baseInput.activity_id,
          kind: 'EXECUTE',
          state_revision: 1,
          binding: {goal_contract_revision: 1},
          goal_id: baseInput.goal_id,
          task_id: null,
          project_id: 'p',
        },
      }),
      compileContext: async () => ({
        id: 'bundle-e',
        content_digest: 'sha256:' + 'a'.repeat(64),
        content: {role: 'EXECUTOR'},
      }),
      bindContext: async () => ({context_digest: 'sha256:' + 'b'.repeat(64)}),
      modelInvocation: {createInvocation, completeInvocation},
      buildPlan: () => {
        throw new Error('EXECUTE_PATH_NO_PLAN');
      },
      submitPlanOutcome: async () => undefined,
      toolAdmissionGate: gate,
    };

    const result = await runActivation(
      {...baseInput, kind: 'EXECUTE'},
      {
        env: {
          RING_HARNESS_CHECKOUT: checkout as string,
          RING_CONTROL_URL: 'http://127.0.0.1:58101',
          RING_WORKER_JWT: 'test-jwt',
        },
        createCordisPorts: async () => ports,
        createExecuteHost: async () => host,
      },
    );
    expect(result).toMatchObject({
      status: 'FAILED',
      reason: 'NO_TOOL_PROPOSAL',
      kind: 'EXECUTE',
      pending_harness: false,
    });
  },
);

test.skipIf(!cordisBuilt)(
  'EXECUTE DI fixture：tool-call+arguments → effect_ids（≠ Goal DONE）',
  async () => {
    const createInvocation = vi.fn(async () => ({invocationId: 'inv-e2'}));
    const completeInvocation = vi.fn(async () => ({
      text: '',
      chunks: [
        {
          type: 'tool-call' as const,
          name: 'read_file',
          arguments: '{"path":"a.ts"}',
        },
        {type: 'finish' as const, reason: 'stop' as const},
      ],
    }));
    const putCollectorContent = vi.fn(async (_input: PutCollectorInput) => ({
      artifactId: 'art-tool',
      digest: 'sha256:' + 'f'.repeat(64),
    }));
    const createStep = vi.fn(async () => ({
      stepId: 's2',
      logicalStepId: 's2',
      intentRevision: 1,
    }));
    const prepareEffect = vi.fn(async () => ({
      effectId: 'eff-1',
      status: 'PREPARED',
      stateRevision: 1,
    }));
    const dispatchEffect = vi.fn(async () => ({status: 'DISPATCHED'}));
    const {createHeartbeatLinkedAdmissionGate} = await import(
      '../harness/brokerToolHttpPorts.js'
    );
    const gate = createHeartbeatLinkedAdmissionGate();
    const host = {
      gate,
      ports: {createStep, prepareEffect, dispatchEffect},
      observe: {getEffect: vi.fn(), postTrustedReceipt: vi.fn()},
      artifacts: {
        putCollectorContent,
        getArtifactContent: vi.fn(async () => ''),
      },
    };
    const ports: CordisPlanBridgePorts = {
      claimPlan: async () => ({
        lease: {
          activity_id: baseInput.activity_id,
          attempt_id: baseInput.attempt_id!,
          fencing_epoch: baseInput.fencing_epoch!,
        },
        activity: {
          id: baseInput.activity_id,
          kind: 'EXECUTE',
          state_revision: 1,
          binding: {goal_contract_revision: 1},
          goal_id: baseInput.goal_id,
          task_id: null,
          project_id: 'p',
        },
      }),
      compileContext: async () => ({
        id: 'bundle-e2',
        content_digest: 'sha256:' + 'a'.repeat(64),
        content: {role: 'EXECUTOR'},
      }),
      bindContext: async () => ({context_digest: 'sha256:' + 'b'.repeat(64)}),
      modelInvocation: {createInvocation, completeInvocation},
      buildPlan: () => {
        throw new Error('EXECUTE_PATH_NO_PLAN');
      },
      submitPlanOutcome: async () => undefined,
      toolAdmissionGate: gate,
    };

    const result = await runActivation(
      {...baseInput, kind: 'EXECUTE'},
      {
        env: {
          RING_HARNESS_CHECKOUT: checkout as string,
          RING_CONTROL_URL: 'http://127.0.0.1:58101',
          RING_WORKER_JWT: 'test-jwt',
        },
        createCordisPorts: async () => ports,
        createExecuteHost: async () => host,
      },
    );

    expect(result).toEqual({
      status: 'ACTIVATION_SUBMITTED',
      pending_harness: false,
      kind: 'EXECUTE',
      activity_id: 'act-1',
      attempt_id: 'att-1',
      fencing_epoch: '1',
      effect_ids: ['eff-1'],
      marks_goal_done: false,
    });
    expect(putCollectorContent).toHaveBeenCalledOnce();
    const putArg = putCollectorContent.mock.calls[0]?.[0];
    const {READ_FILE_SCHEMA_DIGEST} = await import('../harness/cordisExecuteBridge.js');
    expect(JSON.parse(String(putArg?.body))).toEqual({
      tool_ref: 'read_file',
      tool_schema_digest: READ_FILE_SCHEMA_DIGEST,
      parameters: {path: 'a.ts'},
    });
  },
);

test('EXECUTE 注入 runExecute 可提交 effect_ids（≠ Goal DONE）', async () => {
  const runExecute = vi.fn(async () => ({
    status: 'ACTIVATION_SUBMITTED' as const,
    pending_harness: false as const,
    kind: 'EXECUTE' as const,
    activity_id: 'act-1',
    attempt_id: 'att-1',
    fencing_epoch: '1',
    effect_ids: ['eff-1'],
  }));
  const result = await runActivation(
    {...baseInput, kind: 'EXECUTE'},
    {runExecute},
  );
  expect(result).toEqual({
    status: 'ACTIVATION_SUBMITTED',
    pending_harness: false,
    kind: 'EXECUTE',
    activity_id: 'act-1',
    attempt_id: 'att-1',
    fencing_epoch: '1',
    effect_ids: ['eff-1'],
    marks_goal_done: false,
  });
  expect(runExecute).toHaveBeenCalledOnce();
});

test('注入 runPlan 成功 → ACTIVATION_SUBMITTED（≠ Goal DONE）', async () => {
  const runPlan = vi.fn(async () => ({
    status: 'ACTIVATION_SUBMITTED' as const,
    pending_harness: false as const,
    kind: 'PLAN' as const,
    activity_id: baseInput.activity_id,
    attempt_id: baseInput.attempt_id,
    fencing_epoch: baseInput.fencing_epoch,
  }));
  const result = await runActivation(baseInput, {runPlan});
  expect(runPlan).toHaveBeenCalledOnce();
  expect(result).toEqual({
    status: 'ACTIVATION_SUBMITTED',
    pending_harness: false,
    kind: 'PLAN',
    activity_id: 'act-1',
    attempt_id: 'att-1',
    fencing_epoch: '1',
  });
});

test('注入 runPlan 抛 ROLE_TOOL_FORBIDDEN 不吞掉', async () => {
  await expect(
    runActivation(baseInput, {
      runPlan: async () => {
        throw new Error('ROLE_TOOL_FORBIDDEN');
      },
    }),
  ).rejects.toThrow(/ROLE_TOOL_FORBIDDEN/);
});

test('缺 admit lease → PENDING_ENV（拒绝扫 claim / LEGACY）', async () => {
  const env = {
    RING_HARNESS_CHECKOUT: checkout || '/tmp/fake-harness-checkout',
    RING_CONTROL_URL: 'http://127.0.0.1:58101',
    RING_WORKER_JWT: 'test-jwt',
  };
  // 仅当 Cordis 已构建才会走到 lease 校验；否则先 PENDING_ENV(Cordis)。
  if (!cordisBuilt) {
    const result = await runActivation(
      {...baseInput, attempt_id: undefined, fencing_epoch: undefined},
      {env},
    );
    expect(result.status).toBe('PENDING_ENV');
    expect(result.pending_harness).toBe(true);
    return;
  }
  const result = await runActivation(
    {...baseInput, attempt_id: undefined, fencing_epoch: undefined},
    {env: {...env, RING_HARNESS_CHECKOUT: checkout}},
  );
  expect(result).toEqual({
    status: 'PENDING_ENV',
    reason:
      'TEMPORAL admit 需要 attempt_id 与 fencing_epoch；拒绝无租约扫 claim / LEGACY 回退',
    kind: 'PLAN',
    pending_harness: true,
  });
});

test.skipIf(!cordisBuilt)(
  'Cordis 已构建 + createCordisPorts → ACTIVATION_SUBMITTED',
  async () => {
    const createInvocation = vi.fn(async () => ({invocationId: 'inv-1'}));
    const completeInvocation = vi.fn(async () => ({text: 'fixture'}));
    const submitPlanOutcome = vi.fn(async () => undefined);
    const ports: CordisPlanBridgePorts = {
      claimPlan: async () => ({
        lease: {
          activity_id: baseInput.activity_id,
          attempt_id: baseInput.attempt_id!,
          fencing_epoch: baseInput.fencing_epoch!,
        },
        activity: {
          id: baseInput.activity_id,
          kind: 'PLAN',
          state_revision: 1,
          binding: {goal_contract_revision: 1},
          goal_id: baseInput.goal_id,
          task_id: null,
          project_id: 'p',
        },
      }),
      compileContext: async () => ({
        id: 'bundle-1',
        content_digest: 'sha256:' + 'a'.repeat(64),
        content: {role: 'PLANNER'},
      }),
      bindContext: async () => ({context_digest: 'sha256:' + 'b'.repeat(64)}),
      modelInvocation: {createInvocation, completeInvocation},
      buildPlan: () => ({tasks: [{id: 'T1'}]}),
      submitPlanOutcome,
    };

    const result = await runActivation(baseInput, {
      env: {
        RING_HARNESS_CHECKOUT: checkout,
        RING_CONTROL_URL: 'http://127.0.0.1:58101',
        RING_WORKER_JWT: 'test-jwt',
      },
      createCordisPorts: async () => ports,
    });

    expect(result).toEqual({
      status: 'ACTIVATION_SUBMITTED',
      pending_harness: false,
      kind: 'PLAN',
      activity_id: 'act-1',
      attempt_id: 'att-1',
      fencing_epoch: '1',
    });
    expect(createInvocation).toHaveBeenCalledOnce();
    expect(submitPlanOutcome).toHaveBeenCalledOnce();
  },
);

test.skipIf(!cordisBuilt)(
  '默认 Control HTTP ports（mock fetch）→ ACTIVATION_SUBMITTED',
  async () => {
    const activityId = baseInput.activity_id;
    const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      const method = init?.method ?? 'GET';
      // 禁止全局 claim
      expect(u).not.toContain('/claims');
      if (method === 'GET' && u.includes(`/api/v1/activities/${activityId}`)) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            data: {
              id: activityId,
              kind: 'PLAN',
              state_revision: 2,
              binding: {goal_contract_revision: 1},
              goal_id: baseInput.goal_id,
              task_id: null,
              project_id: 'proj-1',
            },
          }),
        };
      }
      if (method === 'GET' && u.includes(`/api/v1/goals/${baseInput.goal_id}`)) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            data: {
              id: baseInput.goal_id,
              contract: {
                budget: {
                  wall_clock_seconds: 60,
                  max_tokens: 1000,
                  max_cost_usd: '0',
                  max_tool_calls: 1,
                  max_network_calls: 0,
                  max_disk_bytes: 1024,
                  max_gpu_seconds: null,
                },
                success_criteria: [
                  {
                    id: 'C1',
                    verification_profile_id:
                      '55555555-5555-5555-5555-555555555555',
                  },
                ],
              },
            },
          }),
        };
      }
      if (u.includes('/context-compile')) {
        return {
          ok: true,
          status: 201,
          json: async () => ({
            data: {
              id: 'bundle-1',
              content_digest: 'sha256:' + 'a'.repeat(64),
              content: {role: 'PLANNER'},
            },
          }),
        };
      }
      if (u.endsWith('/context')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            data: {context_digest: 'sha256:' + 'b'.repeat(64)},
          }),
        };
      }
      if (u.endsWith('/model-invocations')) {
        return {
          ok: true,
          status: 201,
          json: async () => ({data: {id: 'inv-http'}}),
        };
      }
      if (u.endsWith('/outcomes')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({data: {status: 'SUCCEEDED'}}),
        };
      }
      throw new Error(`unexpected ${method} ${u}`);
    }) as unknown as typeof fetch;

    const result = await runActivation(baseInput, {
      env: {
        RING_HARNESS_CHECKOUT: checkout,
        RING_CONTROL_URL: 'http://127.0.0.1:58101',
        RING_WORKER_JWT: 'test-jwt',
      },
      fetchImpl,
    });

    expect(result).toEqual({
      status: 'ACTIVATION_SUBMITTED',
      pending_harness: false,
      kind: 'PLAN',
      activity_id: 'act-1',
      attempt_id: 'att-1',
      fencing_epoch: '1',
    });
    const urls = (fetchImpl as ReturnType<typeof vi.fn>).mock.calls.map(
      (c) => String(c[0]),
    );
    expect(urls.some((u) => u.includes('/claims'))).toBe(false);
    expect(urls.some((u) => u.includes('/context-compile'))).toBe(true);
    expect(urls.some((u) => u.includes('/outcomes'))).toBe(true);
  },
);

test('EXECUTE 官方 runtime 缺 chat → PENDING_ENV（不静默回退 Cordis）', async () => {
  if (!cordisBuilt) return;
  const result = await runActivation(
    {...baseInput, kind: 'EXECUTE'},
    {
      env: {
        RING_HARNESS_CHECKOUT: checkout as string,
        RING_CONTROL_URL: 'http://127.0.0.1:58101',
        RING_WORKER_JWT: 'test-jwt',
        RING_HARNESS_EXECUTE_RUNTIME: EXECUTE_RUNTIME_OFFICIAL,
        // 故意不设 RING_LOCAL_QWEN_*
      },
    },
  );
  expect(result.status).toBe('PENDING_ENV');
  expect(String((result as {reason?: string}).reason)).toMatch(
    /RING_LOCAL_QWEN|官方 EXECUTE/,
  );
});

const agentLoopBuilt =
  Boolean(checkout) && isOfficialAgentLoopBuilt(checkout);

test.skipIf(!agentLoopBuilt)(
  'EXECUTE 官方 runtime：注入 chat+Control → effect_ids（≠ DONE）',
  async () => {
    const MARKER = 'RING_RUN_ACTIVATION_OFFICIAL_MARKER';
    let chatRound = 0;
    let effectReads = 0;
    const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
      const href = String(url);
      const method = init?.method ?? 'GET';

      if (href.includes('/v1/chat/completions')) {
        chatRound += 1;
        if (chatRound === 1) {
          return new Response(
            JSON.stringify({
              choices: [
                {
                  finish_reason: 'tool_calls',
                  message: {
                    content: null,
                    tool_calls: [
                      {
                        id: 'call_ra_1',
                        type: 'function',
                        function: {
                          name: 'read_file',
                          arguments: '{"path":"notes/execute.txt"}',
                        },
                      },
                    ],
                  },
                },
              ],
            }),
            {status: 200, headers: {'Content-Type': 'application/json'}},
          );
        }
        return new Response(
          JSON.stringify({
            choices: [
              {
                finish_reason: 'stop',
                message: {content: `cited ${MARKER}`},
              },
            ],
          }),
          {status: 200, headers: {'Content-Type': 'application/json'}},
        );
      }

      if (method === 'GET' && href.includes('/api/v1/activities/')) {
        return Response.json({
          data: {
            id: 'act-1',
            kind: 'EXECUTE',
            state_revision: 1,
            binding: {},
            goal_id: 'goal-1',
            task_id: null,
            project_id: '11111111-1111-1111-1111-111111111111',
            lease: {
              activity_id: 'act-1',
              attempt_id: 'att-1',
              fencing_epoch: '1',
            },
          },
        });
      }
      if (method === 'PUT' && href.includes('/internal/v1/artifacts/')) {
        return Response.json({data: {id: 'input-artifact'}});
      }
      if (method === 'POST' && href.endsWith('/steps')) {
        return Response.json(
          {data: {id: 'step-1', logical_step_id: 'step-1', intent_revision: 1}},
          {status: 201},
        );
      }
      if (method === 'POST' && href.endsWith('/effects/prepare')) {
        return Response.json(
          {
            data: {
              id: 'effect-ra-official',
              status: 'PREPARED',
              state_revision: 1,
            },
          },
          {status: 201},
        );
      }
      if (method === 'GET' && href.includes('/api/v1/effects/effect-ra-official')) {
        effectReads += 1;
        return Response.json({
          data:
            effectReads === 1
              ? {
                  id: 'effect-ra-official',
                  status: 'DISPATCHED',
                  evidence_ids: [],
                }
              : {
                  id: 'effect-ra-official',
                  status: 'SUCCEEDED',
                  evidence_ids: ['result-artifact'],
                },
        });
      }
      if (method === 'GET' && href.includes('/api/v1/artifacts/result-artifact/content')) {
        return new Response(`file\n${MARKER}\nend\n`, {status: 200});
      }
      if (method === 'POST' && href.includes('/heartbeat')) {
        return Response.json({data: {renewal_seq: 1}});
      }
      throw new Error(`unexpected ${method} ${href}`);
    }) as unknown as typeof fetch;

    const result = await runActivation(
      {...baseInput, kind: 'EXECUTE'},
      {
        env: {
          RING_HARNESS_CHECKOUT: checkout as string,
          RING_CONTROL_URL: 'http://control.test',
          RING_WORKER_JWT: 'Bearer worker',
          RING_HARNESS_EXECUTE_RUNTIME: EXECUTE_RUNTIME_OFFICIAL,
          RING_LOCAL_QWEN_BASE: 'http://chat.test',
          RING_LOCAL_QWEN_API_KEY: 'k',
          RING_LOCAL_QWEN_MODEL: 'injected-model',
        },
        fetchImpl,
      },
    );

    expect(result).toMatchObject({
      status: 'ACTIVATION_SUBMITTED',
      pending_harness: false,
      kind: 'EXECUTE',
      effect_ids: ['effect-ra-official'],
    });
    expect(result).not.toHaveProperty('goal_done');
    expect(chatRound).toBeGreaterThanOrEqual(2);
    expect(
      (fetchImpl as ReturnType<typeof vi.fn>).mock.calls.some((c) =>
        String(c[0]).endsWith('/dispatch'),
      ),
    ).toBe(false);
  },
  60_000,
);

test.skipIf(!agentLoopBuilt)(
  'EXECUTE 官方 diagnose+FSM：四 prepare、无 dispatch、≠ DONE',
  async () => {
    let stepN = 0;
    let prepareN = 0;
    const prepareTools: string[] = [];
    const effectReads = new Map<string, number>();

    const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
      const href = String(url);
      const method = init?.method ?? 'GET';

      // FSM 时 chat 不应打到 Control mock；若误打则失败关闭
      if (href.includes('/v1/chat/completions')) {
        throw new Error('diagnose FSM 不应走外部 chat completions');
      }

      if (method === 'GET' && href.includes('/api/v1/activities/')) {
        return Response.json({
          data: {
            id: 'act-1',
            kind: 'EXECUTE',
            state_revision: 1,
            binding: {},
            goal_id: 'goal-1',
            task_id: null,
            project_id: '11111111-1111-1111-1111-111111111111',
            lease: {
              activity_id: 'act-1',
              attempt_id: 'att-1',
              fencing_epoch: '1',
            },
          },
        });
      }
      if (method === 'PUT' && href.includes('/internal/v1/artifacts/')) {
        return Response.json({
          data: {id: `input-art-${prepareN + 1}`},
        });
      }
      if (method === 'POST' && href.endsWith('/steps')) {
        stepN += 1;
        return Response.json(
          {
            data: {
              id: `step-${stepN}`,
              logical_step_id: `step-${stepN}`,
              intent_revision: 1,
            },
          },
          {status: 201},
        );
      }
      if (method === 'POST' && href.endsWith('/effects/prepare')) {
        prepareN += 1;
        let toolRef = 'unknown';
        try {
          const body = JSON.parse(String(init?.body ?? '{}')) as {
            tool_ref?: string;
          };
          toolRef = body.tool_ref ?? toolRef;
        } catch {
          /* ignore */
        }
        prepareTools.push(toolRef);
        const id = `effect-diag-${prepareN}`;
        return Response.json(
          {
            data: {
              id,
              status: 'PREPARED',
              state_revision: 1,
              tool_ref: toolRef,
            },
          },
          {status: 201},
        );
      }
      const effectMatch = href.match(/\/api\/v1\/effects\/(effect-[^/?]+)$/);
      if (method === 'GET' && effectMatch) {
        const id = effectMatch[1]!;
        const n = (effectReads.get(id) ?? 0) + 1;
        effectReads.set(id, n);
        return Response.json({
          data:
            n === 1
              ? {id, status: 'DISPATCHED', evidence_ids: []}
              : {id, status: 'SUCCEEDED', evidence_ids: [`result-${id}`]},
        });
      }
      if (method === 'GET' && href.includes('/api/v1/artifacts/result-')) {
        const isWrite = href.includes('effect-diag-3');
        const isRead = href.includes('effect-diag-1');
        const isTests1 = href.includes('effect-diag-2');
        const body = isRead
          ? 'READ_BUG_MARKER\nbuggy'
          : isTests1
            ? 'exit_code=1\nsuite=public'
            : isWrite
              ? 'WRITE_OK_MARKER\nok'
              : 'exit_code=0\nsuite=public';
        return new Response(body, {status: 200});
      }
      if (method === 'POST' && href.includes('/heartbeat')) {
        return Response.json({data: {renewal_seq: 1}});
      }
      throw new Error(`unexpected ${method} ${href}`);
    }) as unknown as typeof fetch;

    const result = await runActivation(
      {...baseInput, kind: 'EXECUTE'},
      {
        env: {
          RING_HARNESS_CHECKOUT: checkout as string,
          RING_CONTROL_URL: 'http://control.test',
          RING_WORKER_JWT: 'Bearer worker',
          RING_HARNESS_EXECUTE_RUNTIME: EXECUTE_RUNTIME_OFFICIAL,
          RING_HARNESS_EXECUTE_OFFICIAL_MODE: 'diagnose',
          RING_HARNESS_EXECUTE_OFFICIAL_FSM: '1',
          RING_HARNESS_EXECUTE_OFFICIAL_FAST_POLL: '1',
          RING_HARNESS_EXECUTE_READ_PATH: 'order_service/store.py',
          RING_HARNESS_EXECUTE_WRITE_PATH: 'order_service/store.py',
          RING_HARNESS_EXECUTE_WRITE_CONTENT: 'fixed\n',
          RING_LOCAL_QWEN_BASE: 'http://chat.test',
          RING_LOCAL_QWEN_API_KEY: 'k',
          RING_LOCAL_QWEN_MODEL: 'injected-model',
        },
        fetchImpl,
      },
    );

    expect(result).toMatchObject({
      status: 'ACTIVATION_SUBMITTED',
      pending_harness: false,
      kind: 'EXECUTE',
      marks_goal_done: false,
      scripted_order: false,
      chat_driven_order: true,
      driver: EXECUTE_RUNTIME_OFFICIAL,
      tool_names: ['read_file', 'run_tests', 'write_file', 'run_tests'],
    });
    expect(result).not.toHaveProperty('goal_done');
    expect((result as {effect_ids?: string[]}).effect_ids).toEqual([
      'effect-diag-1',
      'effect-diag-2',
      'effect-diag-3',
      'effect-diag-4',
    ]);
    expect(prepareTools).toEqual([
      'read_file',
      'run_tests',
      'write_file',
      'run_tests',
    ]);
    expect(
      (fetchImpl as ReturnType<typeof vi.fn>).mock.calls.some((c) =>
        String(c[0]).endsWith('/dispatch'),
      ),
    ).toBe(false);
  },
  90_000,
);

test.skipIf(!agentLoopBuilt)(
  'EXECUTE 官方 diagnose+seal+FSM：五 prepare、透出 tool_names、≠ DONE',
  async () => {
    const profileId = '11111111-1111-1111-1111-111111111111';
    let stepN = 0;
    let prepareN = 0;
    const prepareTools: string[] = [];
    const effectReads = new Map<string, number>();

    const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
      const href = String(url);
      const method = init?.method ?? 'GET';

      if (href.includes('/v1/chat/completions')) {
        throw new Error('diagnose FSM 不应走外部 chat completions');
      }

      if (method === 'GET' && href.includes('/api/v1/activities/')) {
        return Response.json({
          data: {
            id: 'act-1',
            kind: 'EXECUTE',
            state_revision: 1,
            binding: {},
            goal_id: 'goal-1',
            task_id: null,
            project_id: '11111111-1111-1111-1111-111111111111',
            lease: {
              activity_id: 'act-1',
              attempt_id: 'att-1',
              fencing_epoch: '1',
            },
          },
        });
      }
      if (method === 'PUT' && href.includes('/internal/v1/artifacts/')) {
        return Response.json({
          data: {id: `input-art-${prepareN + 1}`},
        });
      }
      if (method === 'POST' && href.endsWith('/steps')) {
        stepN += 1;
        return Response.json(
          {
            data: {
              id: `step-${stepN}`,
              logical_step_id: `step-${stepN}`,
              intent_revision: 1,
            },
          },
          {status: 201},
        );
      }
      if (method === 'POST' && href.endsWith('/effects/prepare')) {
        prepareN += 1;
        let toolRef = 'unknown';
        try {
          const body = JSON.parse(String(init?.body ?? '{}')) as {
            tool_ref?: string;
          };
          toolRef = body.tool_ref ?? toolRef;
        } catch {
          /* ignore */
        }
        prepareTools.push(toolRef);
        const id = `effect-diag-seal-${prepareN}`;
        return Response.json(
          {
            data: {
              id,
              status: 'PREPARED',
              state_revision: 1,
              tool_ref: toolRef,
            },
          },
          {status: 201},
        );
      }
      const effectMatch = href.match(/\/api\/v1\/effects\/(effect-[^/?]+)$/);
      if (method === 'GET' && effectMatch) {
        const id = effectMatch[1]!;
        const n = (effectReads.get(id) ?? 0) + 1;
        effectReads.set(id, n);
        return Response.json({
          data:
            n === 1
              ? {id, status: 'DISPATCHED', evidence_ids: []}
              : {id, status: 'SUCCEEDED', evidence_ids: [`result-${id}`]},
        });
      }
      if (method === 'GET' && href.includes('/api/v1/artifacts/result-')) {
        const isWrite = href.includes('effect-diag-seal-3');
        const isRead = href.includes('effect-diag-seal-1');
        const isTests1 = href.includes('effect-diag-seal-2');
        const isSeal = href.includes('effect-diag-seal-5');
        const body = isRead
          ? 'READ_BUG_MARKER\nbuggy'
          : isTests1
            ? 'exit_code=1\nsuite=public'
            : isWrite
              ? 'WRITE_OK_MARKER\nok'
              : isSeal
                ? JSON.stringify({
                    candidate_manifest_id: 'cand-1',
                    sealed: true,
                  })
                : 'exit_code=0\nsuite=public';
        return new Response(body, {status: 200});
      }
      if (method === 'POST' && href.includes('/heartbeat')) {
        return Response.json({data: {renewal_seq: 1}});
      }
      throw new Error(`unexpected ${method} ${href}`);
    }) as unknown as typeof fetch;

    const result = await runActivation(
      {...baseInput, kind: 'EXECUTE'},
      {
        env: {
          RING_HARNESS_CHECKOUT: checkout as string,
          RING_CONTROL_URL: 'http://control.test',
          RING_WORKER_JWT: 'Bearer worker',
          RING_HARNESS_EXECUTE_RUNTIME: EXECUTE_RUNTIME_OFFICIAL,
          RING_HARNESS_EXECUTE_OFFICIAL_MODE: 'diagnose',
          RING_HARNESS_EXECUTE_OFFICIAL_FSM: '1',
          RING_HARNESS_EXECUTE_OFFICIAL_FAST_POLL: '1',
          RING_HARNESS_EXECUTE_READ_PATH: 'order_service/store.py',
          RING_HARNESS_EXECUTE_WRITE_PATH: 'order_service/store.py',
          RING_HARNESS_EXECUTE_WRITE_CONTENT: 'fixed\n',
          RING_HARNESS_EXECUTE_SEAL_PROFILE_IDS: JSON.stringify([profileId]),
          RING_LOCAL_QWEN_BASE: 'http://chat.test',
          RING_LOCAL_QWEN_API_KEY: 'k',
          RING_LOCAL_QWEN_MODEL: 'injected-model',
        },
        fetchImpl,
      },
    );

    expect(result).toMatchObject({
      status: 'ACTIVATION_SUBMITTED',
      pending_harness: false,
      kind: 'EXECUTE',
      marks_goal_done: false,
      scripted_order: false,
      chat_driven_order: true,
      driver: EXECUTE_RUNTIME_OFFICIAL,
      tool_names: [
        'read_file',
        'run_tests',
        'write_file',
        'run_tests',
        'seal_candidate',
      ],
      effect_ids: [
        'effect-diag-seal-1',
        'effect-diag-seal-2',
        'effect-diag-seal-3',
        'effect-diag-seal-4',
        'effect-diag-seal-5',
      ],
    });
    expect(result).not.toHaveProperty('goal_done');
    expect(prepareTools).toEqual([
      'read_file',
      'run_tests',
      'write_file',
      'run_tests',
      'seal_candidate',
    ]);
    expect(
      (fetchImpl as ReturnType<typeof vi.fn>).mock.calls.some((c) =>
        String(c[0]).endsWith('/dispatch'),
      ),
    ).toBe(false);
  },
  90_000,
);

test('AUDIT 缺 Control URL → PENDING_ENV（无需 Cordis checkout）', async () => {
  const result = await runActivation(
    {...baseInput, kind: 'AUDIT'},
    {env: {}},
  );
  expect(result).toEqual({
    status: 'PENDING_ENV',
    reason: '缺少 RING_RUNNER_CONTROL_URL 或 RING_CONTROL_URL',
    kind: 'AUDIT',
    pending_harness: true,
  });
});

test('AUDIT 缺 admitted lease → PENDING_ENV', async () => {
  const result = await runActivation(
    {...baseInput, kind: 'AUDIT', attempt_id: undefined, fencing_epoch: undefined},
    {
      env: {
        RING_CONTROL_URL: 'http://127.0.0.1:58101',
        RING_WORKER_JWT: 'jwt',
      },
    },
  );
  expect(result.status).toBe('PENDING_ENV');
  expect(String((result as {reason: string}).reason)).toContain('attempt_id');
});

test('AUDIT CANDIDATE target：无 Broker 回执 → FAILED（拒绝 stub PASS）', async () => {
  const fetchImpl = vi.fn(async (url: string | URL) => {
    const href = String(url);
    if (href.includes('/api/v1/activities/')) {
      return new Response(
        JSON.stringify({
          data: {
            id: 'act-1',
            kind: 'AUDIT',
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
              },
            ],
          },
        }),
        {status: 200, headers: {'Content-Type': 'application/json'}},
      );
    }
    // collector / steps / effects 未 mock → 观察失败关闭
    return new Response('not found', {status: 404});
  }) as unknown as typeof fetch;

  const result = await runActivation(
    {...baseInput, kind: 'AUDIT'},
    {
      env: {
        RING_CONTROL_URL: 'http://127.0.0.1:58101',
        RING_WORKER_JWT: 'jwt',
      },
      fetchImpl,
    },
  );
  expect(result.status).toBe('FAILED');
  expect(String((result as {reason: string}).reason)).toMatch(
    /CANDIDATE_OBSERVE_FAILED|COLLECTOR|HTTP/,
  );
  expect(result).not.toMatchObject({verdict: 'PASS'});
});

test('AUDIT CANDIDATE：DI runCandidateAudit → ACTIVATION_SUBMITTED，marks_goal_done=false', async () => {
  const result = await runActivation(
    {...baseInput, kind: 'AUDIT'},
    {
      env: {
        RING_CONTROL_URL: 'http://127.0.0.1:58101',
        RING_WORKER_JWT: 'jwt',
      },
      runCandidateAudit: async () => ({
        status: 'ACTIVATION_SUBMITTED',
        pending_harness: false,
        kind: 'AUDIT',
        activity_id: 'act-1',
        attempt_id: 'att-1',
        fencing_epoch: '1',
        verdict: 'PASS',
        verifier_run_id: 'run-di',
        marks_goal_done: false,
      }),
    },
  );
  expect(result).toMatchObject({
    status: 'ACTIVATION_SUBMITTED',
    kind: 'AUDIT',
    verdict: 'PASS',
    verifier_run_id: 'run-di',
    marks_goal_done: false,
  });
  expect(result).not.toHaveProperty('goal_done');
});

test('FINALIZE：DI runFinalize → ACTIVATION_SUBMITTED，marks_goal_done=false', async () => {
  const result = await runActivation(
    {...baseInput, kind: 'FINALIZE'},
    {
      env: {
        RING_CONTROL_URL: 'http://127.0.0.1:58101',
        RING_WORKER_JWT: 'jwt',
      },
      runFinalize: async () => ({
        status: 'ACTIVATION_SUBMITTED',
        pending_harness: false,
        kind: 'FINALIZE',
        activity_id: 'act-1',
        attempt_id: 'att-1',
        fencing_epoch: '1',
        verdict: 'PASS',
        verifier_run_ids: ['run-fin'],
        marks_goal_done: false,
      }),
    },
  );
  expect(result).toMatchObject({
    status: 'ACTIVATION_SUBMITTED',
    kind: 'FINALIZE',
    verdict: 'PASS',
    verifier_run_ids: ['run-fin'],
    marks_goal_done: false,
  });
  expect(result).not.toHaveProperty('goal_done');
});

test('FINALIZE：无 Broker 回执 → FAILED（拒绝 stub PASS/DONE）', async () => {
  const fetchImpl = vi.fn(async (url: string | URL) => {
    const href = String(url);
    if (href.includes('/api/v1/activities/')) {
      return new Response(
        JSON.stringify({
          data: {
            id: 'act-1',
            kind: 'FINALIZE',
            state_revision: 1,
            binding: {},
            goal_id: 'goal-1',
            task_id: null,
            project_id: 'proj-1',
            target: {type: 'CANDIDATE', id: 'cand-1'},
            verification_assignments: [
              {
                subject_id: 'cand-1',
                subject_digest: 'sha256:' + 'aa'.repeat(32),
                verification_profile_id: 'prof-1',
                layer: 'GLOBAL',
                audit_round: 1,
              },
            ],
          },
        }),
        {status: 200, headers: {'Content-Type': 'application/json'}},
      );
    }
    if (href.includes('/api/v1/goals/')) {
      return new Response(
        JSON.stringify({
          data: {
            id: 'goal-1',
            contract_revision: 1,
            contract: {
              budget: {},
              success_criteria: [
                {id: 'C1', verification_profile_id: 'prof-1'},
              ],
            },
            barrier: {
              id: 'bar-1',
              status: 'SEALED',
              candidate_manifest_id: 'cand-1',
            },
          },
        }),
        {status: 200, headers: {'Content-Type': 'application/json'}},
      );
    }
    return new Response('not found', {status: 404});
  }) as unknown as typeof fetch;

  const result = await runActivation(
    {...baseInput, kind: 'FINALIZE'},
    {
      env: {
        RING_CONTROL_URL: 'http://127.0.0.1:58101',
        RING_WORKER_JWT: 'jwt',
      },
      fetchImpl,
    },
  );
  expect(result.status).toBe('FAILED');
  expect(String((result as {reason: string}).reason)).toMatch(
    /FINALIZE_OBSERVE_FAILED|ARTIFACT_PUT|HTTP/,
  );
  expect(result).not.toMatchObject({verdict: 'PASS'});
});

test('INTEGRATE：DI runIntegrate → ACTIVATION_SUBMITTED，marks_goal_done=false', async () => {
  const result = await runActivation(
    {...baseInput, kind: 'INTEGRATE'},
    {
      env: {
        RING_CONTROL_URL: 'http://127.0.0.1:58101',
        RING_WORKER_JWT: 'jwt',
      },
      runIntegrate: async () => ({
        status: 'ACTIVATION_SUBMITTED',
        pending_harness: false,
        kind: 'INTEGRATE',
        activity_id: 'act-1',
        attempt_id: 'att-1',
        fencing_epoch: '1',
        candidate_manifest_id: 'cand-di',
        marks_goal_done: false,
      }),
    },
  );
  expect(result).toMatchObject({
    status: 'ACTIVATION_SUBMITTED',
    kind: 'INTEGRATE',
    candidate_manifest_id: 'cand-di',
    marks_goal_done: false,
  });
});

test('INTEGRATE：无 PASS 审计 → FAILED（拒绝 stub 候选）', async () => {
  const fetchImpl = vi.fn(async (url: string | URL) => {
    const href = String(url);
    if (href.includes('/api/v1/activities/')) {
      return new Response(
        JSON.stringify({
          data: {
            id: 'act-1',
            kind: 'INTEGRATE',
            state_revision: 1,
            binding: {},
            goal_id: 'goal-1',
            task_id: null,
            project_id: 'proj-1',
            target: {type: 'INTEGRATION', id: 'goal-1'},
          },
        }),
        {status: 200, headers: {'Content-Type': 'application/json'}},
      );
    }
    if (href.includes('/audits')) {
      return new Response(JSON.stringify({data: []}), {
        status: 200,
        headers: {'Content-Type': 'application/json'},
      });
    }
    throw new Error(`unexpected ${href}`);
  }) as unknown as typeof fetch;

  const result = await runActivation(
    {...baseInput, kind: 'INTEGRATE'},
    {
      env: {
        RING_CONTROL_URL: 'http://127.0.0.1:58101',
        RING_WORKER_JWT: 'jwt',
      },
      fetchImpl,
    },
  );
  expect(result.status).toBe('FAILED');
  expect(String((result as {reason: string}).reason)).toMatch(
    /NO_ELIGIBLE_CANDIDATE_AUDIT|NO_PASS_CANDIDATE|INTEGRATE_RESOLVE/,
  );
});

test('AUDIT GOAL_REVIEW：确定性 Critic → ACTIVATION_SUBMITTED，marks_goal_done=false', async () => {
  const snapDigest = 'sha256:' + '11'.repeat(32);
  let outcomeBody: unknown;
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = (init?.method || 'GET').toUpperCase();
    if (method === 'GET' && href.includes('/api/v1/activities/act-1')) {
      return new Response(
        JSON.stringify({
          data: {
            id: 'act-1',
            kind: 'AUDIT',
            state_revision: 2,
            binding: {
              subject_digest: snapDigest,
              goal_contract_revision: 1,
              plan_revision: 1,
            },
            goal_id: 'goal-1',
            task_id: null,
            project_id: 'proj-1',
            target: {type: 'GOAL_REVIEW', id: 'goal-1'},
          },
        }),
        {status: 200, headers: {'Content-Type': 'application/json'}},
      );
    }
    if (method === 'POST' && href.includes('/context-compile')) {
      return new Response(
        JSON.stringify({
          data: {
            id: 'bundle-1',
            content_digest: 'sha256:' + '22'.repeat(32),
            content: {
              role: 'AUDITOR',
              input_bindings: [
                {
                  classification: 'EVIDENCE',
                  digest: snapDigest,
                  artifact_id: 'art-snap',
                },
              ],
            },
          },
        }),
        {status: 201, headers: {'Content-Type': 'application/json'}},
      );
    }
    if (method === 'POST' && href.endsWith('/context')) {
      return new Response(
        JSON.stringify({data: {context_digest: 'sha256:' + '33'.repeat(32)}}),
        {status: 200, headers: {'Content-Type': 'application/json'}},
      );
    }
    if (method === 'GET' && href.includes('/api/v1/artifacts/art-snap/content')) {
      return new Response(
        JSON.stringify({
          open_effect_ids: ['fx-open'],
          recent_activities: [],
          goal_status: 'RUNNING',
        }),
        {status: 200, headers: {'Content-Type': 'application/json'}},
      );
    }
    if (method === 'POST' && href.includes('/outcomes')) {
      outcomeBody = JSON.parse(String(init?.body ?? '{}'));
      return new Response(JSON.stringify({data: {status: 'SUCCEEDED'}}), {
        status: 200,
        headers: {'Content-Type': 'application/json'},
      });
    }
    throw new Error(`unexpected ${method} ${href}`);
  }) as unknown as typeof fetch;

  const result = await runActivation(
    {...baseInput, kind: 'AUDIT'},
    {
      env: {
        RING_CONTROL_URL: 'http://127.0.0.1:58101',
        RING_WORKER_JWT: 'jwt',
      },
      fetchImpl,
    },
  );

  expect(result).toMatchObject({
    status: 'ACTIVATION_SUBMITTED',
    kind: 'AUDIT',
    pending_harness: false,
    marks_goal_done: false,
    finding_codes: ['OPEN_EFFECTS'],
  });
  expect(result).not.toHaveProperty('goal_done');
  expect(outcomeBody).toMatchObject({
    outcome: {
      target_type: 'GOAL_REVIEW',
      review: {
        review_snapshot_digest: snapDigest,
        findings: [expect.objectContaining({code: 'OPEN_EFFECTS', severity: 'BLOCKER'})],
      },
    },
  });
});
