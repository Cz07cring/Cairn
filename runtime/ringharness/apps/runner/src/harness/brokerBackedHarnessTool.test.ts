import {expect, test, vi} from 'vitest';
import {createBrokerBackedHarnessTool} from './brokerBackedHarnessTool.js';

const activation = {
  kind: 'EXECUTE' as const,
  projectId: '11111111-1111-1111-1111-111111111111',
  activityId: '22222222-2222-2222-2222-222222222222',
  lease: {
    activity_id: '22222222-2222-2222-2222-222222222222',
    attempt_id: '33333333-3333-3333-3333-333333333333',
    fencing_epoch: '7',
  },
};

test('execute：Broker 可信终态和结果工件到达后才向 Harness 返回成功', async () => {
  const requests: Array<{url: string; method: string}> = [];
  let effectReads = 0;
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';
    requests.push({url: href, method});

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
        {data: {id: 'effect-1', status: 'PREPARED', state_revision: 1}},
        {status: 201},
      );
    }
    if (method === 'GET' && href.endsWith('/api/v1/effects/effect-1')) {
      effectReads += 1;
      return Response.json({
        data:
          effectReads === 1
            ? {id: 'effect-1', status: 'DISPATCHED', evidence_ids: []}
            : {
                id: 'effect-1',
                status: 'SUCCEEDED',
                evidence_ids: ['result-artifact'],
              },
      });
    }
    if (
      method === 'GET' &&
      href.endsWith('/api/v1/artifacts/result-artifact/content')
    ) {
      return new Response('文件正文', {status: 200});
    }
    throw new Error(`${method} ${href}`);
  });

  const tool = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: fetchImpl as unknown as typeof fetch,
    poll: {maxAttempts: 2, delayMs: 0},
  });

  const result = await tool.execute({
    callId: 'call-1',
    name: 'read_file',
    arguments: JSON.stringify({path: 'src/main.ts'}),
  });

  expect(result).toMatchObject({
    isError: false,
    content: [{type: 'text', text: '文件正文'}],
    meta: {
      effectId: 'effect-1',
      status: 'SUCCEEDED',
      evidenceIds: ['result-artifact'],
      envelope: {spilled: false, runeCount: 4},
    },
  });
  expect(
    requests.some(
      ({url, method}) => method === 'POST' && url.endsWith('/dispatch'),
    ),
  ).toBe(false);
});

test('execute：超单结果预算时外溢到 evidence 指针，不二次 PUT、不写本机 spill', async () => {
  const bigBody = '汉'.repeat(30);
  let putCount = 0;
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';
    if (method === 'PUT' && href.includes('/internal/v1/artifacts/')) {
      putCount += 1;
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
        {data: {id: 'effect-big', status: 'SUCCEEDED', evidence_ids: ['result-big']}},
        {status: 201},
      );
    }
    if (method === 'GET' && href.endsWith('/api/v1/effects/effect-big')) {
      return Response.json({
        data: {
          id: 'effect-big',
          status: 'SUCCEEDED',
          evidence_ids: ['result-big'],
        },
      });
    }
    if (
      method === 'GET' &&
      href.endsWith('/api/v1/artifacts/result-big/content')
    ) {
      return new Response(bigBody, {status: 200});
    }
    throw new Error(`${method} ${href}`);
  });

  const tool = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: fetchImpl as unknown as typeof fetch,
    poll: {maxAttempts: 1, delayMs: 0},
    envelopeBudgets: {maxResultRunes: 10, maxTurnAggregateRunes: 1000, previewRunes: 3},
  });

  const result = await tool.execute({
    callId: 'call-big',
    name: 'read_file',
    arguments: JSON.stringify({path: 'big.txt'}),
  });

  expect(result.isError).toBe(false);
  expect(result.content[0]?.text).toContain('[tool_result_spilled]');
  expect(result.content[0]?.text).toContain('artifacts=result-big');
  expect(result.content[0]?.text).toContain('preview: 汉汉汉…');
  expect(result.content[0]?.text).toContain('外溢≠PASS/DONE');
  expect(result.meta.envelope?.spilled).toBe(true);
  expect(result.meta.evidenceIds).toEqual(['result-big']);
  // 仅输入工件 PUT 一次；外溢复用 evidence，禁止再写结果副本
  expect(putCount).toBe(1);
});

test('execute：UNKNOWN 终态禁止当成功，标记须对账', async () => {
  let effectReads = 0;
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';
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
        {data: {id: 'effect-u', status: 'PREPARED', state_revision: 1}},
        {status: 201},
      );
    }
    if (method === 'GET' && href.endsWith('/api/v1/effects/effect-u')) {
      effectReads += 1;
      return Response.json({
        data: {
          id: 'effect-u',
          status: effectReads === 1 ? 'DISPATCHED' : 'UNKNOWN',
          evidence_ids: [],
        },
      });
    }
    throw new Error(`${method} ${href}`);
  });

  const tool = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: fetchImpl as unknown as typeof fetch,
    poll: {maxAttempts: 2, delayMs: 0},
  });

  const result = await tool.execute({
    callId: 'call-u',
    name: 'read_file',
    arguments: JSON.stringify({path: 'src/main.ts'}),
  });

  expect(result.isError).toBe(true);
  expect(result.error?.info.code).toBe('EFFECT_UNKNOWN');
  expect(result.meta).toMatchObject({
    effectId: 'effect-u',
    status: 'UNKNOWN',
    requiresReconciliation: true,
  });
});

test('execute：轮询未终态 → EFFECT_UNSETTLED，不冒充 SUCCEEDED', async () => {
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';
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
        {data: {id: 'effect-d', status: 'PREPARED', state_revision: 1}},
        {status: 201},
      );
    }
    if (method === 'GET' && href.endsWith('/api/v1/effects/effect-d')) {
      return Response.json({
        data: {id: 'effect-d', status: 'DISPATCHED', evidence_ids: []},
      });
    }
    throw new Error(`${method} ${href}`);
  });

  const tool = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: fetchImpl as unknown as typeof fetch,
    poll: {maxAttempts: 2, delayMs: 0},
  });

  const result = await tool.execute({
    callId: 'call-d',
    name: 'read_file',
    arguments: JSON.stringify({path: 'src/main.ts'}),
  });

  expect(result.isError).toBe(true);
  expect(result.error?.info.code).toBe('EFFECT_UNSETTLED');
  expect(result.meta.requiresReconciliation).toBe(true);
  expect(result.meta.status).toBe('DISPATCHED');
});

test('execute：并行到达的 Harness 调用在 adapter 内串行并形成 Step 链', async () => {
  const stepBodies: Array<Record<string, unknown>> = [];
  let stepIndex = 0;
  let effectIndex = 0;
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';

    if (method === 'PUT' && href.includes('/internal/v1/artifacts/')) {
      return Response.json({data: {id: `input-${stepBodies.length + 1}`}});
    }
    if (method === 'POST' && href.endsWith('/steps')) {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      stepBodies.push(body);
      stepIndex += 1;
      return Response.json(
        {
          data: {
            id: `step-${stepIndex}`,
            logical_step_id: `step-${stepIndex}`,
            intent_revision: 1,
          },
        },
        {status: 201},
      );
    }
    if (method === 'POST' && href.endsWith('/effects/prepare')) {
      effectIndex += 1;
      return Response.json(
        {
          data: {
            id: `effect-${effectIndex}`,
            status: 'PREPARED',
            state_revision: 1,
          },
        },
        {status: 201},
      );
    }
    const effectMatch = href.match(/\/api\/v1\/effects\/(effect-\d+)$/);
    if (method === 'GET' && effectMatch) {
      const id = effectMatch[1] as string;
      return Response.json({
        data: {id, status: 'SUCCEEDED', evidence_ids: [`result-${id}`]},
      });
    }
    const artifactMatch = href.match(/\/api\/v1\/artifacts\/(result-effect-\d+)\/content$/);
    if (method === 'GET' && artifactMatch) {
      return new Response(artifactMatch[1], {status: 200});
    }
    throw new Error(`${method} ${href}`);
  });
  const tool = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: fetchImpl as unknown as typeof fetch,
    poll: {maxAttempts: 1, delayMs: 0},
  });

  const [first, second] = await Promise.all([
    tool.execute({
      callId: 'call-1',
      name: 'read_file',
      arguments: JSON.stringify({path: 'a.ts'}),
    }),
    tool.execute({
      callId: 'call-2',
      name: 'read_file',
      arguments: JSON.stringify({path: 'b.ts'}),
    }),
  ]);

  expect(first.isError).toBe(false);
  expect(second.isError).toBe(false);
  expect(stepBodies).toHaveLength(2);
  expect(stepBodies[0]?.predecessor_step_id).toBeNull();
  expect(stepBodies[1]?.predecessor_step_id).toBe('step-1');
});

test('execute：UNKNOWN 关闭本 activation 后续工具准入且不盲目重试', async () => {
  let putCount = 0;
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';
    if (method === 'PUT' && href.includes('/internal/v1/artifacts/')) {
      putCount += 1;
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
        {data: {id: 'effect-unknown', status: 'PREPARED', state_revision: 1}},
        {status: 201},
      );
    }
    if (method === 'GET' && href.endsWith('/api/v1/effects/effect-unknown')) {
      return Response.json({
        data: {id: 'effect-unknown', status: 'UNKNOWN', evidence_ids: []},
      });
    }
    throw new Error(`${method} ${href}`);
  });
  const tool = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: fetchImpl as unknown as typeof fetch,
    poll: {maxAttempts: 1, delayMs: 0},
  });

  const first = await tool.execute({
    callId: 'call-unknown',
    name: 'read_file',
    arguments: JSON.stringify({path: 'a.ts'}),
  });
  expect(first).toMatchObject({
    isError: true,
    error: {info: {code: 'EFFECT_UNKNOWN'}},
    meta: {requiresReconciliation: true},
  });
  await expect(
    tool.execute({
      callId: 'call-after-unknown',
      name: 'read_file',
      arguments: JSON.stringify({path: 'b.ts'}),
    }),
  ).rejects.toThrow(/TOOL_ADMISSION_CLOSED:EFFECT_UNKNOWN/);
  expect(putCount).toBe(1);
});

test('execute：观察阶段 Hard Idle 关准入并对账，≠DONE、不盲 SUCCEEDED', async () => {
  let now = 0;
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';
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
        {data: {id: 'effect-hang', status: 'PREPARED', state_revision: 1}},
        {status: 201},
      );
    }
    if (method === 'GET' && href.endsWith('/api/v1/effects/effect-hang')) {
      return Response.json({
        data: {id: 'effect-hang', status: 'DISPATCHED', evidence_ids: []},
      });
    }
    throw new Error(`${method} ${href}`);
  });

  const tool = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: fetchImpl as unknown as typeof fetch,
    poll: {
      maxAttempts: 3,
      delayMs: 200,
      sleep: async (ms) => {
        now += ms;
      },
    },
    watchdogConfig: {
      now: () => now,
      budgets: {
        awaiting_effect_observation: {softIdleMs: 50, hardIdleMs: 150},
        executing_tool: {softIdleMs: 10_000, hardIdleMs: 20_000},
      },
    },
  });

  const result = await tool.execute({
    callId: 'call-hang',
    name: 'read_file',
    arguments: JSON.stringify({path: 'hang.ts'}),
  });

  expect(result.isError).toBe(true);
  expect(result.error?.info.code).toBe('WATCHDOG_HARD_IDLE');
  expect(result.meta.requiresReconciliation).toBe(true);
  expect(result.meta.status).toBe('DISPATCHED');
  expect(result.content[0]?.text).toContain('≠DONE');
  expect(tool.watchdog.hardOutcome()?.marksGoalDone).toBe(false);
  expect(tool.watchdog.heartbeatDetails().phase).toBe(
    'awaiting_effect_observation',
  );

  await expect(
    tool.execute({
      callId: 'call-after-hard',
      name: 'read_file',
      arguments: JSON.stringify({path: 'b.ts'}),
    }),
  ).rejects.toThrow(/TOOL_ADMISSION_CLOSED:WATCHDOG_HARD_IDLE/);
});

test('execute：同参同结果重复 → Nudge 后软 ForceStop；换工具仍可写，≠DONE', async () => {
  let effectSeq = 0;
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';
    if (method === 'PUT' && href.includes('/internal/v1/artifacts/')) {
      return Response.json({data: {id: 'input-artifact'}});
    }
    if (method === 'POST' && href.endsWith('/steps')) {
      effectSeq += 1;
      return Response.json(
        {
          data: {
            id: `step-${effectSeq}`,
            logical_step_id: `step-${effectSeq}`,
            intent_revision: 1,
          },
        },
        {status: 201},
      );
    }
    if (method === 'POST' && href.endsWith('/effects/prepare')) {
      const evidence =
        effectSeq <= 3 ? 'result-same' : 'result-write';
      return Response.json(
        {
          data: {
            id: `effect-${effectSeq}`,
            status: 'SUCCEEDED',
            evidence_ids: [evidence],
          },
        },
        {status: 201},
      );
    }
    if (method === 'GET' && href.includes('/api/v1/effects/effect-')) {
      const id = href.split('/').pop() as string;
      const n = Number(id.replace('effect-', ''));
      const evidence = n <= 3 ? 'result-same' : 'result-write';
      return Response.json({
        data: {id, status: 'SUCCEEDED', evidence_ids: [evidence]},
      });
    }
    if (
      method === 'GET' &&
      href.endsWith('/api/v1/artifacts/result-same/content')
    ) {
      return new Response('同一正文', {status: 200});
    }
    if (
      method === 'GET' &&
      href.endsWith('/api/v1/artifacts/result-write/content')
    ) {
      return new Response('{"written":true}', {status: 200});
    }
    throw new Error(`${method} ${href}`);
  });

  const tool = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: fetchImpl as unknown as typeof fetch,
    poll: {maxAttempts: 1, delayMs: 0},
    progressGuardConfig: {repeatSuccessBeforeNudge: 2},
  });

  const args = JSON.stringify({path: 'same.ts'});
  const first = await tool.execute({callId: 'c1', name: 'read_file', arguments: args});
  expect(first.isError).toBe(false);
  expect(first.meta.progressGuard).toBeUndefined();

  const nudged = await tool.execute({callId: 'c2', name: 'read_file', arguments: args});
  expect(nudged.isError).toBe(false);
  expect(nudged.content[0]?.text).toContain('[no_progress_nudge]');
  expect(nudged.meta.progressGuard).toMatchObject({
    action: 'nudge',
    marksGoalDone: false,
  });

  const stopped = await tool.execute({callId: 'c3', name: 'read_file', arguments: args});
  expect(stopped.isError).toBe(true);
  expect(stopped.error?.info.code).toBe('NO_PROGRESS_FORCE_STOP');
  expect(stopped.meta.progressGuard).toMatchObject({
    action: 'force_stop',
    marksGoalDone: false,
    closeToolAdmission: false,
  });
  expect(tool.progressGuard.metrics().forceStopped).toBe(false);

  // 同参再调：软拒绝，不抛关闸
  const banned = await tool.execute({callId: 'c4', name: 'read_file', arguments: args});
  expect(banned.isError).toBe(true);
  expect(banned.error?.info.code).toBe('NO_PROGRESS_FORCE_STOP');
  expect(tool.progressGuard.metrics().forceStopped).toBe(false);

  // 换工具：全闸仍开，可 write（diagnose 读循环后仍能修）
  const written = await tool.execute({
    callId: 'c5',
    name: 'write_file',
    arguments: JSON.stringify({path: 'same.ts', content: 'fixed'}),
  });
  expect(written.isError).toBe(false);
  expect(written.meta.status).toBe('SUCCEEDED');
  expect(written.content[0]?.text).toContain('written');
  expect(tool.progressGuard.metrics().forceStopped).toBe(false);
});

test('execute：prepare 422 不永久关准入，允许换参重试下一工具', async () => {
  let prepares = 0;
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';
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
      prepares += 1;
      if (prepares === 1) {
        return new Response(
          JSON.stringify({
            error: {code: 'VALIDATION_ERROR', message: 'path 非法'},
          }),
          {status: 422},
        );
      }
      return Response.json(
        {data: {id: 'effect-ok', status: 'PREPARED', state_revision: 1}},
        {status: 201},
      );
    }
    if (method === 'GET' && href.endsWith('/api/v1/effects/effect-ok')) {
      return Response.json({
        data: {
          id: 'effect-ok',
          status: 'SUCCEEDED',
          evidence_ids: ['result-ok'],
        },
      });
    }
    if (
      method === 'GET' &&
      href.endsWith('/api/v1/artifacts/result-ok/content')
    ) {
      return new Response('ok-body', {status: 200});
    }
    throw new Error(`${method} ${href}`);
  });

  const tool = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: fetchImpl as unknown as typeof fetch,
    poll: {maxAttempts: 1, delayMs: 0},
  });

  const first = await tool.execute({
    callId: 'bad-args',
    name: 'read_file',
    arguments: JSON.stringify({path: '../escape'}),
  });
  expect(first.isError).toBe(true);
  expect(first.error?.info.code).toBe('EFFECT_PREPARE_REJECTED');
  expect(first.content[0]?.text).toMatch(/EFFECT_PREPARE_FAILED: HTTP 422/);
  expect(first.content[0]?.text).toMatch(/VALIDATION_ERROR|path/);

  const second = await tool.execute({
    callId: 'good-args',
    name: 'read_file',
    arguments: JSON.stringify({path: 'ok.ts'}),
  });
  expect(second.isError).toBe(false);
  expect(second.content[0]?.text).toContain('ok-body');
  expect(prepares).toBe(2);
});

test('execute：seal prepare SEAL_REQUIRES_GREEN_TESTS → ToolResult 可续跑', async () => {
  let prepares = 0;
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';
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
      prepares += 1;
      if (prepares === 1) {
        return new Response(
          JSON.stringify({
            error: {
              code: 'SEAL_REQUIRES_GREEN_TESTS',
              message:
                'SEAL_REQUIRES_GREEN_TESTS: write_file 之后须有 exit_code=0 的 run_tests',
            },
          }),
          {status: 422},
        );
      }
      return Response.json(
        {data: {id: 'effect-ok', status: 'PREPARED', state_revision: 1}},
        {status: 201},
      );
    }
    if (method === 'GET' && href.endsWith('/api/v1/effects/effect-ok')) {
      return Response.json({
        data: {
          id: 'effect-ok',
          status: 'SUCCEEDED',
          evidence_ids: ['result-ok'],
        },
      });
    }
    if (
      method === 'GET' &&
      href.endsWith('/api/v1/artifacts/result-ok/content')
    ) {
      return new Response('exit_code=0\nok', {status: 200});
    }
    throw new Error(`${method} ${href}`);
  });

  const tool = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: fetchImpl as unknown as typeof fetch,
    poll: {maxAttempts: 1, delayMs: 0},
  });

  const sealDenied = await tool.execute({
    callId: 'seal-early',
    name: 'seal_candidate',
    arguments: JSON.stringify({
      verification_profile_ids: ['11111111-1111-1111-1111-111111111111'],
    }),
  });
  expect(sealDenied.isError).toBe(true);
  expect(sealDenied.error?.info.code).toBe('SEAL_REQUIRES_GREEN_TESTS');
  expect(sealDenied.content[0]?.text).toMatch(/exit_code=0/);
  expect(sealDenied.content[0]?.text).toMatch(/run_tests/);

  const after = await tool.execute({
    callId: 'run-green',
    name: 'run_tests',
    arguments: JSON.stringify({suite: 'public'}),
  });
  expect(after.isError).toBe(false);
  expect(prepares).toBe(2);
});

test('execute：createStep 409 不永久关准入，允许下一工具继续', async () => {
  let steps = 0;
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';
    if (method === 'PUT' && href.includes('/internal/v1/artifacts/')) {
      return Response.json({data: {id: 'input-artifact'}});
    }
    if (method === 'POST' && href.endsWith('/steps')) {
      steps += 1;
      if (steps === 1) {
        return new Response(
          JSON.stringify({
            error: {code: 'STEP_CONFLICT', message: 'STEP_CONFLICT'},
          }),
          {status: 409},
        );
      }
      return Response.json(
        {data: {id: 'step-2', logical_step_id: 'step-2', intent_revision: 1}},
        {status: 201},
      );
    }
    if (method === 'POST' && href.endsWith('/effects/prepare')) {
      return Response.json(
        {data: {id: 'effect-ok', status: 'PREPARED', state_revision: 1}},
        {status: 201},
      );
    }
    if (method === 'GET' && href.endsWith('/api/v1/effects/effect-ok')) {
      return Response.json({
        data: {
          id: 'effect-ok',
          status: 'SUCCEEDED',
          evidence_ids: ['result-ok'],
        },
      });
    }
    if (
      method === 'GET' &&
      href.endsWith('/api/v1/artifacts/result-ok/content')
    ) {
      return new Response('ok-body', {status: 200});
    }
    throw new Error(`${method} ${href}`);
  });

  const tool = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: fetchImpl as unknown as typeof fetch,
    poll: {maxAttempts: 1, delayMs: 0},
  });

  const first = await tool.execute({
    callId: 'conflict',
    name: 'write_file',
    arguments: JSON.stringify({path: 'a.ts', content: 'x'}),
  });
  expect(first.isError).toBe(true);
  expect(first.error?.info.code).toBe('STEP_CONFLICT');
  expect(first.content[0]?.text).toMatch(/STEP_CREATE_FAILED: HTTP 409/);

  const second = await tool.execute({
    callId: 'retry',
    name: 'read_file',
    arguments: JSON.stringify({path: 'ok.ts'}),
  });
  expect(second.isError).toBe(false);
  expect(steps).toBe(2);
});

test('第三百三十三批：watchdog Hard 关 host.gate，后续 execute 拒入；≠DONE', async () => {
  let now = 0;
  const tool = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: vi.fn() as unknown as typeof fetch,
    poll: {maxAttempts: 1, delayMs: 0},
    watchdogConfig: {
      now: () => now,
      budgets: {
        awaiting_llm: {softIdleMs: 10, hardIdleMs: 40},
      },
    },
  });

  tool.watchdog.enterPhase('awaiting_llm', 'llm_stream');
  now = 50;
  const hard = tool.watchdog.tick();
  expect(hard?.kind).toBe('hard_idle');
  expect(hard?.marksGoalDone).toBe(false);
  expect(hard?.closesAdmission).toBe(true);

  await expect(
    tool.execute({
      callId: 'after-hard',
      name: 'read_file',
      arguments: JSON.stringify({path: 'x.ts'}),
    }),
  ).rejects.toThrow(/TOOL_ADMISSION_CLOSED/);
});

test('第三百四十批：gate 已因 NO_PROGRESS 关闭时 execute 补落总结 ToolResult；≠DONE', async () => {
  let summaryPuts = 0;
  const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    const method = init?.method ?? 'GET';
    if (method === 'PUT' && href.includes('/internal/v1/artifacts/')) {
      summaryPuts += 1;
      return Response.json({data: {id: `summary-${summaryPuts}`}});
    }
    throw new Error(`${method} ${href}`);
  });
  const tool = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: fetchImpl as unknown as typeof fetch,
    poll: {maxAttempts: 1, delayMs: 0},
  });

  // 模拟：ForceStop 已关闸，但总结 PUT 尚未发生（崩溃窗口）
  tool.progressGuard.observe({
    callId: 'seed',
    tool: 'read_file',
    argsDigest: 'd1',
    outcome: 'UNKNOWN',
    signals: [{kind: 'effect_terminal', value: 'UNKNOWN'}],
  });
  expect(tool.progressGuard.metrics().forceStopped).toBe(true);

  const closed = await tool.execute({
    callId: 'recover-summary',
    name: 'read_file',
    arguments: JSON.stringify({path: 'recover.ts'}),
  });
  expect(closed.isError).toBe(true);
  expect(closed.error?.info?.code).toBe('NO_PROGRESS_FORCE_STOP');
  expect(closed.meta?.progressGuard?.marksGoalDone).toBe(false);
  expect(closed.meta?.summaryArtifactId).toMatch(/^summary-/);
  expect(summaryPuts).toBeGreaterThanOrEqual(1);
});

test('第三百三十三批：goalId 使两代 Broker 工具共享 Nudge 预算；≠DONE', async () => {
  const {resetNudgeBudgetScopeRegistryForTests} = await import(
    './nudgeBudgetScope.js'
  );
  const {argsDigestOfCanonicalPayload} = await import('./noProgressGuard.js');
  resetNudgeBudgetScopeRegistryForTests();

  const goalId = 'goal-b333-broker';
  const argsA = argsDigestOfCanonicalPayload('{"path":"fail.ts"}');
  const failedObs = (callId: string) => ({
    callId,
    tool: 'read_file',
    argsDigest: argsA,
    outcome: 'FAILED' as const,
    signals: [{kind: 'effect_terminal' as const, value: 'FAILED'}],
  });

  const first = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: vi.fn() as unknown as typeof fetch,
    goalId,
    progressGuardConfig: {maxNudgeBudget: 1},
  });
  expect(first.progressGuard.observe(failedObs('a0')).action).toBe('continue');
  expect(first.progressGuard.observe(failedObs('a1')).action).toBe('nudge');
  expect(first.progressGuard.metrics().nudgeBudgetConsumed).toBe(1);

  const second = createBrokerBackedHarnessTool({
    baseUrl: 'http://control.test',
    authorization: 'Bearer worker',
    activation,
    fetchImpl: vi.fn() as unknown as typeof fetch,
    goalId,
    progressGuardConfig: {maxNudgeBudget: 1},
  });
  expect(second.progressGuard.metrics().nudgeBudgetConsumed).toBe(1);
  const verdicts: string[] = [];
  for (let i = 0; i < 5; i += 1) {
    const v = second.progressGuard.observe(failedObs(`b${i}`));
    verdicts.push(v.action);
    expect(v.marksGoalDone).toBe(false);
  }
  expect(verdicts.filter((a) => a === 'nudge')).toHaveLength(0);
  expect(verdicts).toContain('force_stop');
  resetNudgeBudgetScopeRegistryForTests();
});
